"""
ltx.py — Backend-Adapter fuer die LTX-API (api.ltx.io, Lightricks): Alpha Gen (Maske aus
Video) und Video-Upscale.

Ablauf (async):
  upload -> POST /v1/upload                      -> upload_url + storage_uri (+ required_headers)
            PUT  upload_url  (Datei, mp4)        -> Datei liegt 24 h in LTX-Storage
  submit -> POST /v2/<endpoint> {"video_uri": storage_uri, ...}  -> 202 {id}
  poll   -> GET  /v2/<endpoint>/<id>             -> status pending|processing|completed|failed
            completed: result{...} mit den Ergebnis-URLs (Keys je Endpoint)

Auth: Header  Authorization: Bearer <LTX_API_KEY>  (Key aus der LTX Developer Console,
console.ltx.io -- getrennt vom LTX-Studio-Account; Abrechnung ueber vorausbezahlte Credits).

Manifest-Felder: "endpoint" (z. B. "video-alpha-gen"), "api_static" (Zusatzfelder im Body,
z. B. {"resolution": "4k"}), "ltx_primary" (Result-Key der Hauptdatei, Default "video_url").
Weitere URLs im Result (z. B. cutout_video_url) gehen als Status.extra_urls zurueck und werden
vom Worker zusaetzlich neben dem Hauptergebnis gespeichert.

Nur Python-Standardbibliothek.
"""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

from .base import (
    Backend, BackendError, Status, PENDING, RUNNING, DONE, ERROR,
    format_error, http_error_message, is_transient_http, looks_like_prompt_denied,
    prompt_denied_message,
)
from .. import config


API_BASE = "https://api.ltx.io"
_PROVIDER = "LTX"


class LtxBackend(Backend):
    name = "ltx"

    def __init__(self):
        self._jobs = {}     # job_id -> (endpoint, primary_key)

    # ---- oeffentliche Schnittstelle ---------------------------------------------

    def submit(self, model_cfg, prompt, inputs, params):
        key = config.get_key("LTX_API_KEY", required=True)
        if not inputs:
            raise BackendError(format_error("connect a video to in1", _PROVIDER))
        src = inputs[sorted(inputs.keys())[0]]
        body = {"video_uri": self._to_uri(src, key)}
        for ui_key, field in (model_cfg.get("api_params") or {}).items():
            val = (params or {}).get(ui_key)
            if val not in (None, ""):
                body[field] = val
        for field, val in (model_cfg.get("api_static") or {}).items():
            body[field] = val

        endpoint = model_cfg["endpoint"]
        data = self._request("POST", "{b}/v2/{e}".format(b=API_BASE, e=endpoint), key, body)
        job_id = data.get("id")
        if not job_id:
            raise BackendError(format_error("no job id in the answer", _PROVIDER, str(data)[:300]))
        self._jobs[job_id] = (endpoint, model_cfg.get("ltx_primary") or "video_url")
        return job_id

    def poll(self, job_id):
        key = config.get_key("LTX_API_KEY", required=True)
        if job_id not in self._jobs:
            raise BackendError(format_error("unknown job id {j!r}".format(j=job_id), _PROVIDER))
        endpoint, primary = self._jobs[job_id]
        data = self._request("GET", "{b}/v2/{e}/{i}".format(b=API_BASE, e=endpoint, i=job_id), key)
        status = str(data.get("status") or "").lower()

        if status == "completed":
            result = data.get("result") if isinstance(data.get("result"), dict) else data
            urls = {k: v for k, v in result.items() if isinstance(v, str) and v.startswith("http")}
            main = urls.pop(primary, None) or (next(iter(urls.values())) if urls else None)
            if not main:
                raise BackendError(format_error(
                    "completed but no result URL", _PROVIDER, json.dumps(data)[:300]))
            urls = {k.replace("_video_url", "").replace("_url", ""): v for k, v in urls.items() if v != main}
            st = Status(DONE, progress=1.0, result_url=main)
            st.extra_urls = urls
            return st
        if status == "failed":
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            etype, msg = str(err.get("type") or ""), str(err.get("message") or "")
            if etype == "content_filtered_error" or looks_like_prompt_denied(msg):
                return Status(ERROR, error=prompt_denied_message(_PROVIDER))
            if etype == "insufficient_funds_error":
                return Status(ERROR, error=format_error(
                    "not enough LTX credits -- buy credits in the LTX Developer Console", _PROVIDER, etype))
            return Status(ERROR, error=format_error(msg or "job failed", _PROVIDER, etype or "failed"))
        if status == "processing":
            return Status(RUNNING, progress=None)
        if status in ("pending", "queued", ""):
            return Status(PENDING, progress=0.0)
        return Status(ERROR, error=format_error(
            "unexpected status {s!r}".format(s=status), _PROVIDER, json.dumps(data)[:300]))

    # ---- intern -----------------------------------------------------------------

    def _to_uri(self, src, key):
        """https-URL durchreichen; lokale Datei zu LTX-Storage hochladen (storage_uri)."""
        if str(src).startswith(("https://", "ltx://")):
            return src
        info = self._request("POST", API_BASE + "/v1/upload", key)
        upload_url, storage_uri = info.get("upload_url"), info.get("storage_uri")
        if not upload_url or not storage_uri:
            raise BackendError(format_error(
                "no upload target in the answer", _PROVIDER, json.dumps(info)[:200]))
        headers = dict(info.get("required_headers") or {})
        headers.setdefault("Content-Type", "video/mp4")
        with open(src, "rb") as fh:
            payload = fh.read()
        req = Request(upload_url, data=payload, headers=headers, method="PUT")
        try:
            with urlopen(req, timeout=900) as resp:
                resp.read()
        except HTTPError as exc:
            raise BackendError(format_error(
                "upload failed: " + exc.read().decode("utf-8", "replace")[:200], _PROVIDER,
                "HTTP {c}".format(c=exc.code)), transient=is_transient_http(exc.code))
        except URLError as exc:
            raise BackendError(format_error("upload failed: {r}".format(r=exc.reason), _PROVIDER),
                               transient=True)
        return storage_uri

    def _request(self, method, url, key, body=None):
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            headers["Content-Type"] = "application/json"
            raw = b""
        req = Request(url, data=raw, headers=headers, method=method)
        try:
            with urlopen(req, timeout=120) as resp:
                text = resp.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise BackendError(http_error_message(_PROVIDER, detail, code=exc.code, url=url),
                               transient=is_transient_http(exc.code))
        except URLError as exc:
            raise BackendError(format_error("network error: {r}".format(r=exc.reason), _PROVIDER),
                               transient=True)
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise BackendError(format_error("invalid JSON in the answer", _PROVIDER, text[:300]))
