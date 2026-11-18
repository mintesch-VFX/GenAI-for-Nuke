"""
mcp_auth.py — Anmeldung am Magnific-MCP-Server (OAuth 2.0 Device-Flow + PKCE).

Der MCP-Server (https://mcp.magnific.com) kennt keinen API-Key, sondern nur OAuth mit
dem Magnific-Account. Fuer Nuke (kein Browser-Redirect moeglich) nutzen wir den
Device-Flow: die Node registriert sich einmalig als oeffentlicher Client (Dynamic Client
Registration), der User bestaetigt EINMAL einen Code im Browser, danach haelt ein
Refresh-Token (30 Tage, rotiert bei jeder Nutzung) die Anmeldung am Leben.

Gespeichert wird alles in ~/.nuke/GenAI_mcp.json (client_id + Tokens) -- getrennt von
GenAI_keys.json, nie im Script/Gizmo. Nur Python-Standardbibliothek, kein Nuke-Import.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


AUTH_BASE = "https://auth.magnific.com/realms/mcp"
REGISTER_URL = AUTH_BASE + "/clients-registrations/openid-connect"
DEVICE_URL = AUTH_BASE + "/protocol/openid-connect/auth/device"
TOKEN_URL = AUTH_BASE + "/protocol/openid-connect/token"
MCP_URL = "https://mcp.magnific.com"
SCOPE = "openid profile email mcp:custom-audience"
CLIENT_NAME = "GenAI Nuke"
STATE_FILENAME = "GenAI_mcp.json"

_lock = threading.RLock()


class McpAuthError(RuntimeError):
    """Anmeldung/Token-Refresh fehlgeschlagen (Text ist fuer den User gedacht)."""


def state_path():
    return os.path.join(config._nuke_home(), STATE_FILENAME)


def _load():
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    path = state_path()
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(path, 0o600)   # unter Windows wirkungslos, schadet nicht
    except OSError:
        pass


def is_logged_in():
    """True, wenn ein Refresh-Token vorliegt (Anmeldung vorhanden und nicht ausgeloggt)."""
    return bool(_load().get("refresh_token"))


def status_text():
    d = _load()
    if not d.get("refresh_token"):
        return "not logged in"
    left = d.get("refresh_expires_at", 0) - time.time()
    if left > 0:
        return "logged in (session valid for ~{n} more days)".format(n=max(1, int(left // 86400)))
    return "logged in (session may have expired -- log in again if calls fail)"


def logout():
    """Tokens verwerfen (der registrierte Client bleibt, damit der naechste Login schnell geht)."""
    with _lock:
        d = _load()
        for k in ("access_token", "refresh_token", "expires_at", "refresh_expires_at"):
            d.pop(k, None)
        _save(d)


# ---- HTTP -----------------------------------------------------------------------

def _post(url, data, as_form=True):
    if as_form:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            code, text = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise McpAuthError("network error talking to the Magnific login server: {r}".format(r=exc.reason))
    try:
        return code, (json.loads(text) if text else {})
    except ValueError:
        return code, {"error": "invalid_response", "error_description": text[:200]}


def _err(d):
    return "{e}: {m}".format(e=d.get("error", "error"), m=d.get("error_description", "")).strip(": ")


# ---- Client-Registrierung -------------------------------------------------------

def ensure_client():
    """client_id zurueckgeben; registriert sich beim ersten Mal (Dynamic Client Registration)."""
    with _lock:
        d = _load()
        if d.get("client_id"):
            return d["client_id"]
        code, r = _post(REGISTER_URL, {
            "client_name": CLIENT_NAME,
            "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "scope": SCOPE,
        }, as_form=False)
        if code not in (200, 201) or not r.get("client_id"):
            raise McpAuthError("client registration failed: " + _err(r))
        d["client_id"] = r["client_id"]
        _save(d)
        return d["client_id"]


# ---- Login (Device-Flow + PKCE) -------------------------------------------------

def start_device_login():
    """
    Device-Flow starten. Liefert ein dict mit 'verification_uri_complete', 'user_code',
    'expires_in' (+ interne Felder); damit wird danach finish_device_login() aufgerufen.
    """
    client_id = ensure_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code, r = _post(DEVICE_URL, {
        "client_id": client_id, "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    if code != 200 and r.get("error") == "invalid_client":
        # Client wurde serverseitig entfernt -> neu registrieren und noch einmal versuchen.
        with _lock:
            d = _load(); d.pop("client_id", None); _save(d)
        return start_device_login()
    if code != 200:
        raise McpAuthError("could not start the login: " + _err(r))
    r["_verifier"] = verifier
    r["_t0"] = time.time()
    return r


def finish_device_login(state, cancelled=None):
    """
    Blockiert (im Hintergrund-Thread aufrufen), bis der User den Code im Browser bestaetigt
    hat, und speichert dann die Tokens. cancelled: optionales Callable -> True = abbrechen.
    """
    client_id = ensure_client()
    interval = int(state.get("interval", 5))
    deadline = state["_t0"] + int(state.get("expires_in", 600))
    while time.time() < deadline:
        if cancelled and cancelled():
            raise McpAuthError("login cancelled")
        code, r = _post(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": state["device_code"], "client_id": client_id,
            "code_verifier": state["_verifier"],
        })
        if code == 200:
            _store_tokens(r)
            return True
        err = r.get("error")
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        raise McpAuthError("login failed: " + _err(r))
    raise McpAuthError("the login code expired -- start the login again")


def _store_tokens(r):
    with _lock:
        d = _load()
        now = time.time()
        d["access_token"] = r["access_token"]
        d["expires_at"] = now + int(r.get("expires_in", 300))
        if r.get("refresh_token"):
            d["refresh_token"] = r["refresh_token"]
            d["refresh_expires_at"] = now + int(r.get("refresh_expires_in", 0) or 0)
        _save(d)


# ---- Token fuer API-Aufrufe -----------------------------------------------------

def get_access_token(force_refresh=False):
    """
    Gueltiges Access-Token liefern (bei Bedarf per Refresh-Token erneuern).
    McpAuthError, wenn man neu anmelden muss (setup dialog > Magnific MCP).
    """
    with _lock:
        d = _load()
        if not d.get("refresh_token"):
            raise McpAuthError("not logged in to Magnific MCP -- log in in the GenAI setup window")
        if (not force_refresh and d.get("access_token")
                and d.get("expires_at", 0) - time.time() > 60):
            return d["access_token"]
        client_id = d.get("client_id") or ensure_client()
        code, r = _post(TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
            "client_id": client_id,
        })
        if code != 200:
            if r.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
                for k in ("access_token", "refresh_token", "expires_at", "refresh_expires_at"):
                    d.pop(k, None)
                _save(d)
                raise McpAuthError("the Magnific MCP login expired -- log in again in the GenAI setup window")
            raise McpAuthError("could not refresh the Magnific MCP login: " + _err(r))
        _store_tokens(r)
        return _load()["access_token"]
