"""
magnific_mcp.py — Backend ueber den Magnific-MCP-Server (statt der REST-API).

Warum: Ueber MCP gibt es neue Modelle sofort (Kling 4.0, Gemini Omni 1.2, ...) und die
komplette "Modify Video"-Familie, die per REST fehlt. Login: OAuth (siehe mcp_auth.py),
Abrechnung: Credits des angemeldeten (persoenlichen) Magnific-Accounts.

Ablauf:
  submit -> lokale Eingaenge hochladen (creations_request_upload -> PUT -> finalize)
            -> Tool aufrufen (video_generate | video_modify | video_depth_map | images_generate)
            -> Creation-ID
  poll   -> creations_wait (Long-Poll) -> Status / Ergebnis-URL
  download -> Standard (HTTP-GET der signierten URL)

Das Manifest bestimmt pro Modell/Mode: "mcp_tool", "mcp_slug" (Modell-Slug) und in der
input_map je Eingang eine "role": start | end | reference (+ "type": image|video) | source.
"""

import json
import mimetypes
import os
import urllib.error
import urllib.request

from .base import (
    Backend, BackendError, Status, PENDING, RUNNING, DONE, ERROR,
    format_error, is_transient_http, looks_like_prompt_denied, prompt_denied_message,
)
from .. import mcp_client


_PROVIDER = "Magnific MCP"

# Je Tool: Feldname des Modell-Slugs und wie ein Bild/Video-Verweis aussieht.
_TOOL = {
    "video_generate": {"slug_field": "slug", "ref_key": "url", "frame_key": "url"},
    "video_modify": {"slug_field": "slug", "ref_key": "creationIdentifier", "frame_key": None},
    "images_generate": {"slug_field": "mode", "ref_key": "identifier", "frame_key": None},
    # Video Depth Anything: nur Quelle + colormap, kein Modell-Slug, kein Prompt.
    "video_depth_map": {"slug_field": None, "ref_key": None, "frame_key": None, "prompt": False},
}

_FAILED = ("failed", "error", "errored", "canceled", "cancelled", "rejected", "blocked", "refused")
_DONE = ("completed", "complete", "done", "succeeded", "success", "finished")


class MagnificMcpBackend(Backend):
    name = "magnific_mcp"

    def __init__(self):
        self._uploads = {}   # lokaler Pfad -> Creation-ID (gleicher Upload nur einmal)

    # ---- oeffentliche Schnittstelle ---------------------------------------------

    def submit(self, model_cfg, prompt, inputs, params):
        tool = model_cfg.get("mcp_tool")
        spec = _TOOL.get(tool)
        if not spec:
            raise BackendError(format_error("unknown MCP tool {t!r} in the manifest".format(t=tool), _PROVIDER))
        client = mcp_client.get_client()

        args = {}
        if model_cfg.get("mcp_slug") and spec["slug_field"]:
            args[spec["slug_field"]] = model_cfg["mcp_slug"]
        if prompt and spec.get("prompt", True):
            args["prompt"] = prompt

        # UI-Parameter -> Tool-Argumente (gleiche Mechanik wie beim REST-Backend).
        int_keys = set(model_cfg.get("int_params", []))
        value_maps = model_cfg.get("api_value_maps") or {}
        for ui_key, field in (model_cfg.get("api_params") or {}).items():
            val = (params or {}).get(ui_key)
            if val in (None, ""):
                continue
            if ui_key in value_maps:
                val = value_maps[ui_key].get(val, val)
            if ui_key in int_keys:
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    pass
            args[field] = val
        for field, val in (model_cfg.get("api_static") or {}).items():
            args[field] = val

        # Eingaenge: hochladen und je nach Rolle einsetzen.
        input_map = model_cfg.get("input_map", [])
        for idx, path in sorted((inputs or {}).items()):
            if idx >= len(input_map) or not input_map[idx]:
                continue
            role = input_map[idx].get("role")
            if not role:
                continue
            ident = self._to_ref(client, path)
            if role == "source":
                args["creationIdentifier"] = ident
            elif role in ("start", "end") and spec["frame_key"]:
                args.setdefault("keyframes", {})[role] = {"type": "image", spec["frame_key"]: ident}
            elif role == "reference":
                rtype = input_map[idx].get("type", "image")
                args.setdefault("references", []).append({"type": rtype, spec["ref_key"]: ident})

        result = client.call(tool, args)
        ids = _find_identifiers(result)
        if not ids:
            raise BackendError(format_error(
                "no creation id in the answer", _PROVIDER, json.dumps(result)[:300]))
        return ids[0]

    def poll(self, job_id):
        client = mcp_client.get_client()
        data = client.call("creations_wait", {"identifiers": [job_id], "timeoutSeconds": 8})
        entries = data.get("results") if isinstance(data, dict) else None
        entry = entries[0] if entries else {}
        status = str(entry.get("status") or "").lower()
        res = entry.get("results") if isinstance(entry.get("results"), dict) else {}

        if status in _DONE:
            url = res.get("url") or _first_url(res)
            if not url:
                raise BackendError(format_error(
                    "completed but no result URL", _PROVIDER, json.dumps(entry)[:300]))
            return Status(DONE, progress=1.0, result_url=url)
        if status in _FAILED:
            reason = (entry.get("error") or entry.get("message") or entry.get("reason")
                      or res.get("error") or res.get("message"))
            if isinstance(reason, dict):
                reason = reason.get("message") or json.dumps(reason)
            if reason and looks_like_prompt_denied(str(reason)):
                return Status(ERROR, error=prompt_denied_message(_PROVIDER))
            return Status(ERROR, error=format_error(
                str(reason) if reason else "generation failed with no reason given", _PROVIDER, status))
        if status in ("queued", "pending", "created", "waiting"):
            return Status(PENDING, progress=0.0)
        if not status and data.get("allTerminal"):
            raise BackendError(format_error("job not found", _PROVIDER, json.dumps(data)[:300]))
        return Status(RUNNING, progress=None)

    def download(self, result_url, target_dir, filename=None):
        # Endung aus der URL nehmen (Bild kann png/jpg/webp sein, Video mp4/mov).
        ext = os.path.splitext(result_url.split("?")[0])[1].lower()
        if filename and ext in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"):
            filename = os.path.splitext(filename)[0] + ext
        return Backend.download(self, result_url, target_dir, filename=filename)

    # ---- intern -----------------------------------------------------------------

    def _to_ref(self, client, path):
        """http(s)-URL durchreichen; lokale Datei als (unsichtbare) Creation hochladen."""
        if str(path).startswith(("http://", "https://")):
            return path
        if path in self._uploads:
            return self._uploads[path]
        ident = _upload(client, path)
        self._uploads[path] = ident
        return ident


# ---- Upload ----------------------------------------------------------------------

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/x-m4v"}


def _upload(client, path):
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME.get(ext) or mimetypes.guess_type(path)[0]
    if not mime:
        raise BackendError(format_error("unsupported file type for upload: " + ext, _PROVIDER))
    req = client.call("creations_request_upload", {"mimeType": mime})
    put_url, tmp_path = req.get("proxyUploadUrl"), req.get("path")
    if not put_url or not tmp_path:
        raise BackendError(format_error("no upload target in the answer", _PROVIDER, json.dumps(req)[:200]))
    with open(path, "rb") as fh:
        body = fh.read()
    put = urllib.request.Request(put_url, data=body, method="PUT", headers={"Content-Type": mime})
    try:
        with urllib.request.urlopen(put, timeout=600) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise BackendError(format_error(
            "upload failed: " + exc.read().decode("utf-8", "replace")[:200], _PROVIDER,
            "HTTP {c}".format(c=exc.code)), transient=is_transient_http(exc.code))
    except urllib.error.URLError as exc:
        raise BackendError(format_error("upload failed: {r}".format(r=exc.reason), _PROVIDER), transient=True)
    fin = client.call("creations_finalize_upload", {
        "path": tmp_path, "fileName": os.path.basename(path), "visible": False})
    ident = fin.get("identifier") or (_find_identifiers(fin) or [None])[0]
    if not ident:
        raise BackendError(format_error("upload not finalized", _PROVIDER, json.dumps(fin)[:200]))
    return ident


# ---- Antworten lesen --------------------------------------------------------------

def _find_identifiers(obj):
    """Creation-IDs aus einer Tool-Antwort ziehen (Form ist je Tool leicht verschieden)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "identifier" and isinstance(v, str):
                    found.append(v)
                elif k == "identifiers" and isinstance(v, list):
                    found.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    out = []
    for f in found:
        if f not in out:
            out.append(f)
    return out


def _first_url(res):
    """Erste URL in einem results-Block suchen (z. B. tiles[0].url), sonst None."""
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("url"), str):
                return o["url"]
            for v in o.values():
                u = walk(v)
                if u:
                    return u
        elif isinstance(o, list):
            for x in o:
                u = walk(x)
                if u:
                    return u
        return None
    return walk(res)
