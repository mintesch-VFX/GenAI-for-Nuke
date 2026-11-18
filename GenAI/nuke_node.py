"""
nuke_node.py — die eigentliche GenAI-Node in Nuke (Group + Knob-UI + Generierung).

MVP-Stufe: ein Modell (Nano Banana), Referenz-Eingänge + @ref-Auflösung, Prompt,
Generate-Button, Live-Status/Timer. Async: Rendern der Eingänge im Main-Thread,
API-Roundtrip im Hintergrund-Thread, UI/Graph-Updates via nuke.executeInMainThread.

Braucht das Nuke-Python (`import nuke`).
"""

import base64
import datetime
import json
import os
import shutil
import threading
import time
import traceback

import nuke

from . import config, render, video
from . import prompt as prompt_mod
from .backends import get_backend
from .backends.base import DONE, ERROR


_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")

# Eingänge werden schlicht nummeriert (in1..in4); welche Rolle ein Eingang im
# aktuellen Modell/Mode hat, zeigt der "Inputs"-Hinweistext in der Node.
INPUT_NAMES = ["in1", "in2", "in3", "in4"]
NUM_INPUTS = len(INPUT_NAMES)

LEGAL_TEXT = {
    "green": "\U0001F7E2 cleared for use",
    "yellow": "\U0001F7E1 only with proper contract",
    "red": "\U0001F534 not approved",
}


# ---- Manifest ------------------------------------------------------------------

def load_models():
    with open(_MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)["models"]


def _find_model(label, iface=None):
    """
    Manifest-Eintrag zu einem Dropdown-Label (None, wenn unbekannt). Labels sind nur je Interface
    eindeutig ("Kling 4.0" gibt es in API und MCP) -> mit iface zuerst dort suchen. Alte Labels
    (mit Klammer-Suffix, z. B. "Kling 4.0 (Video, MCP)") finden ueber "legacy_labels" weiter ihr Modell.
    """
    label = (label or "").strip()
    models = load_models()
    pools = [[m for m in models if _interface_of(m) in (None, iface)], models] if iface else [models]
    for pool in pools:
        for key in ("label", "legacy_labels"):
            for m in pool:
                v = m.get(key)
                if v == label or (isinstance(v, list) and label in v):
                    return m
    return None


def _model_by_label(label, iface=None):
    return _find_model(label, iface) or load_models()[0]


def _node_model(node):
    """Manifest-Eintrag des in der Node gewaehlten Modells (beruecksichtigt das Interface)."""
    return _model_by_label(node["model"].value(), _node_interface(node))


# ---- Interface: API <-> MCP ------------------------------------------------------
#
# Das Interface-Dropdown (oben in der Node) filtert die Modellliste: "API" zeigt die Modelle
# ueber REST/direkte APIs (fal, OpenAI, Magnific-API-Key), "MCP" die Modelle ueber den Magnific-
# MCP-Server (Backend "magnific_mcp"). "Generate Only" (Backend "local") steht in beiden.

INTERFACES = ["API", "MCP"]


def _interface_of(model):
    """"API" | "MCP" | None (None = in beiden Listen, z. B. Generate Only)."""
    backend = model.get("backend")
    if backend == config.LOCAL_BACKEND:
        return None
    return "MCP" if backend == config.MCP_BACKEND else "API"


_SETUP_HINT = "open the GenAI setup window"


def _open_setup():
    """GenAI-Setup-Fenster oeffnen (Magnific-Login + optionale Keys). Nur im Main-Thread aufrufen."""
    try:
        from . import setup_panel
        setup_panel.show_setup()
    except Exception:
        nuke.tprint(traceback.format_exc())


def _has_real_models(iface):
    """True, wenn es fuer das Interface mindestens ein nutzbares Modell ausser "Generate Only" gibt."""
    return any(_model_available(m) and _interface_of(m) == iface and m.get("backend") != config.LOCAL_BACKEND
               for m in load_models())


def _explain_error(exc):
    """Fehlertext fuer die Status-Zeile; fehlende Keys bekommen eine Anleitung statt der Rohmeldung."""
    if isinstance(exc, config.MissingKeyError):
        if exc.key_name == "FAL_KEY":
            return ("Sending images/video to this API model needs a fal.ai key (it hosts the uploads). "
                    "Easier: switch the MCP/API dropdown to MCP and log in with Magnific -- no key needed.")
        return "Missing {k} -- enter it in the GenAI setup window, or switch to MCP and log in.".format(
            k=exc.key_name)
    return str(exc)


def _is_setup_problem(exc_or_text):
    """True, wenn der Fehler durch fehlenden Login/Key entsteht (dann geht das Setup-Fenster auf)."""
    if isinstance(exc_or_text, config.MissingKeyError):
        return True
    text = str(exc_or_text).lower()
    return "log in" in text and ("magnific" in text or "setup window" in text)


def _model_labels(iface):
    """
    Labels der verfuegbaren Modelle fuer das Interface (Key bzw. MCP-Login vorhanden).
    Reihenfolge: Bildmodelle zuerst, dann Videomodelle, "Generate Only" ganz unten
    (innerhalb der Gruppen in Manifest-Reihenfolge).
    """
    def _group(m):
        if m.get("backend") == config.LOCAL_BACKEND:
            return 2
        return 0 if m.get("type") == "image" else 1
    models = [m for m in load_models() if _model_available(m) and _interface_of(m) in (None, iface)]
    labels = [m["label"] for m in sorted(models, key=_group)]   # sorted ist stabil
    return labels or ["— log in: " + _SETUP_HINT + " —"]


def _default_interface():
    """Interface fuer neue Nodes: MCP -- ausser jemand nutzt nur API-Keys und ist nicht bei Magnific eingeloggt."""
    if config.has_backend_key(config.MCP_BACKEND):
        return "MCP"
    return "API" if config.has_any_api_key() else "MCP"


def _node_interface(node):
    """Aktuelles Interface der Node (bei alten Nodes ohne Knob: aus dem Modell abgeleitet)."""
    k = node.knob("interface")
    if k is not None and (k.value() or "").strip() in INTERFACES:
        return k.value().strip()
    try:
        return _interface_of(_model_by_label(node["model"].value())) or "API"
    except Exception:
        return "API"


def _apply_interface(node, iface=None, prefer_model=None):
    """
    Model-Dropdown auf die Modelle des Interfaces umstellen (gewaehltes Modell bleibt, wenn es
    dort vorkommt, sonst das erste) und die Modell-Knobs (Provider/Mode/Parameter) aufbauen.
    """
    iface = iface or _node_interface(node)
    labels = _model_labels(iface)
    mk = node["model"]
    cur = prefer_model or mk.value()
    found = _find_model(cur, iface)             # alte Labels (mit Klammer) auf das neue Label abbilden
    cur = found["label"] if found else cur
    mk.setValues(labels)
    mk.setValue(cur if cur in labels else labels[0])
    if iface == "MCP" and not config.has_backend_key(config.MCP_BACKEND):
        _set_status(node, "Not logged in to Magnific MCP — " + _SETUP_HINT + " (it also opens when you press Generate).")
    return _apply_model(node, _model_by_label(mk.value(), iface))


# ---- Node bauen ----------------------------------------------------------------

def build_node():
    """Legt eine neue GenAI-Node (Group) mit Referenz-Eingängen und Knob-UI an."""
    # Erster Start (weder Magnific-Login noch ein API-Key): das Setup-Fenster oeffnet sich von selbst.
    if not config.is_configured():
        _open_setup()

    # Start im MCP-Interface (nur bei reinen API-Key-Nutzern ohne Login: API).
    iface = _default_interface()
    labels = _model_labels(iface)

    group = nuke.createNode("Group", inpanel=False)
    try:
        group.setName("GenAI")
    except Exception:
        pass  # Nuke vergibt bei Kollision selbst einen eindeutigen Namen

    # Innenleben: N Eingänge + schwarzer Platzhalter am Output
    group.begin()
    for i in range(NUM_INPUTS):
        inp = nuke.nodes.Input()
        inp.setName(INPUT_NAMES[i])
    placeholder = nuke.nodes.Constant()
    placeholder.setName("placeholder")
    placeholder["color"].setValue([0, 0, 0, 1])
    out = nuke.nodes.Output()
    out.setInput(0, placeholder)
    group.end()

    _add_ui_knobs(group, labels, iface)
    _apply_model(group, _model_by_label(labels[0], iface))
    return group


def _add_ui_knobs(group, labels, iface="API", add_tab=True):
    """Alle UI-Knobs der GenAI-Node anlegen (neue Node und Migration alter Nodes)."""
    if add_tab:
        group.addKnob(nuke.Tab_Knob("ai_tab", "GenAI"))

    model_knob = nuke.Enumeration_Knob("model", "Model", labels)
    model_knob.setTooltip(
        "Choose the generative model. Only models whose API key is set appear here.\n\n"
        "GenAI  ·  © 2026 Min Tesch")
    group.addKnob(model_knob)

    # MCP/API-Umschalter rechts neben dem Model-Dropdown (ohne Label, gleiche Zeile).
    iface_knob = nuke.Enumeration_Knob("interface", "", INTERFACES)
    iface_knob.setTooltip(
        "MCP = Magnific MCP (more models, e.g. Kling 4 Modify; needs the Magnific login in the GenAI "
        "setup; bills your personal Magnific account).\n"
        "API = REST/direct APIs (fal, OpenAI, Magnific API key, LTX).")
    iface_knob.clearFlag(nuke.STARTLINE)
    group.addKnob(iface_knob)
    try:
        iface_knob.setValue(iface)
    except Exception:
        pass

    legal_knob = nuke.Text_Knob("legal", "Rights", "")
    group.addKnob(legal_knob)

    # Startwerte NICHT " ": Nuke fixiert die Pulldown-Breite bei Erstellung und
    # vergrößert sie bei setValues() nicht mehr -> breiter Platzhalter, sonst wird
    # z. B. "1080p" auf "10i" abgeschnitten. Die echten Werte setzt _apply_model_params.
    mode_knob = nuke.Enumeration_Knob("mode", "Mode", ["Reference -> Video"])
    mode_knob.setTooltip("Generation mode (model-specific). Changes how the inputs are used.")
    group.addKnob(mode_knob)

    mode_hint = nuke.Text_Knob("mode_hint", "Inputs", "")
    mode_hint.setTooltip("What the connected inputs in1..in4 mean for the current model/mode.")
    group.addKnob(mode_hint)

    group.addKnob(nuke.Text_Knob("div_prompt", ""))  # Trennlinie

    prompt_knob = nuke.Multiline_Eval_String_Knob("prompt", "Prompt", "")
    prompt_knob.setTooltip("Refer to the connected inputs with @in1..@in4 "
                           "(Kling elements: @Element1/@Element2).")
    group.addKnob(prompt_knob)

    # Parameter-Knobs immer anlegen; Werte/Sichtbarkeit richten sich nach dem
    # gewählten Modell (siehe _apply_model_params, auch bei Modellwechsel).
    aspect_knob = nuke.Enumeration_Knob("aspect_ratio", "Aspect Ratio", ["16:9"])
    aspect_knob.setTooltip("Output aspect ratio sent to the model. Match your plate (e.g. 16:9).")
    group.addKnob(aspect_knob)

    res_knob = nuke.Enumeration_Knob("resolution", "Resolution", ["1080p"])
    group.addKnob(res_knob)

    dur_knob = nuke.Enumeration_Knob("duration", "Duration (s)", [" "])
    dur_knob.setTooltip("Video length in seconds.")
    group.addKnob(dur_knob)

    qual_knob = nuke.Enumeration_Knob("quality", "Quality", ["medium"])
    qual_knob.setTooltip("Image quality (GPT). Higher = better but more expensive.")
    group.addKnob(qual_knob)

    cost_knob = nuke.Text_Knob("cost_est", "Est. cost", "")
    cost_knob.setTooltip(
        "Rough estimate for the current settings.\n"
        "Magnific = credits (~€0.0005/credit); fal/OpenAI = approx. USD."
    )
    group.addKnob(cost_knob)

    out_dir_knob = nuke.File_Knob("out_dir", "Output Folder")
    out_dir_knob.setTooltip(
        "Empty = use the installer path setting (or GenAI/UPLOAD + DOWNLOAD next to the .nk).\n"
        "Set = fixed base path; GenAI/UPLOAD + DOWNLOAD are created underneath it."
    )
    group.addKnob(out_dir_knob)

    group.addKnob(nuke.Text_Knob("div_go", ""))  # Trennlinie vor der Aktion

    # Startwert NICHT " ": gleicher Grund wie bei Mode/Resolution oben -- ein
    # echter (langer) Platzhalter verhindert, dass Nuke die Pulldown-Breite beim
    # ersten Befuellen zu knapp fixiert.
    provider_knob = nuke.Enumeration_Knob("provider", "Provider", ["Magnific"])
    provider_knob.setTooltip(
        "Which API/service generates this. Always shown, even with only one option "
        "(e.g. OpenAI, Magnific, fal.ai, Google) -- models offered by more than one "
        "provider (e.g. Seedance 2 via fal or Magnific) let you pick here."
    )
    group.addKnob(provider_knob)

    btn = nuke.PyScript_Knob(
        "generate", "  Generate  ",
        "import GenAI.nuke_node as N; N.generate(nuke.thisNode())",
    )
    btn.clearFlag(nuke.STARTLINE)  # gleiche Zeile wie Provider
    group.addKnob(btn)

    group.addKnob(nuke.Text_Knob("status", "Status", "ready"))

    last_out = nuke.File_Knob("last_output", "Last Result")
    last_out.setTooltip("Path of the most recent generation (informational; editing has no effect).")
    group.addKnob(last_out)

    # ---- History / Extract --------------------------------------------------
    group.addKnob(nuke.Text_Knob("div_hist", "History"))

    gen_sel = nuke.Enumeration_Knob("gen_select", "Generation", [" "])
    gen_sel.setTooltip("Switch between this node's past generations.")
    group.addKnob(gen_sel)

    prev_btn = nuke.PyScript_Knob(
        "gen_prev", "  < Prev  ",
        "import GenAI.nuke_node as N; N.gen_step(nuke.thisNode(), -1)")
    group.addKnob(prev_btn)
    next_btn = nuke.PyScript_Knob(
        "gen_next", "  Next >  ",
        "import GenAI.nuke_node as N; N.gen_step(nuke.thisNode(), 1)")
    next_btn.clearFlag(nuke.STARTLINE)  # gleiche Zeile wie Prev
    group.addKnob(next_btn)

    extract_btn = nuke.PyScript_Knob(
        "extract", "  Extract to Read  ",
        "import GenAI.nuke_node as N; N.extract(nuke.thisNode())")
    extract_btn.setTooltip("Bake the shown generation into a standalone Read node in the comp.")
    group.addKnob(extract_btn)

    restore_btn = nuke.PyScript_Knob(
        "restore_settings", "  Restore Settings  ",
        "import GenAI.nuke_node as N; N.restore_settings(nuke.thisNode())")
    restore_btn.setTooltip(
        "Load the shown generation's model, mode, prompt and parameters back into the "
        "controls above. Does NOT re-run — press Generate afterwards to reproduce it.")
    group.addKnob(restore_btn)

    hist_store = nuke.String_Knob("history", "history", "")
    hist_store.setVisible(False)  # interner JSON-Speicher der Generierungen
    group.addKnob(hist_store)

    xcnt = nuke.Int_Knob("extract_count", "extract_count")
    xcnt.setVisible(False)  # monotoner Zähler für eindeutige Extract-Namen
    group.addKnob(xcnt)


# ---- Provider (fal / Magnific / ...) --------------------------------------------
#
# Manche Modelle sind bei mehreren Anbietern verfuegbar (z. B. Seedance 2 als
# "seedance-2.0" bei fal UND als "seedance-2-pro" bei Magnific) -- mit jeweils
# eigenem Endpoint, eigenen Modes/Aufloesungen/Rechten. Statt getrennter Model-
# Eintraege traegt so ein Manifest-Eintrag ein "providers"-Array; der Provider-
# Knob waehlt daraus, _effective_model() liefert das gemergte, "flache" Dict, das
# der Rest des Codes (Mode-/Parameter-Knobs, resolve_endpoint, Backend-Dispatch)
# genau wie einen normalen (Provider-losen) Modell-Eintrag behandelt.

_PROVIDER_DISPLAY = {
    "fal": "fal.ai",
    "magnific": "Magnific",
    "openai": "OpenAI",
    "google": "Google",
    "local": "Local",
    "magnific_mcp": "Magnific MCP",
    "ltx": "LTX",
}


def _provider_display(name_or_backend):
    """Menschlicher Servicename fuer Anzeige, z. B. 'fal' -> 'fal.ai'."""
    return _PROVIDER_DISPLAY.get(name_or_backend, (name_or_backend or "?").capitalize())


def _provider_label(v):
    # Gleicher Trick wie _res_label: Nuke fixiert die Pulldown-Breite beim ersten
    # Befuellen; ohne Padding wird z. B. "Magnific" sonst abgeschnitten.
    return v.center(10)


def _provider_names(model):
    """Verfuegbare Providernamen (Key hinterlegt) fuer ein Modell; [] ohne Provider-Array."""
    providers = model.get("providers")
    if not providers:
        return []
    return [p["name"] for p in providers if config.has_backend_key(p["backend"])]


def _provider_options(model):
    """
    (Anzeige-Name, roher Providername) je Wahlmoeglichkeit im Provider-Knob:
    - Modelle mit "providers": ein Eintrag pro verfuegbarem Provider (waehlbar).
    - Modelle ohne "providers": EIN synthetischer Eintrag aus model["backend"],
      informativ (zeigt den genutzten Dienst) statt versteckt zu werden.
    """
    providers = model.get("providers")
    if providers:
        return [(_provider_display(p["name"]), p["name"])
                for p in providers if config.has_backend_key(p["backend"])]
    return [(_provider_display(model.get("backend", "")), None)]


def _model_available(model):
    """True, wenn das Modell direkt (backend-Key) oder ueber mind. einen Provider nutzbar ist."""
    if model.get("providers"):
        return bool(_provider_names(model))
    return config.has_backend_key(model.get("backend", ""))


def _select_provider(node, model, prefer=None):
    """
    Provider-Knob fuer 'model' aufbauen: IMMER sichtbar (zeigt den genutzten Dienst
    auch dann, wenn es nur einen gibt -- z. B. "OpenAI" bei GPT). Bevorzugt 'prefer'
    (roher Providername, z. B. beim Restore), sonst den bisherigen Wert falls noch
    gueltig, sonst den ersten verfuegbaren/synthetischen Eintrag.
    Gibt den rohen Providernamen zurueck (None bei Modellen ohne Provider-Array).
    """
    pk = node.knob("provider")
    options = _provider_options(model)
    if not pk or not options:
        return options[0][1] if options else None
    displays = [_provider_label(d) for d, _ in options]
    try:
        previous = pk.value()  # vor setValues lesen -- danach steht der Knob wieder auf dem ersten Eintrag
        pk.setValues(displays)
        target = None
        if prefer:
            match = next((d for d, n in options if n == prefer), None)
            target = _provider_label(match) if match else None
        if not target:
            target = previous if previous in displays else None
        pk.setValue(target or displays[0])
        pk.setVisible(True)
    except Exception:
        pass
    sel = (pk.value() or "").strip()
    match = next((n for d, n in options if d == sel), None)
    return match


def _effective_model(model, node):
    """
    Gemergtes 'flaches' Modell-Dict fuer die aktuell gewaehlte Provider-Variante.
    Ohne "providers"-Array wird 'model' unveraendert zurueckgegeben (bestehende
    Modelle laufen exakt wie vorher). Provider-Felder ueberschreiben gleichnamige
    Modell-Felder (backend/endpoint/modes/resolutions/legal/cost/...).
    """
    providers = model.get("providers")
    if not providers:
        return model
    pk = node.knob("provider")
    sel = (pk.value() or "").strip() if pk else ""
    name = next((p["name"] for p in providers if _provider_display(p["name"]) == sel), None)
    prov = next((p for p in providers if p["name"] == name), None) or providers[0]
    eff = dict(model)
    eff.update(prov)
    eff.pop("providers", None)
    return eff


def _apply_effective(node, model, keep_mode=False):
    """Mode-/Parameter-Knobs + Rights-Ampel auf die AKTUELLE Provider-Auswahl anwenden
    (rebaut den Provider-Knob selbst nicht -- fuer Mode-/Provider-Wechsel ohne Modellwechsel).
    keep_mode: den gewaehlten Mode beibehalten, wenn der neue Provider ihn ebenfalls kennt."""
    eff = _effective_model(model, node)
    _apply_model_params(node, eff, keep_mode=keep_mode)
    if node.knob("legal"):
        try:
            node["legal"].setValue(LEGAL_TEXT.get(eff.get("legal"), ""))
        except Exception:
            pass
    return eff


def _apply_model(node, model, prefer_provider=None):
    """Kompletter Modellwechsel: Provider-Knob neu aufbauen, dann _apply_effective."""
    _select_provider(node, model, prefer=prefer_provider)
    return _apply_effective(node, model)


def _apply_model_params(node, model, keep_mode=False):
    """
    Mode-Dropdown + Parameter-Knobs aufs Modell einstellen (beim Modellwechsel).
    Setzt das Mode-Dropdown auf den Default-Mode (mit keep_mode: auf den bisher
    gewaehlten, sofern das neue Modell/der neue Provider ihn kennt), dann die Param-Knobs.
    'model' ist hier immer bereits das "flache" (Provider-aufgeloeste) Dict.
    """
    mode_knob = node.knob("mode")
    modes = model.get("modes") or []
    if mode_knob:
        if len(modes) > 1:
            try:
                labels = [m["label"] for m in modes]
                previous = (mode_knob.value() or "").strip() if keep_mode else None
                mode_knob.setValues(labels)
                mode_knob.setValue(previous if previous in labels
                                   else model.get("default_mode", modes[0]["label"]))
            except Exception:
                pass
            mode_knob.setVisible(True)
        else:
            mode_knob.setVisible(False)
    _apply_mode_params(node, model)


def _apply_mode_params(node, model):
    """
    Parameter-Knobs (aspect/resolution/duration/quality) auf das aktuelle Mode/Modell
    einstellen. Werte kommen bevorzugt aus dem aktuellen Mode (z. B. Kling: Start/End
    kann 4K, Reference-Video nicht). Ändert das Mode-Dropdown selbst NICHT — daher
    auch beim Mode-Wechsel aufrufbar, ohne den Mode zurückzusetzen.
    """
    _update_mode_hint(node, model)
    specs = [
        ("aspect_ratio", "aspect_ratios", "default_aspect_ratio"),
        ("resolution", "resolutions", "default_resolution"),
        ("duration", "durations", "default_duration"),
        ("quality", "qualities", "default_quality"),
    ]
    for knob_name, values_key, default_key in specs:
        knob = node.knob(knob_name)
        if not knob:
            continue
        values = _mode_get(model, node, values_key)
        if values:
            try:
                is_res = (knob_name == "resolution")
                knob.setValues([_res_label(v) for v in values] if is_res else list(values))
                default = _mode_get(model, node, default_key, values[0])
                if knob_name == "duration":
                    # Standard = so lang wie der Shot (Frame-Range/fps), gerundet.
                    picked = _pick_duration(values, _shot_seconds())
                    if picked is not None:
                        default = picked
                knob.setValue(_res_label(default) if is_res else default)
            except Exception:
                pass
            knob.setVisible(True)
        else:
            knob.setVisible(False)
    _update_cost(node, model)


def _shot_seconds():
    """Länge des aktuellen Shots in Sekunden aus Frame-Range/fps."""
    try:
        r = nuke.root()
        first = int(r["first_frame"].value())
        last = int(r["last_frame"].value())
        fps = float(r["fps"].value()) or 24.0
        return max(1.0, (last - first + 1) / fps)
    except Exception:
        return None


def _pick_duration(values, shot_sec):
    """
    Aus den erlaubten Duration-Werten (Strings, evtl. inkl. 'auto') den kleinsten
    numerischen Wert >= Shot-Länge wählen (deckt den Shot ab); sonst den längsten.
    """
    if shot_sec is None:
        return None
    nums = []
    for v in values:
        try:
            nums.append((int(v), v))
        except (TypeError, ValueError):
            pass  # 'auto' u. Ä. überspringen
    if not nums:
        return None
    nums.sort()
    for n, v in nums:
        if n >= shot_sec - 0.001:
            return v
    return nums[-1][1]


def _current_mode(model, node):
    """Aktuell gewählten Mode-Eintrag (dict) liefern, sonst None."""
    modes = model.get("modes") or []
    if not modes:
        return None
    label = node["mode"].value() if node.knob("mode") else None
    return next((m for m in modes if m["label"] == label), modes[0])


def _mode_get(model, node, key, default=None):
    """Wert aus dem aktuell gewählten Mode (falls dort gesetzt), sonst aus dem Modell.
    So können Modes z. B. Auflösung/Dauer/Status-Pfad/Flags eigenständig überschreiben."""
    mode = _current_mode(model, node)
    if isinstance(mode, dict) and key in mode:
        return mode[key]
    return model.get(key, default)


def _update_mode_hint(node, model):
    """Kurzen Hinweistext zum aktuellen Mode unter dem Dropdown anzeigen."""
    hint_knob = node.knob("mode_hint")
    if not hint_knob:
        return
    mode = _current_mode(model, node)
    text = (mode.get("hint") if mode else None) or model.get("hint", "")
    if text:
        hint_knob.setValue(text)
        hint_knob.setVisible(True)
    else:
        hint_knob.setValue("")
        hint_knob.setVisible(False)


CR_TO_EUR = 0.0005  # grobe Umrechnung Magnific-Credits -> Euro (plan-abhängig)

# Ziel-Formate für Modelle, die Eingänge aufs Aspect beschneiden müssen (Kling Omni
# lehnt extreme Seitenverhältnisse ab). ~2 MP, unter Klings 2.086-MP-Limit.
_ASPECT_FORMAT = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1440, 1440),
                   "21:9": (2520, 1080), "4:3": (1600, 1200), "3:4": (1200, 1600)}

def _res_label(v):
    # Nuke schätzt die Pulldown-Breite zu knapp und schneidet z. B. "1080p" auf
    # "108" ab. Den Wert mit Leerzeichen auf feste Breite auffüllen zwingt den
    # Pulldown breit genug; zentriert wirkt es ausgewogen (statt links "verloren").
    # Nuke schneidet rechts ab -> Breite knapp halten, damit "1080p" nicht clippt.
    # Beim Lesen: strip() entfernt die Füll-Leerzeichen (Werte haben keine internen).
    return v.center(9)


def _estimate_cost(model, node):
    """Kurzer Kosten-Schätztext für die aktuellen Einstellungen (Credits bzw. USD)."""
    cost = _mode_get(model, node, "cost")
    if not cost:
        return ""

    def _res():
        k = node.knob("resolution")
        return k.value().strip() if k and k.visible() else None

    unit = cost.get("unit")
    if unit == "cr":
        if "per_image" in cost:
            table = cost["per_image"]
            cr = table.get(_res()) or min(table.values())
            return "≈ {c} cr  (≈ €{e:.2f})".format(c=cr, e=cr * CR_TO_EUR)
        if "per_sec" in cost:
            table = cost["per_sec"]
            ps = table.get(_res()) or max(table.values())
            dk = node.knob("duration")
            try:
                d = int(dk.value()) if dk and dk.visible() else 5
            except (TypeError, ValueError):
                d = 5
            cr = ps * d
            return "≈ {c} cr  (≈ €{e:.2f})  — {d}s".format(
                c=cr, e=cr * CR_TO_EUR, d=d)
    if unit == "usd":
        return cost.get("note", "")
    if unit == "ltx":
        return _estimate_ltx(cost, node)
    return ""


def _estimate_ltx(cost, node):
    """LTX: Preis pro Sekunde Input-Video (Alpha Gen nach kurzer Seite gestaffelt)."""
    try:
        r = nuke.root()
        secs = (int(r["last_frame"].value()) - int(r["first_frame"].value()) + 1) / (float(r["fps"].value()) or 24.0)
        if cost.get("kind") == "alpha":
            src = node.input(0)
            if src is None:
                return "connect in1 for a price estimate  (USD 0.10-1.00 per second of input)"
            fmt = src.format()
            short = min(fmt.width(), fmt.height())
            rate, tier = (0.10, "1080") if short <= 1080 else ((0.25, "1440") if short <= 1440 else (1.00, "2160"))
        else:
            rate, tier = float(cost.get("rate", 0)), cost.get("tier", "")
        return "≈ ${c:.2f}  ({s:.1f}s × ${r:.2f}/s{t}, LTX credits)".format(
            c=secs * rate, s=secs, r=rate, t=(", " + tier + " tier") if tier else "")
    except Exception:
        return ""


def _update_cost(node, model):
    """Kosten-Knob auf die aktuelle Schätzung setzen (leer -> ausblenden)."""
    kb = node.knob("cost_est")
    if not kb:
        return
    txt = _estimate_cost(model, node)
    try:
        kb.setValue(txt)
        kb.setVisible(bool(txt))
    except Exception:
        pass


def resolve_endpoint(model, node, has_inputs):
    """
    (endpoint, input_map) für den aktuellen Zustand bestimmen.
    Mit Modes: der gewählte Mode -- falls der Mode selbst keinen eigenen Endpoint
    traegt (z. B. Magnific Seedance 2/2.5 Pro: der Endpoint haengt dort nur von der
    Aufloesung ab, nicht vom Mode -- resolution_endpoints im Backend loest das
    spaeter auf), auf den Modell-Endpoint zurueckfallen statt KeyError zu werfen.
    Ohne Modes: mit Input edit_endpoint, sonst endpoint.
    """
    mode = _current_mode(model, node)
    if mode is not None:
        return mode.get("endpoint") or model.get("endpoint"), mode.get("input_map", [])
    if has_inputs and model.get("edit_endpoint"):
        return model["edit_endpoint"], model.get("input_map", [])
    return model["endpoint"], (model.get("input_map", []) if has_inputs else [])


# ---- Generierung ---------------------------------------------------------------

def _clamp_video_range_or_abort(node, model_label, vfirst, natural_vlast, fps, min_sec, max_sec):
    """
    Verfuegbare Referenz-Video-Laenge (vfirst..natural_vlast bei fps) gegen die
    Modell-Grenzen [min_sec, max_sec] pruefen (max_sec=None = kein Maximum).

    Zu kurz -> Statusmeldung setzen und None liefern (Aufrufer bricht dann ab, statt
    der API eine laengere/kuerzere Referenz vorzutaeuschen als tatsaechlich
    hochgeladen wird -- das liess Jobs sonst stillschweigend fehlschlagen).
    Zu lang -> auf max_sec kappen (ab vfirst), kein Fehler.
    Sonst -> die Range unveraendert liefern.

    Gibt (vfirst, vlast) zurueck, oder None bei Abbruch (Status ist dann gesetzt).
    """
    natural_secs = (natural_vlast - vfirst + 1) / fps
    if natural_secs < min_sec - 0.001:
        _set_status(node, (
            "Reference video too short: only {have:.2f}s available in the "
            "comp/shot frame range, but {label} needs at least {need}s. "
            "Extend the frame range or connect longer footage.".format(
                have=natural_secs, need=min_sec, label=model_label)
        ))
        return None
    if max_sec is not None and natural_secs > max_sec:
        clamped_vlast = vfirst + int(round(max_sec * fps)) - 1
        return (vfirst, min(clamped_vlast, natural_vlast))
    return (vfirst, natural_vlast)


def _mode_out_of_sync(eff, node):
    """True, wenn der Mode-Knob einen Wert hat, den das aktuelle Modell/der Provider nicht
    kennt (Knobs nach Modell-/Provider-Wechsel nicht nachgezogen). _current_mode wuerde dann
    still auf den ersten Mode zurueckfallen."""
    modes = eff.get("modes") or []
    mk = node.knob("mode")
    return bool(len(modes) > 1 and mk and (mk.value() or "").strip() not in [m["label"] for m in modes])


def _unused_inputs(connected, input_map):
    """1-basierte Slots der angeschlossenen Eingaenge, wenn KEIN einziger davon im aktuellen
    Mode verwendet wird (dann gehen alle verloren); sonst []."""
    used = [i for i in connected if i < len(input_map) and input_map[i] and (input_map[i].get("field") or input_map[i].get("role"))]
    return [] if used else [i + 1 for i in connected]


def _resolve_genai_dirs(node):
    """(root, in_dir, outputs_dir) fuer diese Node. Ausgabe-Ort: Node-out_dir (fest) >
    output_rel (relativ zum Script, aus dem Installer) > output_dir (fester Default) >
    automatisch aus dem Shot."""
    base = node["out_dir"].value() if node.knob("out_dir") else None
    base = base.strip() if base else ""
    output_rel = None
    if not base:
        output_rel = config.get_value("output_rel")
        if not output_rel:
            base = config.get_value("output_dir") or ""
    return render.genai_dirs(base=(base or None), output_rel=output_rel)


def _export_inputs(node, eff):
    """
    Generate Only: die angeschlossenen Eingaenge OHNE API-Aufruf in einen eigenen Ordner
    <GenAI>/UPLOAD/<Datum_Uhrzeit>_<Node-Name>/ schreiben (alle Eingaenge einer Runde
    zusammen), damit man sie woanders hochladen kann. Viewer-Look eingebacken, volle
    Aufloesung, nur RGB -- exakt das, was sonst an die API ginge. Im Ordner liegen nur
    die fertigen Dateien (die einzelnen Video-Frames werden nach dem Encoden geloescht).
      Mode "Video"       -> in1.mp4 ueber die Comp-Range (Comp-fps, frame-genau) + in2..in4.png
                            vom aktuellen Frame (in1 ist immer das Video, der Rest Standbilder)
      Mode "Still frame" -> in1..in4.png vom aktuellen Frame
    """
    mode = _current_mode(eff, node) or {}
    kind = mode.get("export", "still")
    connected = [i for i in range(node.inputs()) if node.input(i) is not None]
    if not connected:
        _set_status(node, "Connect at least one input (in1..in4) to export.")
        return

    _set_status(node, "rendering inputs ...")
    root, in_dir, _out_dir = _resolve_genai_dirs(node)
    r = nuke.root()
    fps = float(r["fps"].value()) or 24.0
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    node_name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in node.name())
    folder = os.path.join(in_dir, "{s}_{n}".format(s=stamp, n=node_name))
    k = 1
    while os.path.exists(folder):   # zweiter Export derselben Node in derselben Sekunde
        k += 1
        folder = os.path.join(in_dir, "{s}_{n}_{k}".format(s=stamp, n=node_name, k=k))
    os.makedirs(folder)

    if kind == "video":
        # Konvention: in1 ist das Video, in2..in4 sind immer Standbilder (aktueller Frame).
        video_idx = set(i for i in connected if i == 0)
        tmp_dir = os.path.join(folder, "_frames")
        try:
            inputs = render.render_inputs(node, tmp_dir, video_indices=video_idx,
                                          plain_names=True, still_dir=folder)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        jobs = [(i, inputs[i]) for i in sorted(inputs) if i in video_idx]
        n_stills = len(inputs) - len(jobs)
        if not jobs:   # in1 nicht angeschlossen -> nur Standbilder
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _export_done(node, folder, 0, n_stills)
            return
        _set_status(node, "encoding in1 video ...")
        worker = threading.Thread(target=_export_video_worker,
                                  args=(node, jobs, folder, tmp_dir, fps, n_stills))
        worker.daemon = True
        worker.start()
    else:
        inputs = render.render_inputs(node, folder, plain_names=True)
        _export_done(node, folder, 0, len(inputs))


def _export_video_worker(node, jobs, folder, tmp_dir, fps, n_stills=0):
    """Hintergrund: je Eingang PNG-Sequenz -> in<N>.mp4 (lokal); Temp-Frames werden immer geloescht."""
    try:
        done = 0
        for i, val in jobs:
            out_mp4 = os.path.join(folder, "in{n}.mp4".format(n=i + 1))
            video.encode_local(val["seq_dir"], fps, out_mp4)
            done += 1
        nuke.executeInMainThread(_export_done, args=(node, folder, done, n_stills))
    except Exception as exc:
        nuke.executeInMainThread(_set_status, args=(node, "Error: {e}".format(e=exc)))
        nuke.tprint(traceback.format_exc())
    finally:
        # Einzelframes brauchen wir nicht mehr (nur den von uns angelegten Temp-Ordner).
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _export_done(node, folder, n_videos, n_stills):
    """Status + 'Last Result' auf den Export-Ordner setzen."""
    posix = folder.replace("\\", "/")
    try:
        node["last_output"].setValue(posix)
    except Exception:
        pass
    parts = []
    if n_videos:
        parts.append("in1 video")
    if n_stills:
        parts.append("{n} still{s}".format(n=n_stills, s="" if n_stills == 1 else "s"))
    _set_status(node, "exported {w} -> {p}".format(w=" + ".join(parts) or "nothing", p=posix))


def generate(node):
    """Generate-Button: Eingänge rendern (Main-Thread), dann Job im Hintergrund."""
    try:
        model = _node_model(node)
        eff = _effective_model(model, node)
        if not config.has_backend_key(eff["backend"]):
            if eff["backend"] == config.MCP_BACKEND:
                _set_status(node, "Not logged in to Magnific MCP — log in in the GenAI setup window, then press Generate again.")
            else:
                _set_status(node, ("Missing {k} — enter it in the GenAI setup window (or switch the dropdown "
                                   "to MCP and log in with Magnific).").format(
                    k=config.backend_key_name(eff["backend"]) or eff["backend"]))
            _open_setup()
            return
        if _mode_out_of_sync(eff, node):
            _apply_effective(node, model)
            _set_status(node, (
                "Mode/parameters did not match {label} and were refreshed -- "
                "check the Mode, then press Generate again.".format(label=model["label"])))
            return
        if eff.get("local_export"):
            # Generate Only: kein API-Aufruf, nur die Eingaenge in einen Ordner schreiben.
            _export_inputs(node, eff)
            return
        prompt_text = node["prompt"].value() or ""

        connected = [i for i in range(node.inputs()) if node.input(i) is not None]
        # 1-basierte Slot-Nummern der belegten Eingänge (mit Lücken, z. B. in1+in3 ->
        # [1, 3]) — so ist @inN fest an den physischen Slot N gekoppelt.
        connected_slots = [i + 1 for i in connected]
        errors = prompt_mod.validate(prompt_text, connected_slots)
        if errors:
            summary = prompt_mod.summarize_refs(prompt_text, connected_slots)
            msg = "Prompt error: " + "; ".join(errors)
            if summary:
                msg += "  |  " + summary
            _set_status(node, msg)
            return
        if not prompt_text.strip() and not connected:
            _set_status(node, "Enter a prompt or connect an input.")
            return
        if eff.get("requires_input") and not connected:
            _set_status(node, "{label} requires a connected input image.".format(label=model["label"]))
            return

        # Rechts-Warnung bei rot geflaggten Modellen (z. B. Kling: behält Trainingsrecht).
        if eff.get("legal") == "red":
            warn = ("⚠ {label} is rights-flagged RED (e.g. keeps training rights "
                    "on your input/output).\nGenerate anyway?".format(label=model["label"]))
            if not nuke.ask(warn):
                _set_status(node, "cancelled (rights-flagged model)")
                return

        # Endpoint + Input-Mapping aus dem aktuellen Mode (oder Fallback) auflösen.
        endpoint, input_map = resolve_endpoint(eff, node, bool(connected))
        # Nutzt der Mode keinen der angeschlossenen Eingaenge (z. B. Text -> Image mit
        # Referenzen), gehen sie still verloren -- vorher nachfragen statt Credits zu verbrennen.
        unused = _unused_inputs(connected, input_map)
        if unused:
            _mode = _current_mode(eff, node)
            ask = ("The current mode{m} does not use the connected input(s) {ins} -- "
                   "they will NOT be sent to the model.\nGenerate anyway?".format(
                       m=(" '" + _mode["label"] + "'") if _mode else "",
                       ins=", ".join("in" + str(s) for s in unused)))
            if not nuke.ask(ask):
                _set_status(node, "cancelled (inputs not used by the selected mode)")
                return
        # Welche angeschlossenen Eingänge sind Guide-Videos (input_map ... video:true)?
        video_indices = set(
            i for i in connected
            if i < len(input_map) and input_map[i] and input_map[i].get("video")
        )

        _set_status(node, "rendering inputs ...")
        # Ausgabe-Ort: Node-out_dir (fest) > output_rel (relativ zum Script, aus dem
        # Installer) > output_dir (fester Default) > automatisch aus dem Shot.
        root, in_dir, outputs_dir = _resolve_genai_dirs(node)

        # Manche Modelle (Kling Omni) verlangen ein Referenz-Video passend zur
        # gewählten Dauer (max 10s) statt der vollen Comp-Range.
        video_range = None
        force_duration = None
        pad_secs = None
        if _mode_get(eff, node, "ref_video_matches_duration") and node.knob("duration"):
            try:
                dur = int((node["duration"].value() or "5").strip())
            except (TypeError, ValueError):
                dur = 5
            r = nuke.root()
            vf = int(r["first_frame"].value())
            vfps = float(r["fps"].value()) or 24.0
            vlast_capped = min(vf + int(round(dur * vfps)) - 1, int(r["last_frame"].value()))

            # Kleinste vom Modell erlaubte Dauer (statt hart 3 zu erzwingen) -- z. B.
            # Kling Reference-Video braucht mindestens 3s. Ist die Comp/Shot-Range
            # kuerzer als dieses Minimum, gibt es schlicht nicht genug Referenz-
            # Material: abbrechen statt der API eine laengere duration vorzutaeuschen
            # als das tatsaechlich hochgeladene Video hat (das liess den Job zuvor
            # mit "Task FAILED" ohne Begruendung scheitern -- die API validiert das).
            allowed = _mode_get(eff, node, "durations") or []
            try:
                min_dur = min(int(v) for v in allowed if str(v).isdigit())
            except ValueError:
                min_dur = 3
            clamped = _clamp_video_range_or_abort(
                node, model["label"], vf, vlast_capped, vfps, min_dur, None)
            if clamped is None:
                return
            video_range = clamped
            ref_secs = (video_range[1] - video_range[0] + 1) / vfps
            # Referenz-Video und Output-Dauer müssen übereinstimmen (Kling maxDelta):
            # ist die Comp kürzer als die gewählte Dauer, Dauer entsprechend kappen
            # (nur nach unten -- niemals ueber die tatsächlich verfügbare Länge hoch).
            force_duration = str(min(dur, int(round(ref_secs))))

        # Seedance / Gemini Omni: Referenzvideo braucht KEINE harte Kopplung an die
        # gewaehlte Output-Dauer (anders als Kling maxDelta oben; Seedance gleicht die
        # Dauer weiter unten ueber duration_follows_ref_video an) -- nur die
        # eigenen Min/Max-Grenzen der jeweiligen API (z. B. Seedance 2-15s, Gemini
        # Omni Reference max 3s). Volle Comp/Shot-Range vermessen, bei Bedarf aufs
        # Maximum kappen; zu kurz -> abbrechen statt einen ungueltigen Call zu senden.
        video_ref_bounds = _mode_get(eff, node, "video_ref_duration_bounds")
        if video_ref_bounds and video_range is None:
            r = nuke.root()
            vf = int(r["first_frame"].value())
            vlast_full = int(r["last_frame"].value())
            vfps = float(r["fps"].value()) or 24.0
            min_sec, max_sec = video_ref_bounds
            clamped = _clamp_video_range_or_abort(
                node, model["label"], vf, vlast_full, vfps, min_sec, max_sec)
            if clamped is None:
                return
            video_range = clamped

        # Seedance: Output-Dauer folgt der Laenge des Referenzvideos (sonst erfindet das
        # Modell z. B. bei 5,2s Referenz und 10s Output die zweite Haelfte dazu und
        # streckt die Bewegung). Kleinste erlaubte Dauer >= Referenzlaenge waehlen (damit
        # kein Frame verloren geht) und die Referenz per Standbild darauf auffuellen. Nur
        # wenn die Referenz laenger als die groesste erlaubte Dauer ist, wird sie gekuerzt.
        # Test-Schalter: "ref_pad": "off" in GenAI_keys.json sendet die Referenz ohne Auffuellen.
        if _mode_get(eff, node, "duration_follows_ref_video") and node.knob("duration"):
            if video_range is None:   # Modell ohne eigene Grenzen: volle Comp-Range
                video_range = (int(nuke.root()["first_frame"].value()),
                               int(nuke.root()["last_frame"].value()))
            vfps_ref = float(nuke.root()["fps"].value()) or 24.0
            ref_secs = (video_range[1] - video_range[0] + 1) / vfps_ref
            allowed = [int(v) for v in (_mode_get(eff, node, "durations") or []) if str(v).isdigit()]
            if allowed:
                fitting = [d for d in allowed if d >= ref_secs - 0.001]
                chosen = min(fitting) if fitting else max(allowed)
                if chosen < ref_secs - 0.001:
                    video_range = (video_range[0],
                                   min(video_range[1], video_range[0] + int(round(chosen * vfps_ref)) - 1))
                force_duration = str(chosen)
                pad_off = str(config.get_value("ref_pad", "on")).strip().lower() in ("off", "0", "false", "no")
                if chosen > ref_secs + 0.001 and not pad_off:
                    pad_secs = chosen   # Referenz wird per Standbild auf diese Laenge gehalten

        # Kling Omni: Eingänge aufs Ziel-Seitenverhältnis beschneiden (extreme
        # Aspects werden abgelehnt) — Format aus dem aspect_ratio-Knob.
        input_format = None
        if _mode_get(eff, node, "crop_inputs_to_aspect") and node.knob("aspect_ratio"):
            ar = node["aspect_ratio"].value() or "16:9"
            input_format = _ASPECT_FORMAT.get(ar, (1920, 1080))

        inputs = render.render_inputs(node, in_dir, video_indices=video_indices,
                                      video_range=video_range,
                                      max_pixels=_mode_get(eff, node, "max_input_pixels"),
                                      video_max_pixels=_mode_get(eff, node, "video_max_pixels"),
                                      video_max_long=_mode_get(eff, node, "video_max_long"),
                                      video_max_short=_mode_get(eff, node, "video_max_short"),
                                      input_format=input_format)
        if pad_secs:
            for _v in inputs.values():
                if isinstance(_v, dict) and _v.get("seq_dir"):
                    _v["pad_secs"] = pad_secs

        # UI-Parameter einsammeln (nur sichtbare Knobs des aktuellen Modells).
        params = {}
        for pk in ("aspect_ratio", "resolution", "duration", "quality"):
            knob = node.knob(pk)
            if knob and knob.visible():
                val = knob.value()
                if pk == "resolution":
                    val = val.strip()  # Füll-Leerzeichen weg -> exakter API-Wert
                params[pk] = val
        if force_duration is not None:
            params["duration"] = force_duration  # an die Referenz-Video-Länge angeglichen

        api_prompt = prompt_mod.resolve_for_api(prompt_text.strip(), video_indices=video_indices)

        # Aufgelösten Endpoint/Input-Map in eine Modell-Kopie legen (Backend nutzt sie direkt).
        active = dict(eff)
        active["endpoint"] = endpoint
        active["input_map"] = input_map
        active["edit_endpoint"] = None
        # Mode-spezifische Felder in die Modell-Kopie ziehen (Status-Pfad, guide_fps).
        active["status_endpoint"] = _mode_get(eff, node, "status_endpoint")
        active["resolution_endpoints"] = _mode_get(eff, node, "resolution_endpoints")
        active["guide_fps"] = _mode_get(eff, node, "guide_fps", 24)
        # Konstante API-Felder (z. B. generate_audio=false); Mode darf das Modell überschreiben.
        active["api_static"] = _mode_get(eff, node, "api_static")
        # Mode kann eigene api_params vorgeben (z. B. Edit-Endpoint ohne aspect/duration).
        _mode = _current_mode(eff, node)
        if _mode is not None and "api_params" in _mode:
            active["api_params"] = _mode["api_params"]
        # Magnific MCP: Tool + Modell-Slug koennen pro Mode wechseln (z. B. Kling 4 Standard
        # fuer Start/End-Frames, Omni fuer Referenzen; Video Modify: ein Mode je Modell).
        for _k in ("mcp_tool", "mcp_slug"):
            _v = _mode_get(eff, node, _k)
            if _v:
                active[_k] = _v

        # Einstellungs-Snapshot für die History (Restore Settings): roher Prompt inkl.
        # @refs, Modell/Mode/Provider-Label und die tatsächlich gesendeten Parameter.
        # "model" bleibt der urspruengliche (Provider-lose) Label, damit restore_settings
        # den Manifest-Eintrag wiederfindet -- der Provider wird separat gespeichert.
        mode_label = None
        _mk = node.knob("mode")
        if _mk and _mk.visible():
            mode_label = _mk.value()
        settings = {
            "model": model["label"],
            "interface": _node_interface(node),   # Labels sind nur je Interface eindeutig
            # Roher Providername (aus dem Provider-Dict in der manifest, nicht der
            # Anzeigetext) -- nur bei echten Provider-Modellen gesetzt, damit
            # restore_settings ueber _apply_model(prefer_provider=...) exakt
            # denselben Provider wiederfindet.
            "provider": (eff.get("name") if model.get("providers") else None),
            "mode": mode_label,
            "prompt": prompt_text,
            "params": dict(params),
        }

        _set_status(node, "sending to {label} ...".format(label=model["label"]))
        try:
            comp_fps = float(nuke.root()["fps"].value()) or None
        except Exception:
            comp_fps = None
        worker = threading.Thread(
            target=_worker,
            args=(node, active, api_prompt, inputs, params, outputs_dir, settings, comp_fps),
        )
        worker.daemon = True
        worker.start()
    except Exception as exc:
        _set_status(node, "Error: {e}".format(e=exc))
        nuke.tprint(traceback.format_exc())


def _write_log(outputs_dir, model, prompt, params, input_summary, output_path):
    """
    Eine Zeile pro Generierung in <shot>/GenAI/generation_log.txt schreiben:
    Zeit, Modell, Parameter, hochgeladene Inputs, Output-Dateiname, Prompt.
    """
    try:
        genai_dir = os.path.dirname(outputs_dir)  # <shot>/GenAI  (OUT liegt darunter)
        log_path = os.path.join(genai_dir, "generation_log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [ts, "model=" + str(model.get("label", "?"))]
        if params:
            parts.append("params=" + ",".join("{k}={v}".format(k=k, v=v) for k, v in params.items()))
        parts.append("inputs=[" + ", ".join(input_summary) + "]")
        parts.append("output=" + os.path.basename(output_path))
        parts.append('prompt="' + (prompt or "").replace("\n", " ").strip() + '"')
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(" | ".join(parts) + "\n")
    except Exception:
        nuke.tprint(traceback.format_exc())


def _fps_differs(a, b):
    """True, wenn zwei Framerates merklich verschieden sind (23.976 ~ 24)."""
    try:
        return abs(float(a) / float(b) - 1.0) > 0.005
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _fps_text(v):
    return ("%g" % round(float(v), 3))


def _worker(node, model, prompt_text, inputs, params, outputs_dir, settings=None, comp_fps=None):
    """Hintergrund: (Guide-Videos hochladen) -> submit -> poll -> download."""
    try:
        # Input-Beschreibung fürs Log festhalten (vor der Video->URL-Umwandlung).
        input_summary = []
        for i in sorted(inputs.keys()):
            v = inputs[i]
            if isinstance(v, dict):
                input_summary.append("in{i}=guide-video".format(i=i))
            else:
                input_summary.append("in{i}={f}".format(i=i, f=os.path.basename(v)))

        # Guide-Video-Eingänge (Sequenz-dicts) zu mp4 encoden + zu fal hochladen,
        # dann durch die resultierende URL ersetzen.
        for idx in list(inputs.keys()):
            val = inputs[idx]
            if isinstance(val, dict) and val.get("seq_dir"):
                # Guide-Video mit der vom Modell erwarteten fps senden (Seedance/Gemini
                # = 24). Die gerenderten Frames liegen auf dem Comp-Raster; weicht die
                # Comp-fps ab (z. B. 25/30), wird zeitrichtig umgetaktet, damit das
                # Referenzvideo in Echtzeit statt zu langsam/schnell ankommt.
                guide_fps = model.get("guide_fps", 24)
                src_fps = val.get("fps")
                if guide_fps == "comp":      # Modify-Modelle: Laenge/fps kommen aus dem Footage
                    guide_fps = src_fps or comp_fps or 24
                status = "uploading guide video ..."
                if _fps_differs(src_fps, guide_fps):
                    status = "uploading guide video (retiming {a} -> {b} fps) ...".format(
                        a=_fps_text(src_fps), b=_fps_text(guide_fps))
                pad_secs = val.get("pad_secs")
                if pad_secs:
                    status = "uploading guide video (last frame held to {n} frames) ...".format(
                        n=int(round(pad_secs * guide_fps)))
                nuke.executeInMainThread(_set_status, args=(node, status))
                if model.get("backend") in (config.MCP_BACKEND, "ltx"):
                    # MCP/LTX: lokal zu mp4 encoden; das Backend laedt es selbst hoch
                    # (kein fal-Upload, kein FAL_KEY noetig).
                    inputs[idx] = video.encode_local(
                        val["seq_dir"], guide_fps, val["seq_dir"].rstrip("/\\") + ".mp4",
                        src_fps=src_fps,
                        pad_frames=(int(round(pad_secs * guide_fps)) if pad_secs else None),
                    )
                    continue
                inputs[idx] = video.encode_and_upload(
                    val["seq_dir"], val["first"], val["last"], guide_fps, src_fps=src_fps,
                    pad_frames=(int(round(pad_secs * guide_fps)) if pad_secs else None),
                )

        backend = get_backend(model["backend"])
        job_id = backend.submit(model, prompt_text, inputs, params)

        start = time.time()
        eta = model.get("eta_sec", 30)
        poll_fails = 0
        while True:
            try:
                st = backend.poll(job_id)
                poll_fails = 0
            except Exception as exc:
                # Der Job laeuft beim Anbieter weiter, auch wenn eine einzelne Statusabfrage
                # an einem 502/Timeout scheitert -- dann nicht abbrechen, sondern erneut fragen.
                if not (getattr(exc, "transient", False) or isinstance(exc, OSError)):
                    raise
                poll_fails += 1
                if poll_fails > MAX_POLL_FAILS:
                    raise RuntimeError(
                        "Provider kept failing while checking the job -- it may still have finished "
                        "on the provider's side (job {j}).  ({e})".format(j=job_id, e=exc))
                nuke.executeInMainThread(_set_status, args=(node, (
                    "provider hiccup while checking the job, retrying ({n}/{m}) ...".format(
                        n=poll_fails, m=MAX_POLL_FAILS))))
                time.sleep(min(2 * poll_fails, 15))
                continue
            elapsed = int(time.time() - start)
            if st.status == DONE:
                ext = model.get("output_ext", "png")
                is_video = model.get("type") == "video"
                filename = "gen_{t}.{ext}".format(t=int(time.time()), ext=ext)
                path = backend.download(st.result_url, outputs_dir, filename=filename)
                _write_log(outputs_dir, model, prompt_text, params, input_summary, path)
                extra_note = None
                saved = []
                for _label, _url in (getattr(st, "extra_urls", None) or {}).items():
                    try:
                        _ext = os.path.splitext(_url.split("?")[0])[1] or ".bin"
                        backend.download(_url, outputs_dir, filename="{b}_{l}{e}".format(
                            b=os.path.splitext(filename)[0], l=_label, e=_ext))
                        saved.append("{b}_{l}{e}".format(b=os.path.splitext(filename)[0], l=_label, e=_ext))
                    except Exception as _exc:
                        nuke.tprint("extra download failed ({l}): {e}".format(l=_label, e=_exc))
                if saved:
                    extra_note = "also saved: " + ", ".join(saved)
                if is_video:
                    # Nukes Reader indiziert fal-mp4s falsch -> in PNG-Sequenz wandeln.
                    nuke.executeInMainThread(_set_status, args=(node, "transcoding video ..."))
                    seq_dir = os.path.splitext(path)[0] + "_frames"
                    # Ergebnis auf die Comp-fps legen (zeitrichtig), sonst laeuft ein
                    # 24-fps-Clip in einer 25/30-fps-Comp zu schnell.
                    pattern, first, last, _fps, native_fps = video.mp4_to_sequence(
                        path, seq_dir, target_fps=comp_fps)
                    note = None
                    if _fps_differs(native_fps, _fps):
                        note = "retimed {a} -> {b} fps".format(
                            a=_fps_text(native_fps), b=_fps_text(_fps))
                    if extra_note:
                        note = (note + "; " if note else "") + extra_note
                    nuke.executeInMainThread(_on_done, args=(node, pattern, True, first, last, model.get("label"), settings, note))
                else:
                    nuke.executeInMainThread(_on_done, args=(node, path, False, None, None, model.get("label"), settings))
                return
            if st.status == ERROR:
                nuke.executeInMainThread(_set_status, args=(node, "Error: {e}".format(e=st.error)))
                if _is_setup_problem(st.error):
                    nuke.executeInMainThread(_open_setup)
                return
            remaining = eta - elapsed
            if remaining > 0:
                msg = "{s} ... {el}s (~{r}s left)".format(s=st.status, el=elapsed, r=remaining)
            else:
                msg = "{s} ... {el}s (finishing up ...)".format(s=st.status, el=elapsed)
            nuke.executeInMainThread(_set_status, args=(node, msg))
            time.sleep(2)
    except Exception as exc:
        nuke.executeInMainThread(_set_status, args=(node, "Error: {e}".format(e=_explain_error(exc))))
        if _is_setup_problem(exc):
            nuke.executeInMainThread(_open_setup)
        nuke.tprint(traceback.format_exc())


MAX_HISTORY = 30
MAX_POLL_FAILS = 20   # aufeinanderfolgende fehlgeschlagene Statusabfragen (~4 min), dann Abbruch
_SUPPRESS_CB = {"on": False}   # unterdrückt gen_select-knobChanged bei programmatischem setValue


def _gen_status(prefix, entry):
    """Status-Text mit angehängtem Modell (falls in der History-Entry gespeichert)."""
    m = (entry or {}).get("model")
    return prefix + ("  ·  " + m if m else "")


def _on_done(node, path, is_video=False, first=None, last=None, model_label=None, settings=None, note=None):
    """Main-Thread: neue Generierung in die History aufnehmen und anzeigen."""
    entry = _history_add(node, path, bool(is_video), first, last, model_label, settings)
    _show_generation(node, entry)
    try:
        if node.knob("last_output"):
            node["last_output"].setValue(entry["path"])
    except Exception:
        pass
    # No automatic viewer switch: viewer operations over the MCP bridge froze the
    # GUI. The Read is wired up; just view the node.
    _set_status(node, _gen_status("done: " + os.path.basename(path), entry) + ("  ·  " + note if note else ""))


def _show_generation(node, entry):
    """Die gegebene Generierung als Read (+ OCIODisplay-invert) in die Node laden.

    Nativ geladen — kein Reformat aufs Plate (der User reformatiert bei Bedarf selbst).
    """
    result_name = node.name() + "_result"
    ocio_name = node.name() + "_toACES"
    reformat_name = node.name() + "_fit"
    posix = entry["path"].replace("\\", "/")
    is_video = entry.get("is_video")

    node.begin()
    try:
        for old in (result_name, ocio_name, reformat_name):
            existing = nuke.toNode(old)
            if existing is not None:
                nuke.delete(existing)
        read = _make_result_read(result_name, posix, is_video, entry.get("first"), entry.get("last"))
        tail = render.make_ocio_display(read, invert=True)
        tail.setName(ocio_name)
        for out in nuke.allNodes("Output"):
            out.setInput(0, tail)
    finally:
        node.end()


def _make_result_read(name, posix, is_video, first, last):
    """Read anlegen (Raw-Colorspace, Video: Range + an Comp-Anfang; Bild: hold)."""
    read = nuke.nodes.Read()
    read.setName(name)
    read["file"].setValue(posix)
    read["colorspace"].setValue(render.RAW_COLORSPACE)  # Raw: Werte wie in der Datei
    try:
        # Generierte Bilder/Videos haben keinen Alpha-Kanal -- ohne "auto alpha" liefert
        # Nuke dann Alpha 0 (schwarz) statt 1. Der Knob heisst auto_alpha.
        read["auto_alpha"].setValue(True)
    except Exception:
        nuke.tprint(traceback.format_exc())
    if is_video:
        try:
            if first is not None:
                read["first"].setValue(int(first))
            if last is not None:
                read["last"].setValue(int(last))
        except Exception:
            pass
        _offset_to_comp(read)
    else:
        _hold_over_range(read)
    try:
        read["reload"].execute()
    except Exception:
        pass
    return read


# ---- History ------------------------------------------------------------------

def _history_load(node):
    try:
        stored = node["history"].value() if node.knob("history") else ""
        if not stored:
            return []
        # Neu: base64 (siehe _history_save). Fallback für evtl. alten roh-JSON.
        try:
            raw = base64.b64decode(stored.encode("ascii")).decode("utf-8")
        except Exception:
            raw = stored
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _history_save(node, hist):
    try:
        # WICHTIG: base64 statt roher JSON. Nukes String_Knob wertet eckige Klammern
        # [...] als TCL-Ausdruck aus -> roher JSON (beginnt mit "[") wird beim Zurück-
        # lesen zu "Syntax error ..." zerstört und die History geht verloren. base64
        # (A-Za-z0-9+/=) enthält keine TCL-Sonderzeichen und überlebt unverändert.
        enc = base64.b64encode(json.dumps(hist).encode("utf-8")).decode("ascii")
        node["history"].setValue(enc)
    except Exception:
        pass


def _rebuild_gen_select(node, hist, index):
    knob = node.knob("gen_select")
    if not knob:
        return
    labels = [e.get("label", str(i + 1)) for i, e in enumerate(hist)] or [" "]
    _SUPPRESS_CB["on"] = True
    try:
        knob.setValues(labels)
        if 0 <= index < len(labels):
            knob.setValue(labels[index])
    except Exception:
        pass
    finally:
        _SUPPRESS_CB["on"] = False


def _current_index(node, hist):
    knob = node.knob("gen_select")
    if not knob or not hist:
        return len(hist) - 1
    try:
        return max(0, min(int(knob.getValue()), len(hist) - 1))
    except Exception:
        return len(hist) - 1


def _history_add(node, path, is_video, first, last, model_label=None, settings=None):
    hist = _history_load(node)
    num = (hist[-1].get("num", len(hist)) + 1) if hist else 1
    label = "#{n}  {t}".format(n=num, t=datetime.datetime.now().strftime("%H:%M:%S"))
    entry = {"path": path.replace("\\", "/"), "is_video": bool(is_video),
             "first": first, "last": last, "num": num, "label": label,
             "model": model_label, "settings": settings}
    hist.append(entry)
    hist = hist[-MAX_HISTORY:]
    _history_save(node, hist)
    _rebuild_gen_select(node, hist, len(hist) - 1)
    return entry


def gen_step(node, delta):
    """Prev/Next: eine Generierung in der History weiterschalten und anzeigen."""
    hist = _history_load(node)
    if not hist:
        _set_status(node, "no generations yet")
        return
    i = max(0, min(_current_index(node, hist) + delta, len(hist) - 1))
    knob = node.knob("gen_select")
    if knob:
        _SUPPRESS_CB["on"] = True
        try:
            knob.setValue(hist[i]["label"])
        except Exception:
            pass
        finally:
            _SUPPRESS_CB["on"] = False
    _show_generation(node, hist[i])
    _set_status(node, _gen_status("showing " + hist[i]["label"], hist[i]))


def extract(node):
    """Aktuell gezeigte Generierung als eigenständigen Read (+OCIODisplay) im Comp."""
    hist = _history_load(node)
    if not hist:
        _set_status(node, "nothing to extract")
        return
    entry = hist[_current_index(node, hist)]
    base = node.name() + "_extract"
    nx, ny = int(node.xpos()), int(node.ypos())

    # Eindeutige Nummer über einen monotonen Zähler-Knob (toNode ist im
    # Button-/Group-Kontext unzuverlässig und findet Root-Nodes nicht).
    cnt_knob = node.knob("extract_count")
    k = (int(cnt_knob.value()) + 1) if cnt_knob else 1
    if cnt_knob:
        cnt_knob.setValue(k)
    read_name = "{b}{i}".format(b=base, i=k)

    # WICHTIG: Der Button läuft im Group-Kontext -> ohne `with nuke.root()` landen
    # die Nodes INNERHALB der Group (unsichtbar im Comp).
    with nuke.root():
        read = _make_result_read(read_name, entry["path"].replace("\\", "/"),
                                 entry.get("is_video"), entry.get("first"), entry.get("last"))
        ocd = render.make_ocio_display(read, invert=True)
        ocd.setName(read_name + "_toACES")
        try:
            read["tile_color"].setValue(0x228822ff)  # grün, damit gut sichtbar
            read.setXYpos(nx + 200, ny + 120)
            # Ein Read mit Postage-Stamp ist ~92 px hoch -> der OCIODisplay muss darunter
            # statt im Read haengen (feste 70 px Abstand ueberlappten ihn).
            read_h = int(read.screenHeight() or 0) or 92
            ocd.setXYpos(nx + 200, ny + 120 + read_h + 25)
        except Exception:
            pass
    _set_status(node, "extracted {lbl} -> {name}".format(lbl=entry["label"], name=read.fullName()))


def _set_enum(node, knob_name, value):
    """
    Enumeration/Text-Knob auf 'value' setzen, tolerant gegenüber den Auffüll-
    Leerzeichen der Resolution (Werte im Dropdown sind zentriert, z. B. "  1080p ").
    Findet die Option, deren getrimmte Form == value ist, und setzt deren Rohtext.
    """
    knob = node.knob(knob_name)
    if not knob or value is None:
        return
    target = str(value).strip()
    try:
        options = list(knob.values())
    except Exception:
        options = None
    try:
        if options:
            for opt in options:
                raw = opt.split("\t")[0]
                if raw.strip() == target:
                    knob.setValue(raw)
                    return
        knob.setValue(value)
    except Exception:
        pass


def restore_settings(node):
    """
    History-Button: Modell, Mode, Prompt und Parameter der aktuell gezeigten
    Generierung zurück in die Bedienelemente laden (führt NICHT neu aus).

    Reihenfolge ist entscheidend: erst Modell (baut Mode-/Parameter-Dropdowns neu
    auf), dann Mode (setzt die Options-Listen des Modes), dann die gespeicherten
    Parameter (überschreiben die Defaults), zuletzt der Prompt. Die Rebuilds werden
    explizit aufgerufen, statt sich auf Callback-Nebenwirkungen zu verlassen.
    """
    hist = _history_load(node)
    if not hist:
        _set_status(node, "no generations yet")
        return
    entry = hist[_current_index(node, hist)]
    settings = entry.get("settings")
    if not settings:
        _set_status(node, "this generation has no saved settings (older entry) — nothing to restore")
        return

    saved_iface = settings.get("interface") if settings.get("interface") in INTERFACES else None
    model = _find_model(settings.get("model"), saved_iface)
    if model is None:
        _set_status(node, "cannot restore: model '{m}' is no longer in the manifest".format(
            m=settings.get("model")))
        return

    # Verfuegbarkeit pruefen -- bei Provider-Modellen reicht ein verfuegbarer Provider;
    # den gespeicherten bevorzugen, sonst auf den ersten verfuegbaren zurueckfallen
    # (aeltere History-Eintraege ohne "provider"-Feld landen ebenfalls hier).
    provider_name = settings.get("provider")
    if model.get("providers"):
        avail_names = _provider_names(model)
        if not avail_names:
            _set_status(node, ("cannot restore: model '{m}' is unavailable (missing API key / login — " + _SETUP_HINT + ")").format(
                m=settings.get("model")))
            return
        if provider_name not in avail_names:
            provider_name = avail_names[0]
    elif not config.has_backend_key(model.get("backend", "")):
        _set_status(node, ("cannot restore: model '{m}' is unavailable (missing API key / login — " + _SETUP_HINT + ")").format(
            m=settings.get("model")))
        return

    # Interface (API/MCP) passend zum Modell stellen, damit es im Model-Dropdown vorkommt.
    iface = _interface_of(model)
    if iface and node.knob("interface") is not None and _node_interface(node) != iface:
        _SUPPRESS_CB["all"] = True
        try:
            node["interface"].setValue(iface)
            node["model"].setValues(_model_labels(iface))
        finally:
            _SUPPRESS_CB["all"] = False

    # Modell + Provider setzen, Mode-/Parameter-Dropdowns auf diese Kombination aufbauen.
    _set_enum(node, "model", model["label"])
    eff = _apply_model(node, model, prefer_provider=provider_name)

    # Gespeicherten Mode wählen und dessen Parameter-Optionslisten aufbauen.
    if settings.get("mode") and node.knob("mode"):
        _set_enum(node, "mode", settings["mode"])
    _apply_mode_params(node, eff)

    # Gespeicherte Parameterwerte über die Mode-Defaults legen.
    for pk, val in (settings.get("params") or {}).items():
        _set_enum(node, pk, val)

    # Prompt zuletzt (kein Callback, keine Nebenwirkung).
    if node.knob("prompt") is not None:
        try:
            node["prompt"].setValue(settings.get("prompt", ""))
        except Exception:
            pass

    _update_cost(node, eff)
    _set_status(node, "restored settings from {lbl} — press Generate to reproduce".format(
        lbl=entry.get("label", "generation")))


def _reformat_to(read, plate_fmt, name, mode="fill"):
    """
    Reformat hinter den Read hängen, das auf das Plate-Format zurückrechnet.
    mode: fill (füllt, winziger Beschnitt) | fit (Letterbox, kein Beschnitt) |
          distort (exakt füllend, leichter Stretch).
    """
    resize = {"fill": "fill", "fit": "fit", "distort": "distort"}.get(mode, "fill")
    rf = nuke.nodes.Reformat(inputs=[read])
    rf.setName(name)
    try:
        rf["type"].setValue("to box")
        rf["box_width"].setValue(plate_fmt.width())
        rf["box_height"].setValue(plate_fmt.height())
        rf["box_fixed"].setValue(True)
        rf["box_pixel_aspect"].setValue(plate_fmt.pixelAspect())
        rf["resize"].setValue(resize)
        rf["black_outside"].setValue(False)
        rf["center"].setValue(True)
    except Exception:
        nuke.tprint(traceback.format_exc())
    return rf


def _hold_over_range(read):
    """Standbild an jedem Frame zeigen (before/after = hold)."""
    for kn, val in (("before", "hold"), ("after", "hold")):
        try:
            read[kn].setValue(val)
        except Exception:
            pass


def _offset_to_comp(read):
    """
    Video so verschieben, dass sein erstes Bild am Comp-Anfang liegt — sonst läge
    es (Range 1..N) außerhalb der Comp-Range und der Viewer zeigt schwarz.
    """
    try:
        first = int(nuke.root()["first_frame"].value())
        read["frame_mode"].setValue("start at")
        read["frame"].setValue(str(first))
    except Exception:
        nuke.tprint(traceback.format_exc())


def _show_in_viewer(node):
    """Aktiven Viewer auf die Node schalten, damit das Ergebnis sofort sichtbar ist."""
    try:
        v = nuke.activeViewer()
        if v is not None:
            v.node().setInput(0, node)
        else:
            nuke.connectViewer(0, node)
    except Exception:
        pass


def _set_status(node, text):
    try:
        node["status"].setValue(text)
    except Exception:
        pass


# ---- Rechts-Ampel live aktualisieren, wenn das Modell gewechselt wird ----------

def _on_knob_changed():
    try:
        if _SUPPRESS_CB.get("all"):     # waehrend der Migration alter Nodes
            return
        node = nuke.thisNode()
        kn = nuke.thisKnob().name()
        if kn == "interface" and node.knob("model"):
            _apply_interface(node, node["interface"].value())
        elif kn == "model" and node.knob("legal"):
            # Modellwechsel: Provider-Knob neu aufbauen (Default-Provider), dann
            # Mode-/Parameter-Knobs + Rights-Ampel auf die resultierende Provider-
            # Variante anwenden.
            model = _node_model(node)
            _apply_model(node, model)
        elif kn == "provider":
            # Provider gewechselt (gleiches Modell): Mode-/Parameter-Knobs + Rights-
            # Ampel neu aufbauen, ohne die Provider-Auswahl selbst zu ueberschreiben.
            # Den gewaehlten Mode behalten (z. B. Reference), statt auf den Default
            # des neuen Providers zurueckzuspringen.
            _apply_effective(node, _node_model(node), keep_mode=True)
        elif kn == "mode":
            # Mode-Wechsel: Hint + Param-Knobs (Auflösung/Dauer je Mode) neu setzen,
            # ohne den Mode selbst zurückzusetzen.
            model = _node_model(node)
            _apply_mode_params(node, _effective_model(model, node))
        elif kn in ("resolution", "duration") and node.knob("cost_est"):
            model = _node_model(node)
            _update_cost(node, _effective_model(model, node))
        elif kn == "gen_select" and not _SUPPRESS_CB["on"] and node.knob("history"):
            hist = _history_load(node)
            if hist:
                entry = hist[_current_index(node, hist)]
                _show_generation(node, entry)
                _set_status(node, _gen_status("showing " + entry.get("label", ""), entry))
    except Exception:
        pass


# Reihenfolge der UI-Knobs einer GenAI-Node (fuer die Migration alter Nodes ohne Interface-Knob)
_UI_KNOB_NAMES = ["ai_tab", "model", "interface", "legal", "mode", "mode_hint", "div_prompt", "prompt", "aspect_ratio",
                  "resolution", "duration", "quality", "cost_est", "out_dir", "div_go", "provider", "generate",
                  "status", "last_output", "div_hist", "gen_select", "gen_prev", "gen_next", "extract",
                  "restore_settings", "history", "extract_count"]
_STATE_KNOBS = ["mode", "prompt", "aspect_ratio", "resolution", "duration", "quality", "out_dir",
                "status", "last_output", "history", "extract_count"]


def _needs_migration(node):
    """True bei alten Nodes: ohne Interface-Knob bzw. mit dem frueheren Layout (Interface ueber Model, mit Label)."""
    if node.knob("model") is None or node.knob("generate") is None:
        return False
    ik = node.knob("interface")
    if ik is None:
        return True
    try:
        if (ik.label() or "").strip():            # altes Layout: Dropdown mit Label "Interface"
            return True
        if ik.getFlag(nuke.STARTLINE):            # neues Layout: Interface in der Zeile von Model
            return True
        names = list(node.knobs().keys())
        return names.index("interface") < names.index("model")   # altes Layout: ueber Model
    except Exception:
        return False


def _migrate_node(node):
    """
    Alte GenAI-Node auf das aktuelle Layout bringen (Interface-Dropdown rechts neben Model, neue
    Modell-Labels): die UI-Knobs werden entfernt und neu angelegt; alle Werte (Prompt, Mode,
    Parameter, History, ...) bleiben erhalten. True, wenn migriert wurde.
    """
    if not _needs_migration(node):
        return False
    state = {}
    for k in ["model", "provider", "interface"] + _STATE_KNOBS:
        kn = node.knob(k)
        if kn is not None:
            try:
                state[k] = kn.value()
            except Exception:
                pass
    try:
        hist = _history_load(node)
        hist_index = _current_index(node, hist) if hist else 0
    except Exception:
        hist, hist_index = [], 0
    old_iface = (state.get("interface") or "").strip()
    model = _find_model(state.get("model"), old_iface if old_iface in INTERFACES else None) or load_models()[0]
    iface = old_iface if old_iface in INTERFACES else (_interface_of(model) or "API")
    model = _model_by_label(model["label"], iface)
    prov_raw = None
    prov_disp = (state.get("provider") or "").strip()
    for d, n in _provider_options(model):
        if d.strip() == prov_disp:
            prov_raw = n

    _SUPPRESS_CB["all"] = True
    try:
        for name in _UI_KNOB_NAMES:
            if name == "ai_tab":
                continue            # der Tab bleibt stehen, die Knobs dahinter werden neu aufgebaut
            kn = node.knob(name)
            if kn is not None:
                node.removeKnob(kn)
        labels = _model_labels(iface)
        _add_ui_knobs(node, labels, iface, add_tab=node.knob("ai_tab") is None)
        node["model"].setValue(model["label"] if model["label"] in labels else labels[0])
        eff = _apply_model(node, model, prefer_provider=prov_raw)
        if state.get("mode") and node.knob("mode"):
            _set_enum(node, "mode", state["mode"])
        _apply_mode_params(node, eff)
        for k in ("aspect_ratio", "resolution", "duration", "quality"):
            if state.get(k) is not None:
                _set_enum(node, k, state[k])
        for k in ("prompt", "out_dir", "status", "last_output", "history", "extract_count"):
            if state.get(k) is not None and node.knob(k) is not None:
                try:
                    node[k].setValue(state[k])
                except Exception:
                    pass
        if hist:
            _SUPPRESS_CB["on"] = True
            try:
                _rebuild_gen_select(node, hist, hist_index)
            finally:
                _SUPPRESS_CB["on"] = False
        _update_cost(node, eff)
    finally:
        _SUPPRESS_CB["all"] = False
    return True


def refresh_all_nodes():
    """
    Alle GenAI-Nodes im Script auf den aktuellen Stand bringen: alte Nodes ohne Interface-
    Dropdown werden migriert, und das Model-Dropdown jeder Node zeigt die aktuelle Modellliste
    ihres Interfaces (das Dropdown wird beim Anlegen festgeschrieben -- neue Manifest-Eintraege
    oder ein frischer MCP-Login erscheinen sonst nur in neu angelegten Nodes). Der gewaehlte
    Eintrag bleibt.
    """
    for n in nuke.allNodes("Group", recurseGroups=True):
        try:
            if not (n.knob("model") and n.knob("generate")):
                continue
            if _migrate_node(n):
                continue
            iface = _node_interface(n)
            labels = _model_labels(iface)
            if n["model"].values() != labels:
                cur = n["model"].value()
                found = _find_model(cur, iface)          # alte Labels -> neues Label
                cur = found["label"] if found else cur
                # Frisch angelegte Node (noch ohne Login: nur "Generate Only"): nach dem Login auf das
                # erste echte Modell stellen, solange sie unbenutzt ist (kein Prompt, keine History).
                if (found and found.get("backend") == config.LOCAL_BACKEND and _has_real_models(iface)
                        and not (n["prompt"].value() or "").strip() and not (n["history"].value() or "").strip()):
                    cur = next((l for l in labels if l != cur), cur)
                _SUPPRESS_CB["all"] = True
                try:
                    shown = n["model"].value()
                    n["model"].setValues(labels)
                    n["model"].setValue(cur if cur in labels else labels[0])
                    if n["model"].value() != (_find_model(shown, iface) or {}).get("label", shown):
                        _apply_model(n, _model_by_label(n["model"].value(), iface))   # anderes Modell: Mode/Parameter neu
                finally:
                    _SUPPRESS_CB["all"] = False
        except Exception:
            nuke.tprint(traceback.format_exc())


def install_callbacks():
    """Einmal registrieren: Ampel folgt der Modell-Auswahl; alte Nodes bekommen neue Modelle."""
    nuke.addKnobChanged(_on_knob_changed, nodeClass="Group")
    try:
        nuke.addOnScriptLoad(refresh_all_nodes)
    except Exception:
        pass
