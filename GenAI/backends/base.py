"""
base.py — gemeinsame Schnittstelle aller Backends.

Jedes Backend implementiert dieselben drei Schritte, damit die Node modellagnostisch
bleibt:

    submit(model_cfg, prompt, input_paths, params) -> job_id
    poll(job_id)                                   -> Status(status, progress, result_url, error)
    download(result_url, ziel_ordner)              -> lokaler_pfad

submit/poll/download laufen später im Hintergrund-Thread der Node; hier ist bewusst
keine Nuke-/UI-Abhängigkeit drin, damit die Backends pur und testbar bleiben.
"""

import json
import os
import re
from urllib.request import urlopen, Request


# Backend-Status-Konstanten
PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"


class Status(object):
    """Ergebnis eines poll()-Aufrufs."""

    def __init__(self, status, progress=None, result_url=None, error=None):
        self.status = status          # einer von PENDING/RUNNING/DONE/ERROR
        self.progress = progress      # 0.0..1.0 oder None, wenn Anbieter nichts liefert
        self.result_url = result_url  # gesetzt, wenn status == DONE
        self.error = error            # Fehlertext, wenn status == ERROR
        self.extra_urls = {}          # weitere Ergebnisdateien {label: url} (z. B. LTX cutout)

    @property
    def is_final(self):
        return self.status in (DONE, ERROR)

    def __repr__(self):
        return "Status({s!r}, progress={p}, result_url={u!r})".format(
            s=self.status, p=self.progress, u=self.result_url
        )


class Backend(object):
    """Basisklasse für alle Anbieter-Adapter."""

    name = "base"

    def submit(self, model_cfg, prompt, input_paths, params):
        """
        Job an den Anbieter schicken. Liefert eine job_id (String), mit der poll()
        später den Fortschritt abfragt.

        model_cfg    : dict aus manifest.json (das ausgewählte Modell)
        prompt       : Prompt-Text (bereits mit aufgelösten @refs, falls nötig)
        input_paths  : Liste lokaler Datei-/URL-Pfade der Eingänge (Reihenfolge = @ref1..N)
        params       : dict der UI-Parameter (aspect_ratio, resolution, ...)
        """
        raise NotImplementedError

    def poll(self, job_id):
        """Aktuellen Status eines Jobs abfragen. Liefert Status()."""
        raise NotImplementedError

    def download(self, result_url, target_dir, filename=None):
        """
        Ergebnis von result_url in target_dir speichern. Liefert den lokalen Pfad.
        Standard-Implementierung reicht für die meisten Anbieter (direkte URL).
        """
        _ensure_dir(target_dir)
        if not filename:
            filename = os.path.basename(result_url.split("?")[0]) or "result.out"
        target_path = os.path.join(target_dir, filename)
        req = Request(result_url, headers={"User-Agent": "nuke-ai-gen/0.1"})
        with urlopen(req) as resp, open(target_path, "wb") as out:
            out.write(resp.read())
        return target_path


class BackendError(RuntimeError):
    """Fehler bei der Anbieter-Kommunikation (HTTP, Auth, ungültige Antwort).

    transient=True: vermutlich vorübergehend (HTTP 5xx/429, Netzwerk) -- beim Abfragen
    eines laufenden Jobs lohnt dann ein erneuter Versuch statt Abbruch."""

    def __init__(self, message="", transient=False):
        super().__init__(message)
        self.transient = transient


def is_transient_http(code):
    """Server-/Gateway-Fehler und Rate-Limits sind meist nur kurz da."""
    return code == 429 or (code is not None and code >= 500)


# ---- Modell-/Content-Ablehnung erkennen -----------------------------------------
#
# Anbieter melden eine Ablehnung durch das Modell selbst (Content-/Safety-Filter,
# oder schlicht "konnte damit nichts generieren") uneinheitlich: fal gibt oft einen
# type/msg wie "no_media_generated" oder "Could not generate ... with the given
# inputs", Magnific liefert dagegen meist nur {"status":"FAILED","error":null} ganz
# ohne Begruendung. Diese Heuristik erkennt bekannte Formulierungen, damit der User
# statt eines rohen HTTP-/JSON-Dumps eine verstaendliche Meldung sieht.

_DENIAL_SIGNALS = (
    "no_media_generated",
    "unsafe content",
    "safety",
    "could not generate a video with the given inputs",
    "could not generate an image with the given inputs",
    "content policy",
    "content_policy",
    "moderation",
    "flagged",
)


def looks_like_prompt_denied(text):
    """
    Grobe Heuristik: deutet der Fehlertext auf eine Modell-/Content-Ablehnung hin
    (Prompt/Input vom Modell selbst verweigert) statt auf einen technischen Fehler
    (Auth, Schema, Netzwerk)? Nur ein Hinweis -- Anbieter kennzeichnen das selten
    eindeutig.
    """
    if not text:
        return False
    low = str(text).lower()
    return any(sig in low for sig in _DENIAL_SIGNALS)


def prompt_denied_message(provider_label, hint=None):
    """
    Einheitliche, verstaendliche Meldung fuer eine (vermutete) Modell-Ablehnung.
    'hint' ist optionaler Zusatztext (z. B. wenn der Anbieter selbst keinen Grund
    nennt -- Magnific liefert oft nur FAILED ohne jede Begruendung).
    """
    msg = (
        "Prompt denied by {p}: the model refused this request -- most likely its "
        "own content/safety filter. Try rephrasing the prompt (avoid direct "
        "instructions like 'remove person'), or a different model.".format(p=provider_label)
    )
    if hint:
        msg += " ({h})".format(h=hint)
    return msg


# ---- Einheitliches Fehlerformat --------------------------------------------------
#
# Konvention im ganzen Node: die fuer den User verstaendliche Nachricht steht IMMER
# zuerst (landet direkt hinter "Error:" im Status-Feld -- ohne durch eine URL/rohes
# JSON scrollen zu muessen). Technischer Kontext (Provider, HTTP-Code, Antwort-URL,
# rohe Response) folgt danach in Klammern, fuers Debuggen bei Bedarf noch da.

def extract_error_detail(raw):
    """
    Aus einer rohen Fehlerantwort (JSON-Text oder bereits geparst) die eigentliche,
    fuer Menschen verstaendliche Meldung herausziehen. Deckt die bekannten Formen ab:
      fal:      {"detail": [{"msg": "..."}]}  oder  {"detail": "..."}
      Magnific: {"message": "..."}
      OpenAI:   {"error": {"message": "..."}}  oder  {"error": "..."}
    Liefert None, wenn keine der bekannten Formen passt (Aufrufer faellt dann auf
    den rohen Text zurueck).
    """
    obj = raw
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return None
    if not isinstance(obj, dict):
        return None
    detail = obj.get("detail")
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict) and first.get("msg"):
            return str(first["msg"])
    if isinstance(detail, str) and detail:
        return detail
    if obj.get("message"):
        return str(obj["message"])
    err = obj.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(err, str) and err:
        return err
    return None


def format_error(message, provider=None, technical=None):
    """
    Fertige Anzeige-Nachricht bauen: 'message' zuerst, Provider/technischer Kontext
    danach in Klammern (nur die vorhandenen Teile, durch ": " getrennt).
    """
    tail = ": ".join(p for p in (provider, technical) if p)
    return "{msg}  ({tail})".format(msg=message, tail=tail) if tail else message


def _html_error_summary(raw):
    """Faellt ein Gateway/CDN aus, kommt HTML statt JSON zurueck. Dann den Seitentitel als
    Meldung nehmen (statt den rohen HTML-Quelltext anzuzeigen); None, wenn es kein HTML ist."""
    text = str(raw or "")
    if not text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = " ".join(m.group(1).split()) if m else ""
    return title or "the service returned an error page instead of an answer"


def http_error_message(provider, raw_body, code=None, url=None):
    """
    Einheitliche Meldung fuer eine HTTP-Fehlerantwort: zuerst pruefen, ob es nach
    einer Modell-/Content-Ablehnung aussieht (looks_like_prompt_denied) -- dann die
    entsprechend erklaerende Meldung; sonst die aus dem JSON extrahierte Nachricht
    (extract_error_detail), sonst der rohe Text gekuerzt. Immer mit format_error()
    einheitlich formatiert (Nachricht zuerst, Technik danach in Klammern).
    """
    html = _html_error_summary(raw_body)
    if html is None and looks_like_prompt_denied(raw_body):
        return prompt_denied_message(provider)
    msg = html or extract_error_detail(raw_body) or (str(raw_body)[:300] if raw_body else "unknown error")
    technical = "HTTP {c}".format(c=code) if code is not None else None
    if url:
        technical = "{t} at {u}".format(t=technical, u=url) if technical else "at {u}".format(u=url)
    return format_error(msg, provider, technical)


def _ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


# ---- Registry: id (aus manifest "backend") -> Backend-Instanz -------------------

_REGISTRY = {}


def get_backend(name):
    """
    Backend-Instanz nach Name aus dem Manifest ("fal", "openai", "magnific", "magnific_mcp", ...).
    Import erfolgt lazy, damit ein fehlendes Backend nicht die ganze Node blockiert.
    """
    if name in _REGISTRY:
        return _REGISTRY[name]

    if name == "fal":
        from .fal import FalBackend
        instance = FalBackend()
    elif name == "openai":
        from .openai import OpenAIBackend
        instance = OpenAIBackend()
    elif name == "magnific":
        from .magnific import MagnificBackend
        instance = MagnificBackend()
    elif name == "google":
        from .google import GoogleBackend
        instance = GoogleBackend()
    elif name == "ltx":
        from .ltx import LtxBackend
        instance = LtxBackend()
    elif name == "magnific_mcp":
        from .magnific_mcp import MagnificMcpBackend
        instance = MagnificMcpBackend()
    else:
        raise BackendError("Unbekanntes Backend: {n!r}".format(n=name))

    _REGISTRY[name] = instance
    return instance
