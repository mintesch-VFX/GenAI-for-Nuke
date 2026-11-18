"""
prompt.py — @-Referenzen im Prompt-Text parsen und auf angeschlossene Eingänge mappen.

Beispiel-Prompt:
    "Mache @ref1 nächtlich, im Stil von @ref2"

@ref1 = erster angeschlossener Eingang, @ref2 = zweiter, usw. (1-basiert, wie im UI).
Das Parsen ist bewusst backend-unabhängig: es liefert die referenzierten Eingangs-
Indizes; das Mapping auf konkrete API-Felder macht das jeweilige Backend.
"""

import re


# @in1, @in2, ... (bevorzugt, passt zu den Eingangs-Namen in1..in4) — auch @ref1
# als Alias. 1-basierte Referenz auf einen angeschlossenen Eingang.
_REF_PATTERN = re.compile(r"@(?:in|ref)(\d+)", re.IGNORECASE)


def find_refs(prompt_text):
    """
    Liefert die im Prompt referenzierten Eingangs-Indizes in Reihenfolge des
    ersten Auftretens, 0-basiert (für Array-Zugriff), ohne Duplikate.

    "@ref1 und @ref3 und nochmal @ref1" -> [0, 2]
    """
    seen = []
    for match in _REF_PATTERN.finditer(prompt_text or ""):
        idx = int(match.group(1)) - 1
        if idx >= 0 and idx not in seen:
            seen.append(idx)
    return seen


_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth"]


def resolve_for_api(prompt_text, video_indices=None):
    """
    Ersetzt @refN im Prompt durch Bezüge, die die Modelle verstehen:
      - Bild-Eingänge -> "the first image", "the second image", ... (natürliche
        Sprache -- funktioniert bei den bisherigen reinen Bild-Referenz-Modellen).
      - Video-Eingänge (Index in video_indices, 0-basiert) -> "@Video1", "@Video2", ...
        Seedance & Co. erwarten für reference_videos explizit dieses Token-Format
        (siehe API-Doku); "the first image" ist für ein Video sinnlos und ohne
        Bezugspunkt -- das ließ ein angeschlossenes Referenz-Video bisher wirkungslos
        bleiben, obwohl es korrekt hochgeladen und mitgeschickt wurde.

    Die Zählung erfolgt jeweils INNERHALB der eigenen Gruppe (1. Video, 2. Video,
    ... bzw. 1. Bild, 2. Bild, ...) in physischer in1..in4-Reihenfolge -- passend zu
    der Reihenfolge, in der die Backends reference_videos[]/reference_images[]
    befüllen (siehe backends/*.py::_apply_inputs).

    Ohne video_indices (Default) verhält sich die Funktion wie bisher: jede @refN
    wird als Bild gezählt (rückwärtskompatibel für Modelle ohne Video-Refs).
    """
    video_indices = video_indices or set()

    def _repl(match):
        n = int(match.group(1))
        idx = n - 1
        if idx in video_indices:
            pos = sum(1 for v in video_indices if v <= idx)
            return "@Video{n}".format(n=pos)
        pos = n - sum(1 for v in video_indices if v < idx)
        if 1 <= pos <= len(_ORDINALS):
            return "the {ord} image".format(ord=_ORDINALS[pos - 1])
        return "image {n}".format(n=pos)

    return _REF_PATTERN.sub(_repl, prompt_text or "")


def validate(prompt_text, connected):
    """
    Prüft, ob alle @inN/@refN im Prompt auf tatsächlich angeschlossene Eingänge zeigen.

    `connected` ist ENTWEDER die Anzahl zusammenhängender Eingänge ab @in1 (int) ODER
    eine Menge/Liste der wirklich belegten, 1-basierten Slot-Nummern. Letzteres ist
    wichtig für LÜCKEN: hängt der Nutzer in1 und in3 an (in2 leer), ist `@in3` gültig,
    weil Slot 3 belegt ist — obwohl nur 2 Eingänge zusammen angeschlossen sind. `@inN`
    ist also fest an den physischen Slot N gekoppelt, nicht an die Reihenfolge.

    Gibt eine Liste menschenlesbarer Fehlermeldungen zurück (leer = alles ok).
    """
    valid = set(range(1, connected + 1)) if isinstance(connected, int) else set(connected)
    errors = []
    for match in _REF_PATTERN.finditer(prompt_text or ""):
        n = int(match.group(1))
        if n < 1:
            errors.append("@in{n}: Referenzen beginnen bei @in1.".format(n=n))
        elif n not in valid:
            errors.append(
                "@in{n} zeigt auf Eingang {n}, der aber nicht angeschlossen ist.".format(n=n)
            )
    return errors


def summarize_refs(prompt_text, connected):
    """
    Kompakter Statustext fuer die Fehlermeldung beim Generate-Klick.
    Beispiel: "@in1 ✓  @in3 ✗  (angeschlossen: in1, in2)".

    Liefert leeren String, wenn der Prompt gar keine @-Refs enthaelt.

    `connected`: Anzahl (int) oder Liste/Menge der belegten 1-basierten Slot-Nummern
    (siehe validate()).
    """
    valid = set(range(1, connected + 1)) if isinstance(connected, int) else set(connected)
    used = []
    seen = set()
    for match in _REF_PATTERN.finditer(prompt_text or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        used.append(n)
    if not used:
        return ""
    parts = []
    for n in used:
        state = "✓" if n in valid else "✗"
        parts.append("@in{n} {s}".format(n=n, s=state))
    conn_list = ", ".join("in{i}".format(i=i) for i in sorted(valid)) if valid else "keine"
    return "  ".join(parts) + "  (angeschlossen: " + conn_list + ")"
