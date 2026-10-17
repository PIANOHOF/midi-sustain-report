#!/usr/bin/env python3
"""Read-only CC64/CC121 observations from a bounded Standard MIDI file.

Mido parses MIDI events. This module only validates the SMF container framing,
orders observations, and calculates their times. No MIDI backend is opened.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TRACK_BYTES = 64 * 1024
MAX_TRACKS = 256
MAX_TRACK_MESSAGES = 25_000
MAX_MESSAGES = 200_000
MAX_REPORTED_EVENTS = 50_000
MAX_TEMPO_EVENTS = 20_000
MAX_ABSOLUTE_TICK = (1 << 53) - 1
DEFAULT_TEMPO = 500_000


class MidiReportError(ValueError):
    """An unsupported or invalid input; callers may show this without a traceback."""


def _container(data: bytes) -> tuple[int, int, list[bytes]]:
    """Check only SMF header/chunk boundaries, not MIDI event encoding."""
    if len(data) > MAX_FILE_BYTES:
        raise MidiReportError("Datei überschreitet die Grenze von 8 MiB.")
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiReportError("Kein vollständiger Standard-MIDI-Header (MThd).")
    if struct.unpack(">I", data[4:8])[0] != 6:
        raise MidiReportError("Nur der sechs Byte lange Standard-MIDI-Header wird unterstützt.")
    file_type, track_count, division = struct.unpack(">HHH", data[8:14])
    if file_type not in (0, 1):
        raise MidiReportError("Nur SMF Typ 0 und Typ 1 werden unterstützt; Typ 2 ist asynchron.")
    if not 1 <= track_count <= MAX_TRACKS:
        raise MidiReportError(f"Spuranzahl muss zwischen 1 und {MAX_TRACKS} liegen.")
    if file_type == 0 and track_count != 1:
        raise MidiReportError("SMF Typ 0 muss genau eine Spur enthalten.")
    if division & 0x8000:
        raise MidiReportError("SMPTE-Zeitteilung wird nicht unterstützt; benötigt wird PPQN.")
    if division == 0:
        raise MidiReportError("PPQN darf nicht null sein.")
    chunks: list[bytes] = []
    offset = 14
    for index in range(track_count):
        if offset + 8 > len(data) or data[offset:offset + 4] != b"MTrk":
            raise MidiReportError(f"Spur {index}: vollständiger MTrk-Block fehlt.")
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        if length > MAX_TRACK_BYTES:
            raise MidiReportError(f"Spur {index} überschreitet 64 KiB Rohdaten.")
        start, end = offset + 8, offset + 8 + length
        if end > len(data):
            raise MidiReportError(f"Spur {index}: MTrk-Längenangabe überschreitet die Datei.")
        chunks.append(data[start:end])
        offset = end
    if offset != len(data):
        raise MidiReportError("Zusätzliche oder nicht deklarierte Blöcke am Dateiende.")
    return file_type, division, chunks


def _read_mido_track(chunk: bytes, ppqn: int, index: int):
    try:
        import mido
    except ImportError as exc:
        raise MidiReportError("Mido fehlt. Bitte requirements.txt in einer virtuellen Umgebung installieren.") from exc
    # The documented file= interface parses one bounded track at a time.
    framed = struct.pack(">4sIHHH4sI", b"MThd", 6, 0, 1, ppqn, b"MTrk", len(chunk)) + chunk
    try:
        parsed = mido.MidiFile(file=io.BytesIO(framed), clip=False)
    except (EOFError, OSError, ValueError, TypeError, IndexError, KeyError, struct.error, mido.KeySignatureError) as exc:
        raise MidiReportError(f"Spur {index}: ungültige MIDI-Ereignisdaten ({type(exc).__name__}).") from exc
    track = parsed.tracks[0]
    if len(track) > MAX_TRACK_MESSAGES:
        raise MidiReportError(f"Spur {index} überschreitet {MAX_TRACK_MESSAGES} Nachrichten.")
    if not track or track[-1].type != "end_of_track":
        raise MidiReportError(f"Spur {index}: abschließende End-of-Track-Nachricht fehlt.")
    if any(message.type == "end_of_track" for message in track[:-1]):
        raise MidiReportError(f"Spur {index}: Ereignisse nach End-of-Track werden nicht akzeptiert.")
    return track


def _seconds_at_ticks(ticks: set[int], tempos: list[tuple[int, int, int, int]], ppqn: int):
    """Integrate integer microseconds exactly; round only the JSON representation."""
    by_tick: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    for event in tempos:
        by_tick[event[0]].append(event)
    tempo = DEFAULT_TEMPO
    previous_tick = 0
    elapsed = Fraction(0)
    result: dict[int, float] = {}
    for tick in sorted(ticks | set(by_tick)):
        elapsed += Fraction((tick - previous_tick) * tempo, ppqn * 1_000_000)
        result[tick] = round(float(elapsed), 9)
        previous_tick = tick
        # Intra-track order is real. Conflicting simultaneous cross-track
        # tempos are rejected by analyze_bytes, rather than inventing timing.
        for _, _, _, new_tempo in sorted(by_tick.get(tick, [])):
            tempo = new_tempo
    return result


def analyze_bytes(data: bytes, name: str = "input.mid") -> dict:
    """Return JSON-compatible observations; never infer physical pedal condition."""
    file_type, ppqn, chunks = _container(data)
    relevant: list[dict] = []
    tempos: list[tuple[int, int, int, int]] = []
    tracks: list[dict] = []
    total_messages = 0
    sysex_count = 0
    warnings: list[dict] = []
    for track_index, chunk in enumerate(chunks):
        track = _read_mido_track(chunk, ppqn, track_index)
        total_messages += len(track)
        if total_messages > MAX_MESSAGES:
            raise MidiReportError(f"Datei überschreitet {MAX_MESSAGES} Nachrichten insgesamt.")
        tick = 0
        for message_index, message in enumerate(track):
            tick += message.time
            if tick > MAX_ABSOLUTE_TICK:
                raise MidiReportError("Absolute Tickzahl überschreitet den sicheren JSON-Ganzzahlbereich.")
            if message.type == "midi_port":
                raise MidiReportError("MIDI-Port-Metadaten werden nicht unterstützt: Kanalzuordnung wäre mehrdeutig.")
            if message.type == "set_tempo":
                if message.tempo <= 0:
                    raise MidiReportError("Set-Tempo muss einen positiven Wert enthalten.")
                tempos.append((tick, track_index, message_index, message.tempo))
                if len(tempos) > MAX_TEMPO_EVENTS:
                    raise MidiReportError(f"Datei überschreitet {MAX_TEMPO_EVENTS} Tempoereignisse.")
            elif message.type == "sysex":
                sysex_count += 1
            elif message.type == "control_change" and message.control in (64, 121):
                state = ("on" if message.value >= 64 else "off") if message.control == 64 else "unknown"
                relevant.append({
                    "absolute_tick": tick,
                    "track_index": track_index,
                    "message_index": message_index,
                    "channel": message.channel + 1,
                    "controller": message.control,
                    "value": message.value,
                    "state": state,
                    "meaning": "damper_pedal_threshold" if message.control == 64 else "reset_requires_receiver_semantics",
                })
                if len(relevant) > MAX_REPORTED_EVENTS:
                    raise MidiReportError(f"Datei überschreitet {MAX_REPORTED_EVENTS} CC64/CC121-Ereignisse.")
        tracks.append({"track_index": track_index, "message_count": len(track), "end_tick": tick})
        del track

    tempo_groups: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    for event in tempos:
        tempo_groups[event[0]].append(event)
    for tick, group in tempo_groups.items():
        last_by_track = {}
        for event in sorted(group):
            last_by_track[event[1]] = event[3]
        if len(set(last_by_track.values())) > 1:
            raise MidiReportError(
                f"Widersprüchliche gleichzeitige Tempoereignisse in mehreren Spuren bei Tick {tick}; "
                "Sekunden sind ohne zusätzliche Reihenfolgefestlegung nicht eindeutig."
            )
    last_tick = max(track["end_tick"] for track in tracks)
    times = _seconds_at_ticks({event["absolute_tick"] for event in relevant} | {last_tick}, tempos, ppqn)
    relevant.sort(key=lambda event: (event["absolute_tick"], event["track_index"], event["message_index"]))

    grouped: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for event in relevant:
        event["seconds"] = times[event["absolute_tick"]]
        grouped[(event["absolute_tick"], event["channel"])].append(event)
    channel_states: dict[int, dict] = {}
    for (tick, channel), group in sorted(grouped.items()):
        last_by_track: dict[int, dict] = {}
        for event in group:
            last_by_track[event["track_index"]] = event
        ambiguous_order = len(last_by_track) > 1
        endings = {event["state"] for event in last_by_track.values()}
        state = next(iter(endings)) if len(endings) == 1 else "unknown"
        if ambiguous_order:
            warnings.append({
                "code": "simultaneous_cross_track_controller_events",
                "absolute_tick": tick,
                "channel": channel,
                "track_indices": sorted(last_by_track),
                "message": "Gleichzeitige CC64/CC121-Ereignisse desselben Kanals in mehreren Spuren: "
                           "Die Anzeige nach Spurindex ist keine belegte Empfänger-Reihenfolge. "
                           "Unterschiedliche mögliche Endzustände werden als unknown geführt.",
            })
        reason = ("ambiguous_simultaneous_cross_track_order" if len(endings) > 1
                  else "reset_requires_receiver_semantics" if state == "unknown" else "last_observed_cc64")
        for event in group:
            event["cross_track_order_ambiguous"] = ambiguous_order
            event["logical_channel_state_after_tick"] = state
        if channel not in channel_states:
            channel_states[channel] = {
                "channel": channel,
                "first_value_unknown_before_first_event": True,
                "first_event_tick": tick,
                "first_event_seconds": times[tick],
            }
        channel_states[channel].update({
            "observed_state_at_end": state,
            "active_at_end": True if state == "on" else False if state == "off" else None,
            "end_state_reason": reason,
            "last_event_tick": tick,
        })
    if any(event["controller"] == 121 for event in relevant):
        warnings.append({
            "code": "cc121_receiver_semantics",
            "message": "CC121 wird protokolliert und setzt das Beobachtungsmodell auf unknown. "
                       "Welche Controller der konkrete Empfänger zurücksetzt, wird nicht behauptet.",
        })
    if sysex_count:
        warnings.append({
            "code": "sysex_not_interpreted",
            "count": sysex_count,
            "message": "SysEx wird nicht interpretiert oder gesendet; gerätespezifische Zustandsänderungen bleiben außerhalb dieses Berichts.",
        })
    return {
        "schema_version": 1,
        "input": {"name": Path(name).name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
        "scope": "CC64 threshold observations and CC121 reset markers; no receiver or hardware diagnosis",
        "smf_type": file_type,
        "ticks_per_quarter_note": ppqn,
        "default_tempo_microseconds_per_quarter": DEFAULT_TEMPO,
        "tempo_event_count": len(tempos),
        "tempo_events": [{
            "absolute_tick": tick,
            "seconds": times[tick],
            "track_index": track_index,
            "message_index": message_index,
            "microseconds_per_quarter": tempo,
        } for tick, track_index, message_index, tempo in sorted(tempos)],
        "message_count": total_messages,
        "end_tick": last_tick,
        "duration_seconds": times[last_tick],
        "display_order": ["absolute_tick", "track_index", "message_index"],
        "channel_numbers": "1..16; channels are shared across tracks, not one channel per track",
        "seconds_rounding_decimal_places": 9,
        "tracks": tracks,
        "counts": {
            "cc64_on": sum(event["controller"] == 64 and event["state"] == "on" for event in relevant),
            "cc64_off": sum(event["controller"] == 64 and event["state"] == "off" for event in relevant),
            "cc121_reset_markers": sum(event["controller"] == 121 for event in relevant),
        },
        "channels": [channel_states[channel] for channel in sorted(channel_states)],
        "events": relevant,
        "warnings": warnings,
    }


def analyze_file(path: str | os.PathLike) -> dict:
    """Open only a regular input file, read at most the limit plus one byte."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MidiReportError("Eingabe muss eine reguläre Datei sein; keine Geräte, FIFOs oder Verzeichnisse.")
            if info.st_size > MAX_FILE_BYTES:
                raise MidiReportError("Datei überschreitet die Grenze von 8 MiB.")
            data = source.read(MAX_FILE_BYTES + 1)
    except MidiReportError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise MidiReportError(f"Eingabedatei kann nicht gelesen werden ({type(exc).__name__}).") from exc
    return analyze_bytes(data, os.fspath(path))


def main(argv: list[str] | None = None) -> int:
    # Running this CLI should not create dependency bytecode caches either.
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description="Lokaler, schreibgeschützter CC64/CC121-Bericht für SMF Typ 0/1.")
    parser.add_argument("midi_file", help="Vorhandene .mid-Datei; JSON wird ausschließlich auf stdout ausgegeben.")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        print("Fehler: Python 3.10 oder neuer wird benötigt.", file=sys.stderr)
        return 2
    try:
        report = analyze_file(args.midi_file)
    except MidiReportError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
