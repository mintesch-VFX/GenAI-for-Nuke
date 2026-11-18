"""
config.py — API-Keys laden, ohne sie je im Script/Gizmo zu speichern.

Lesereihenfolge (erste Fundstelle gewinnt):
  1) Umgebungsvariable (z. B. FAL_KEY, OPENAI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS)
  2) User-Config-Datei ~/.nuke/GenAI_keys.json  (einfachste Variante für Weitergabe)

So kann jeder Empfänger der Node seinen eigenen Key auf eigene Rechnung nutzen.
"""

import json
import os


# Wo die optionale Key-Datei liegt: ~/.nuke/GenAI_keys.json
KEYS_FILENAME = "GenAI_keys.json"
# Frueherer Name der Key-Datei; wird beim ersten Zugriff automatisch umbenannt,
# damit bestehende Installationen ihre Keys nicht verlieren.
LEGACY_KEYS_FILENAME = "ai_gen_keys.json"


def _nuke_home():
    """Pfad zum .nuke-Verzeichnis des Users (plattformunabhängig)."""
    return os.path.join(os.path.expanduser("~"), ".nuke")


def keys_file_path():
    """Vollständiger Pfad zur optionalen Key-Datei."""
    return os.path.join(_nuke_home(), KEYS_FILENAME)


def _legacy_keys_file_path():
    return os.path.join(_nuke_home(), LEGACY_KEYS_FILENAME)


def _migrate_legacy_keys():
    """
    Alte ai_gen_keys.json (falls vorhanden und neue Datei fehlt noch) einmalig auf
    den neuen Namen umbenennen. Silent — wenn's schiefgeht, hat der User immer noch
    seine alte Datei und kann Keys via Installer neu setzen.
    """
    new_path = keys_file_path()
    old_path = _legacy_keys_file_path()
    if os.path.isfile(old_path) and not os.path.exists(new_path):
        try:
            os.rename(old_path, new_path)
        except OSError:
            pass


def _load_keys_file():
    """Liest ~/.nuke/GenAI_keys.json. Fehlt sie oder ist kaputt -> leeres Dict."""
    _migrate_legacy_keys()
    path = keys_file_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def get_key(name, required=False):
    """
    Liefert den Key `name` (z. B. "FAL_KEY").

    Reihenfolge: Env-Variable > Key-Datei. Nicht gefunden -> None,
    außer required=True, dann MissingKeyError mit klarer Anleitung.
    """
    value = os.environ.get(name)
    if not value:
        value = _load_keys_file().get(name)

    if not value and required:
        raise MissingKeyError(name)
    return value or None


def has_key(name):
    """True, wenn der Key irgendwo hinterlegt ist."""
    return get_key(name) is not None


# ---- Backend -> benötigter Key -------------------------------------------------

BACKEND_KEYS = {
    "fal": "FAL_KEY",
    "openai": "OPENAI_API_KEY",
    "magnific": "MAGNIFIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "ltx": "LTX_API_KEY",
}


# Pseudo-Backend ohne API-Aufruf (Generate Only: Eingaenge nur lokal exportieren).
LOCAL_BACKEND = "local"
# Magnific ueber MCP: Anmeldung per OAuth statt API-Key (siehe mcp_auth.py).
MCP_BACKEND = "magnific_mcp"


def backend_key_name(backend):
    """Name des API-Keys, den ein Backend braucht (oder None)."""
    return BACKEND_KEYS.get(backend)


def has_backend_key(backend):
    """True, wenn der zum Backend gehörende Key hinterlegt ist.
    Das lokale Pseudo-Backend ("local", z. B. Generate Only) braucht keinen Key."""
    if backend == LOCAL_BACKEND:
        return True
    if backend == MCP_BACKEND:          # kein Key, sondern OAuth-Login (mcp_auth)
        from . import mcp_auth
        return mcp_auth.is_logged_in()
    name = BACKEND_KEYS.get(backend)
    return bool(name) and has_key(name)


def has_any_api_key():
    """True, wenn mindestens ein API-Key (fal, OpenAI, Magnific, Google, LTX) hinterlegt ist."""
    return any(has_key(n) for n in BACKEND_KEYS.values())


def is_configured():
    """True, wenn mindestens ein Anbieter nutzbar ist: Magnific-Login (MCP) oder irgendein API-Key."""
    return has_backend_key(MCP_BACKEND) or has_any_api_key()


# ---- Werte/Einstellungen in die Key-Datei schreiben (fürs Setup-Panel) ---------

def save_values(values):
    """
    dict in ~/.nuke/GenAI_keys.json mergen (Datei/Ordner anlegen).
    Nur nicht-leere Werte werden gesetzt; leere/None löschen den Eintrag.
    """
    _migrate_legacy_keys()
    path = keys_file_path()
    data = _load_keys_file()
    for k, v in (values or {}).items():
        if v is None or v == "":
            data.pop(k, None)
        else:
            data[k] = v
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return path


def get_value(name, default=None):
    """
    Wert NUR aus der Key-Datei lesen (ohne Env) — für Einstellungen wie den
    Standard-Ausgabeordner ('output_dir').
    """
    val = _load_keys_file().get(name)
    return val if val not in (None, "") else default


class MissingKeyError(RuntimeError):
    """Wird geworfen, wenn ein zwingend benötigter Key fehlt."""

    def __init__(self, name):
        self.key_name = name
        super().__init__(
            "Kein API-Key '{name}' gefunden.\n"
            "Trage ihn als Umgebungsvariable ein ODER lege ihn in\n"
            "  {path}\n"
            'als {{"{name}": "dein-key-hier"}} ab.'.format(
                name=name, path=keys_file_path()
            )
        )
