# MIDI Sustain Report

Eine kleine lokale Python-CLI, die Sustain-Controller in einer Standard-MIDI-Datei nachvollziehbar auflistet. Sie eignet sich beispielsweise, um zwei Exporte zu vergleichen oder vor einem technischen Gespräch festzuhalten, wann ein Programm CC64 beziehungsweise CC121 gespeichert hat. Die Datei wird ausschließlich gelesen. Das Programm öffnet keine MIDI-Ports, sendet nichts an Instrumente, spielt keinen Ton und verwendet kein Netzwerk.

## Schnell anfangen

Benötigt werden Python **3.10 oder neuer** und Mido **1.3.3**. Mido übernimmt die MIDI-Ereignisdekodierung; ein eigener MIDI-Parser wird hier nicht nachgebaut. Die Versionsangabe wurde am 17. Oktober 2026 mit dem offiziellen PyPI-Eintrag abgeglichen. Nach dem Herunterladen des Repositorys im Projektordner ausführen:

```sh
python3.10 -m venv .venv
```

Virtuelle Umgebung aktivieren:

```sh
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell stattdessen:
# .venv\Scripts\Activate.ps1
```

```sh
python -m pip install -r requirements.txt
python sustain_report.py demo.mid
python -m unittest -v
```

Anstelle von `python3.10` kann beim Erstellen der Umgebung auch ein vorhandenes neueres Python verwendet werden. Die Installation der Abhängigkeit benötigt einen Paketdownload; die anschließende Analyse arbeitet offline. JSON erscheint ausschließlich auf der Standardausgabe. Es gibt keinen Ausgabepfad-Parameter und keine automatische Speicherung, Verzeichnisanlage oder Überschreibung. Eine ungültige beziehungsweise nicht unterstützte Eingabe beendet die CLI mit Status **2** und einer verständlichen Fehlermeldung auf stderr, ohne Teilbericht oder Traceback. Erfolg hat Status **0**.

## Ein nachvollziehbares Beispiel

`demo.mid` wurde für dieses Projekt künstlich erzeugt und ist keine Aufnahme eines Musikers oder eines Instruments. Die dazugehörige vollständige Ausgabe steht in `example-report.json`. Bei 480 Ticks pro Viertelnote wechselt das Tempo an Tick 480 von 500.000 auf 1.000.000 Mikrosekunden pro Viertelnote:

| Tick | Sekunden | Kanal | Controller | Wert | Beobachtung |
| ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 0,0 | 1 | 64 | 127 | on |
| 480 | 0,5 | 1 | 64 | 63 | off |
| 720 | 1,0 | 1 | 121 | 0 | unknown; Reset-Markierung |
| 960 | 1,5 | 1 | 64 | 64 | on |

Der letzte Eintrag ergibt `active_at_end: true`. Das bedeutet ausschließlich: Das letzte auswertbare CC64-Ereignis liegt im On-Bereich. Ein fehlendes späteres Off ist eine Beobachtung im Export, kein Beweis für einen defekten Kontakt, einen fehlerhaften Spieler oder tatsächlich weiterklingende Saiten.

## Was die Felder bedeuten

CC64 wird nach der MIDI-1.0-Schwelle eingeordnet: **0–63 = off, 64–127 = on**. Rohwerte bleiben erhalten. Daraus werden weder Halbpedalwege noch Kalibrierung, Polarität oder mechanische Pedalposition abgeleitet. Vor dem ersten relevanten Ereignis ist der Zustand unbekannt; `first_value_unknown_before_first_event` hält diese Grenze ausdrücklich fest. Kanäle ohne CC64/CC121 erhalten keinen erfundenen Anfangszustand.

CC121 wird als Reset-Markierung protokolliert. Das vorsichtige Beobachtungsmodell wechselt dabei zu `unknown`, bis ein neues CC64 vorliegt. Es behauptet nicht, welche Controller ein bestimmter Empfänger zurücksetzt. CC120 und CC123 werden nicht als Sustain-Off umgedeutet. SysEx wird weder interpretiert noch gesendet; sein Vorkommen erzeugt einen Hinweis auf mögliche unbeobachtete gerätespezifische Änderungen. Der Bericht simuliert daher weder den vollständigen Empfängerzustand noch klingende Noten.

Absolute Ticks und Sekunden berücksichtigen Tempoänderungen aus allen Spuren. Die Zeitintegration verwendet rationale Zahlen; nur die JSON-Sekunden werden auf neun Nachkommastellen gerundet. Spuren- und Nachrichtenindizes beginnen bei **0**, ausgegebene MIDI-Kanäle bei **1**. Derselbe Kanal kann in mehreren Spuren vorkommen. Bei gleichzeitigem CC64/CC121 desselben Kanals aus verschiedenen Spuren warnt der Bericht ausdrücklich: Die Anzeige nach Spurindex beweist keine Empfänger-Reihenfolge. Unterschiedliche mögliche Endzustände bleiben unbekannt. Widersprüchliche gleichzeitige Tempi werden abgelehnt, weil ihre Sekundenfolge nicht eindeutig wäre.

## Grenzen und Prüfung

Unterstützt werden SMF Typ 0/1 mit PPQN. Typ 2, SMPTE-Zeitteilung, MIDI-Port-Metadaten, zusätzliche Containerblöcke und unvollständige Spuren werden klar abgewiesen. Grenzen: **8 MiB** pro Datei, **256 Spuren**, **64 KiB** Rohdaten und **25.000 Nachrichten** pro Spur, **200.000 Nachrichten**, **50.000 berichtete Ereignisse** und **20.000 Tempoereignisse** insgesamt. Mido liest jeweils nur einen begrenzten Track; Nachrichtenlimits werden unmittelbar danach geprüft. Diese bewusst konservativen Grenzen können auch gültige große Exporte ausschließen.

Die Tests verwenden unabhängig geschriebene MIDI-Bytes und prüfen unter anderem Tempo, Running Status, Kanalteilung, Reihenfolge, Reset, beschädigte Dateien und Schreibfreiheit. Sie ersetzen keine Hardwareprüfung. Im Werkstattkontext kann der Bericht eine präzise Beschreibung ergänzen; Informationen zur [E-Piano-Reparatur bei PIANOHOF](https://www.pianohof.de/leistungen/e-piano-reparatur) erläutern den zugehörigen Arbeitsbereich.

Code und Dokumentation wurden **mit KI-Unterstützung erstellt**. Beispiel und Tests sind synthetisch; es werden keine realen Reparaturmessungen behauptet. Der Projektcode steht unter MIT; Mido besitzt seine eigene MIT-Lizenz. Eine Lizenz für fremde Fotos oder MIDI-Dateien wird dadurch nicht erteilt.

Technische Grundlagen: [Mido-Dateidokumentation](https://mido.readthedocs.io/en/stable/files/midi.html), [Mido auf PyPI](https://pypi.org/project/mido/), [MIDI Association – Control Change Messages](https://midi.org/midi-1-0-control-change-messages).
