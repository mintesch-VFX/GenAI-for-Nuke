"""
mcp_client.py — schlanker MCP-Client (Streamable HTTP, JSON-RPC) fuer https://mcp.magnific.com.

Nur das Noetige: initialize (Session), tools/call. Die Tool-Antworten kommen als
structuredContent (JSON); fehlt das, wird der Text-Block als JSON gelesen. Auth ueber
mcp_auth (Bearer-Token, bei 401 einmal erneuern). Python-Standardbibliothek, kein Nuke.
"""

import json
import threading
import urllib.error
import urllib.request

from . import mcp_auth
from .backends.base import BackendError, is_transient_http, format_error


PROTOCOL = "2025-06-18"
_PROVIDER = "Magnific MCP"


class McpClient(object):
    def __init__(self):
        self._sid = None
        self._n = 0
        self._lock = threading.RLock()

    # ---- Transport -------------------------------------------------------------

    def _next_id(self):
        with self._lock:
            self._n += 1
            return self._n

    def _post(self, msg, notify=False, _retried=False):
        token = _token(force=_retried == "auth")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(mcp_auth.MCP_URL, data=json.dumps(msg).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                text = resp.read().decode("utf-8", "replace")
                for k, v in resp.headers.items():
                    if k.lower() == "mcp-session-id":
                        self._sid = v
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if exc.code == 401 and _retried != "auth":
                return self._post(msg, notify, _retried="auth")   # Token erneuern, 1 Retry
            if exc.code in (400, 404) and self._sid and _retried is False and msg.get("method") != "initialize":
                self._sid = None                                    # Session abgelaufen
                self._initialize()
                return self._post(msg, notify, _retried="session")
            if exc.code == 401:
                raise BackendError(format_error(
                    "login expired or rejected -- log in again in the GenAI setup window",
                    _PROVIDER, "HTTP 401"))
            raise BackendError(format_error(
                _short(body) or "request failed", _PROVIDER, "HTTP {c}".format(c=exc.code)),
                transient=is_transient_http(exc.code))
        except urllib.error.URLError as exc:
            raise BackendError(format_error("network error: {r}".format(r=exc.reason), _PROVIDER),
                               transient=True)
        if notify or not text.strip():
            return None
        return _parse(text)

    def _initialize(self):
        with self._lock:
            self._sid = None
            r = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "genai-nuke", "version": "1.0"}}})
            if not r or "error" in r:
                raise BackendError(format_error("initialize failed: " + str(r)[:200], _PROVIDER))
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    # ---- Tool-Aufruf -----------------------------------------------------------

    def call(self, tool, arguments=None):
        """Tool aufrufen, Ergebnis-dict (structuredContent) zurueckgeben. BackendError bei Fehlern."""
        if self._sid is None:
            with self._lock:
                if self._sid is None:
                    self._initialize()
        r = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments or {}}})
        if not isinstance(r, dict):
            raise BackendError(format_error("empty answer from tool " + tool, _PROVIDER))
        if r.get("error"):
            e = r["error"]
            raise BackendError(format_error(_short(e.get("message") if isinstance(e, dict) else e),
                                            _PROVIDER, tool))
        res = r.get("result") or {}
        text = ""
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                text += c.get("text", "")
        if res.get("isError"):
            raise BackendError(format_error(_short(text) or "tool call failed", _PROVIDER, tool),
                               transient=False)
        data = res.get("structuredContent")
        if data is None and text.strip().startswith(("{", "[")):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return data if data is not None else {"text": text}


_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = McpClient()
        return _client


# ---- Hilfsfunktionen -----------------------------------------------------------

def _token(force=False):
    try:
        return mcp_auth.get_access_token(force_refresh=force)
    except mcp_auth.McpAuthError as exc:
        raise BackendError(format_error(str(exc), _PROVIDER))


def _parse(text):
    """Antwort kann reines JSON oder ein SSE-Stream ('data: {...}') sein."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass
    out = None
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except ValueError:
                continue
    if out is None:
        raise BackendError(format_error("invalid answer from the server", _PROVIDER, text[:200]))
    return out


def _short(text, limit=400):
    text = " ".join(str(text or "").split())
    return text[:limit]
