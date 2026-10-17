"""Independent literal SMF fixtures; no Mido serializer is used to make inputs."""
import contextlib
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sustain_report as sr

EOT = b"\x00\xff\x2f\x00"


def smf(*tracks, kind=0, division=480):
    return (b"MThd\x00\x00\x00\x06" + struct.pack(">HHH", kind, len(tracks), division)
            + b"".join(b"MTrk" + struct.pack(">I", len(track)) + track for track in tracks))


class ReportTests(unittest.TestCase):
    def report(self, *tracks, **kwargs):
        return sr.analyze_bytes(smf(*tracks, **kwargs))

    def rejects(self, data, fragment):
        with self.assertRaisesRegex(sr.MidiReportError, fragment):
            sr.analyze_bytes(data)

    def test_threshold_boundaries_are_not_half_pedal_measurements(self):
        report = self.report(b"\x00\xb0\x40\x00\x00\xb0\x40\x3f\x00\xb0\x40\x40\x00\xb0\x40\x7f" + EOT)
        self.assertEqual([e["value"] for e in report["events"]], [0, 63, 64, 127])
        self.assertEqual([e["state"] for e in report["events"]], ["off", "off", "on", "on"])
        self.assertEqual(report["counts"], {"cc64_on": 2, "cc64_off": 2, "cc121_reset_markers": 0})

    def test_default_tempo_and_delta_to_absolute_time(self):
        # Delta 480 = 0x83 0x60; at 480 PPQN default quarter = 0.5 s.
        report = self.report(b"\x83\x60\xb0\x40\x7f\x83\x60\xb0\x40\x00" + EOT)
        self.assertEqual([e["absolute_tick"] for e in report["events"]], [480, 960])
        self.assertEqual([e["seconds"] for e in report["events"]], [0.5, 1.0])
        self.assertEqual(report["duration_seconds"], 1.0)

    def test_tempo_changes_from_every_track(self):
        # Track 0: at tick 0 = 500000; track 1: tick 480 = 1000000.
        conductor = b"\x00\xff\x51\x03\x07\xa1\x20\x87\x40\xff\x2f\x00"
        performance = (b"\x00\xb0\x40\x7f\x83\x60\xff\x51\x03\x0f\x42\x40"
                       b"\x83\x60\xb0\x40\x00" + EOT)
        report = self.report(conductor, performance, kind=1)
        self.assertEqual([e["seconds"] for e in report["events"]], [0.0, 1.5])
        self.assertEqual(report["tempo_event_count"], 2)
        self.assertEqual([e["track_index"] for e in report["tempo_events"]], [0, 1])
        self.assertEqual(report["end_tick"], 960)

    def test_tempo_effect_begins_after_same_tick(self):
        track = (b"\x83\x60\xb0\x40\x7f\x00\xff\x51\x03\x0f\x42\x40"
                 b"\x83\x60\xb0\x40\x00" + EOT)
        self.assertEqual([e["seconds"] for e in self.report(track)["events"]], [0.5, 1.5])

    def test_multiple_tempo_changes_within_one_tick_keep_source_order(self):
        track = (b"\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x51\x03\x0f\x42\x40"
                 b"\x83\x60\xb0\x40\x00" + EOT)
        self.assertEqual(self.report(track)["events"][0]["seconds"], 1.0)

    def test_conflicting_cross_track_tempos_are_rejected(self):
        a = b"\x00\xff\x51\x03\x07\xa1\x20" + EOT
        b = b"\x00\xff\x51\x03\x0f\x42\x40" + EOT
        self.rejects(smf(a, b, kind=1), "gleichzeitige Tempoereignisse")

    def test_identical_cross_track_tempos_are_unambiguous(self):
        tempo = b"\x00\xff\x51\x03\x07\xa1\x20" + EOT
        self.assertEqual(self.report(tempo, tempo, kind=1)["duration_seconds"], 0)

    def test_channels_are_shared_across_tracks(self):
        a = b"\x00\xb2\x40\x7f" + EOT
        b = b"\x83\x60\xb2\x40\x00" + EOT
        report = self.report(a, b, kind=1)
        self.assertEqual([e["channel"] for e in report["events"]], [3, 3])
        self.assertEqual(len(report["channels"]), 1)
        self.assertFalse(report["channels"][0]["active_at_end"])

    def test_channel_sixteen_is_independent_of_channel_one(self):
        report = self.report(b"\x00\xbf\x40\x7f\x00\xb0\x40\x00" + EOT)
        self.assertEqual([c["channel"] for c in report["channels"]], [1, 16])
        self.assertEqual([c["active_at_end"] for c in report["channels"]], [False, True])

    def test_initial_state_unknown_even_when_first_event_is_off(self):
        report = self.report(b"\x83\x60\xb0\x40\x00" + EOT)
        channel = report["channels"][0]
        self.assertTrue(channel["first_value_unknown_before_first_event"])
        self.assertEqual(channel["first_event_tick"], 480)
        self.assertFalse(channel["active_at_end"])

    def test_missing_off_is_only_observed_on_at_end(self):
        report = self.report(b"\x00\xb0\x40\x40\x83\x60\xff\x2f\x00")
        self.assertTrue(report["channels"][0]["active_at_end"])
        self.assertEqual(report["channels"][0]["end_state_reason"], "last_observed_cc64")
        self.assertEqual(report["end_tick"], 480)
        self.assertNotIn("fault", report["channels"][0])

    def test_reset_invalidates_model_until_next_cc64(self):
        report = self.report(b"\x00\xb0\x40\x7f\x01\xb0\x79\x00" + EOT)
        self.assertIsNone(report["channels"][0]["active_at_end"])
        self.assertEqual(report["channels"][0]["end_state_reason"], "reset_requires_receiver_semantics")
        self.assertEqual(report["events"][-1]["controller"], 121)
        self.assertEqual(report["events"][-1]["state"], "unknown")
        renewed = self.report(b"\x00\xb0\x79\x00\x01\xb0\x40\x00" + EOT)
        self.assertFalse(renewed["channels"][0]["active_at_end"])

    def test_all_sound_off_and_all_notes_off_do_not_change_cc64_observation(self):
        report = self.report(b"\x00\xb0\x40\x7f\x01\xb0\x78\x00\x01\xb0\x7b\x00" + EOT)
        self.assertEqual(len(report["events"]), 1)
        self.assertTrue(report["channels"][0]["active_at_end"])

    def test_simultaneous_cross_track_conflict_is_unknown_not_display_order(self):
        report = self.report(b"\x00\xb0\x40\x7f" + EOT, b"\x00\xb0\x40\x00" + EOT, kind=1)
        self.assertEqual([e["track_index"] for e in report["events"]], [0, 1])
        self.assertTrue(all(e["cross_track_order_ambiguous"] for e in report["events"]))
        self.assertIsNone(report["channels"][0]["active_at_end"])
        self.assertEqual(report["channels"][0]["end_state_reason"], "ambiguous_simultaneous_cross_track_order")
        self.assertEqual(report["warnings"][0]["code"], "simultaneous_cross_track_controller_events")

    def test_same_track_order_remains_known(self):
        report = self.report(b"\x00\xb0\x40\x7f\x00\xb0\x40\x00" + EOT)
        self.assertEqual([e["message_index"] for e in report["events"]], [0, 1])
        self.assertFalse(report["channels"][0]["active_at_end"])
        self.assertFalse(report["events"][0]["cross_track_order_ambiguous"])

    def test_simultaneous_identical_outcomes_and_later_resolution(self):
        a = b"\x00\xb0\x40\x7f\x01\xb0\x40\x00" + EOT
        b = b"\x00\xb0\x40\x7f" + EOT
        report = self.report(a, b, kind=1)
        self.assertEqual(report["events"][0]["logical_channel_state_after_tick"], "on")
        self.assertFalse(report["channels"][0]["active_at_end"])

    def test_last_within_each_track_controls_possible_endings(self):
        a = b"\x00\xb0\x40\x7f\x00\xb0\x40\x00" + EOT
        b = b"\x00\xb0\x40\x7f" + EOT
        self.assertIsNone(self.report(a, b, kind=1)["channels"][0]["active_at_end"])

    def test_empty_of_relevant_events_does_not_invent_off_state(self):
        report = self.report(b"\x00\x90\x3c\x40\x01\x80\x3c\x00" + EOT)
        self.assertEqual(report["events"], [])
        self.assertEqual(report["channels"], [])

    def test_running_status_is_parsed_by_mido(self):
        report = self.report(b"\x00\xb0\x40\x7f\x01\x40\x00" + EOT)
        self.assertEqual([e["state"] for e in report["events"]], ["on", "off"])

    def test_sysex_is_not_sent_or_interpreted(self):
        report = self.report(b"\x00\xf0\x03\x7d\x01\xf7\x00\xb0\x40\x7f" + EOT)
        self.assertIn("sysex_not_interpreted", [w["code"] for w in report["warnings"]])
        self.assertTrue(report["channels"][0]["active_at_end"])

    def test_port_zero_and_nonzero_are_rejected(self):
        for port in (0, 4):
            self.rejects(smf(b"\x00\xff\x21\x01" + bytes([port]) + EOT), "MIDI-Port")

    def test_type_two_smpte_zero_division_and_invalid_type_zero(self):
        self.rejects(smf(EOT, kind=2), "Typ 2")
        self.rejects(smf(EOT, division=0xE728), "SMPTE")
        self.rejects(smf(EOT, division=0), "PPQN darf")
        self.rejects(smf(EOT, EOT, kind=0), "genau eine Spur")

    def test_bad_headers_lengths_missing_tracks_and_trailing_bytes(self):
        for data in (b"", b"not-midi", b"MThd\x00\x00\x00\x06"):
            self.rejects(data, "Header")
        self.rejects(b"MThd\x00\x00\x00\x07" + b"\x00" * 7, "sechs Byte")
        self.rejects(smf(EOT)[:-1], "Längenangabe")
        self.rejects(smf(EOT) + b"junk", "Dateiende")
        self.rejects(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0", "MTrk")

    def test_malformed_events_and_end_of_track_policy(self):
        self.rejects(smf(b"\x00\xb0\x40"), "ungültige MIDI")
        self.rejects(smf(b"\x00\xb0\x40\x80" + EOT), "ungültige MIDI")
        self.rejects(smf(b"\x00\xb0\x40\x00"), "End-of-Track")
        self.rejects(smf(EOT + b"\x00\xb0\x40\x00" + EOT), "nach End-of-Track")
        self.rejects(smf(b"\x00\xff\x51\x03\x00\x00\x00" + EOT), "positiven Wert")

    def test_invalid_key_signature_uses_friendly_mido_error(self):
        self.rejects(smf(b"\x00\xff\x59\x02\x08\x00" + EOT), "KeySignatureError")

    def test_real_track_message_limit(self):
        # Program changes with running status take two bytes per event;
        # 25,001 events fit the raw-track limit but exceed the message limit.
        track = b"\x00\xc0\x00" + b"\x00\x00" * sr.MAX_TRACK_MESSAGES + EOT
        self.assertLess(len(track), sr.MAX_TRACK_BYTES)
        self.rejects(smf(track), "Nachrichten")

    def test_non_regular_fifo_is_rejected_without_blocking(self):
        import os
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "not-a-file.mid"
            os.mkfifo(fifo)
            run = subprocess.run([sys.executable, str(Path(sr.__file__)), str(fifo)],
                                 capture_output=True, text=True, timeout=3)
            self.assertEqual(run.returncode, 2)
            self.assertIn("reguläre Datei", run.stderr)
            self.assertNotIn("Traceback", run.stderr)

    def test_size_track_and_message_limits(self):
        self.rejects(b"x" * (sr.MAX_FILE_BYTES + 1), "8 MiB")
        self.rejects(smf(b"x" * (sr.MAX_TRACK_BYTES + 1)), "64 KiB")
        self.rejects(smf(*([EOT] * (sr.MAX_TRACKS + 1)), kind=1), "Spuranzahl")
        # Lowering limits isolates each guard without memory-heavy test input.
        with patch.object(sr, "MAX_TRACK_MESSAGES", 1):
            self.rejects(smf(b"\x00\xb0\x40\x00" + EOT), "Nachrichten")
        with patch.object(sr, "MAX_MESSAGES", 2):
            self.rejects(smf(EOT, EOT, EOT, kind=1), "insgesamt")
        with patch.object(sr, "MAX_REPORTED_EVENTS", 1):
            self.rejects(smf(b"\x00\xb0\x40\x00\x00\xb0\x40\x7f" + EOT), "CC64/CC121")
        with patch.object(sr, "MAX_TEMPO_EVENTS", 0):
            self.rejects(smf(b"\x00\xff\x51\x03\x07\xa1\x20" + EOT), "Tempoereignisse")

    def test_input_stays_unchanged_and_report_has_no_absolute_path(self):
        data = smf(b"\x00\xb0\x40\x7f" + EOT)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private-name.mid"
            source.write_bytes(data)
            before = source.stat().st_mtime_ns
            report = sr.analyze_file(source)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(source.stat().st_mtime_ns, before)
            self.assertEqual(report["input"]["name"], "private-name.mid")
            self.assertEqual(report["input"]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(sorted(Path(directory).iterdir()), [source])

    def test_file_errors_have_no_traceback_or_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "broken.mid"
            bad.write_bytes(b"not midi")
            for path in (bad, Path(directory) / "missing.mid", Path(directory)):
                run = subprocess.run([sys.executable, str(Path(sr.__file__)), str(path)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 2)
                self.assertEqual(run.stdout, "")
                self.assertIn("Fehler:", run.stderr)
                self.assertNotIn("Traceback", run.stderr)

    def test_cli_success_matches_library_and_writes_only_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.mid"
            source.write_bytes(smf(b"\x00\xb0\x40\x7f" + EOT))
            run = subprocess.run([sys.executable, str(Path(sr.__file__)), str(source)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            import json
            self.assertEqual(json.loads(run.stdout), sr.analyze_file(source))
            self.assertEqual(run.stderr, "")
            self.assertEqual(sorted(Path(directory).iterdir()), [source])

    def test_main_reports_dependency_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.mid"
            source.write_bytes(smf(EOT))
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(sr, "_read_mido_track", side_effect=sr.MidiReportError("Mido fehlt.")):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    result = sr.main([str(source)])
            self.assertEqual(result, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
