"""
magnific.py — Backend-Adapter für die Magnific/Freepik-Video-API (Kling).

Grund: Kling kann als einziger Anbieter **nativ 4K** — und das gibt es nur über
Magnific (alle fal/Replicate/PiAPI/... enden bei 1080p). Deshalb ein eigenes
Backend statt fal.

Ablauf (async, wie fal):
  submit -> POST https://api.magnific.com/v1/ai/video/{endpoint}     -> data.task_id
  poll   -> GET  https://api.magnific.com/v1/ai/video/{endpoint}/{id} -> data.status / data.generated[]

Auth: Header  x-magnific-api-key: <MAGNIFIC_API_KEY>

Besonderheit: Magnific akzeptiert Bilder NUR als echte URLs (kein base64). Lokale
Eingänge werden daher vor dem Submit zu fal-Storage hochgeladen (video.upload_file),
die resultierende https-URL geht ins Payload. Guide-Video-Eingänge kommen bereits
als URL rein (der Worker lädt sie vorher hoch).

Nur Python-Standardbibliothek (+ video.upload_file für den Bild-Upload).
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

from .base import (
    Backend, BackendError, Status, PENDING, RUNNING, DONE, ERROR,
    looks_like_prompt_denied, prompt_denied_message, format_error, http_error_message,
    is_transient_http,
)
from .. import config, video


# Basis der Magnific-REST-API. Der Manifest-"endpoint" enthält die Kategorie plus
# Modell, z. B. "video/kling-v3-pro" oder "text-to-image/nano-banana-pro-flash".
API_BASE = "https://api.magnific.com/v1/ai"


class MagnificBackend(Backend):
    name = "magnific"

    def __init__(self):
        # task_id -> endpoint (für die Status-URL)
        self._jobs = {}

    # ---- öffentliche Schnittstelle ---------------------------------------------

    def submit(self, model_cfg, prompt, inputs, params):
        key = config.get_key("MAGNIFIC_API_KEY", required=True)

        # Endpoint entweder direkt (endpoint) oder aufloesung-abhaengig
        # (resolution_endpoints: {"1080p": "video/seedance-2-pro-1080p", ...}).
        # Grund: Seedance 2 Pro hat pro Aufloesung einen eigenen POST-Pfad — die
        # Aufloesung ist kein Body-Feld, sondern Teil der URL.
        endpoint = model_cfg["endpoint"]
        res_endpoints = model_cfg.get("resolution_endpoints") or {}
        if res_endpoints:
            res = (params or {}).get("resolution")
            if res and res in res_endpoints:
                endpoint = res_endpoints[res]

        payload = {"prompt": prompt or ""}

        # Nur die vom Manifest deklarierten API-Parameter senden (UI-Key -> API-Feld).
        # api_value_maps uebersetzt UI-Werte in API-Werte pro Feld (Magnific-Seedance
        # will z. B. "widescreen_16_9" statt "16:9").
        api_params = model_cfg.get("api_params", {})
        int_keys = set(model_cfg.get("int_params", []))
        value_maps = model_cfg.get("api_value_maps") or {}
        for ui_key, api_field in api_params.items():
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
            payload[api_field] = val

        # Konstante API-Felder aus dem Manifest (kein UI-Knob dahinter). Wird nach
        # api_params gesetzt und gewinnt daher.
        for api_field, val in (model_cfg.get("api_static") or {}).items():
            payload[api_field] = val

        # Eingänge per input_map einsetzen; lokale Dateien -> echte URL (Upload).
        _apply_inputs(payload, model_cfg.get("input_map", []), inputs, _to_url)

        url = "{base}/{ep}".format(base=API_BASE, ep=endpoint)
        data = self._request(url, key, method="POST", body=payload)

        task_id = _dig(data, "data", "task_id") or data.get("task_id")
        if not task_id:
            raise BackendError(format_error("no task_id in the response", "Magnific", str(data)[:300]))
        # Status-Pfad kann vom POST-Pfad abweichen (Kling: POST .../kling-v3-pro,
        # GET .../kling-v3/{id}; Seedance 2 Pro: POST .../seedance-2-pro-1080p,
        # GET .../seedance-2-pro/{id}). Manifest-"status_endpoint" ueberschreibt sonst endpoint.
        self._jobs[task_id] = model_cfg.get("status_endpoint") or endpoint
        return task_id

    def poll(self, job_id):
        key = config.get_key("MAGNIFIC_API_KEY", required=True)
        endpoint = self._jobs.get(job_id)
        if not endpoint:
            raise BackendError(format_error("unknown job_id {j!r}".format(j=job_id), "Magnific"))

        url = "{base}/{ep}/{id}".format(base=API_BASE, ep=endpoint, id=job_id)
        data = self._request(url, key, method="GET")
        block = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = (block.get("status") or "").upper()

        if raw == "COMPLETED":
            gen = block.get("generated") or []
            result_url = gen[0] if gen else None
            if not result_url:
                raise BackendError(format_error(
                    "COMPLETED but no result URL in the response", "Magnific", str(data)[:300]))
            return Status(DONE, progress=1.0, result_url=result_url)
        if raw in ("IN_PROGRESS", "PROCESSING"):
            return Status(RUNNING, progress=None)
        if raw in ("CREATED", "IN_QUEUE", "PENDING"):
            return Status(PENDING, progress=0.0)
        if raw == "FAILED":
            err = block.get("error")
            if err and looks_like_prompt_denied(str(err)):
                return Status(ERROR, error=prompt_denied_message("Magnific"))
            if err:
                return Status(ERROR, error=format_error(str(err), "Magnific", "task failed"))
            # Magnific liefert bei vielen Ablehnungen KEINE Begruendung (error=null) --
            # in unserer Erfahrung meistens Content-Safety oder ein ungueltiger Input
            # (z. B. Referenz zu kurz/lang). Ehrliche, hilfreiche Meldung statt des
            # nichtssagenden Rohobjekts.
            return Status(ERROR, error=format_error(
                "generation failed with no reason given by the API. This is often a "
                "content-safety rejection or an invalid input (e.g. an unsupported "
                "reference length/resolution). Try rephrasing the prompt, a different "
                "reference, or a different model.",
                "Magnific", "task failed, no detail",
            ))
        if looks_like_prompt_denied(str(block)):
            return Status(ERROR, error=prompt_denied_message("Magnific"))
        return Status(ERROR, error=format_error(
            "unexpected status {s!r}".format(s=raw or "?"), "Magnific", str(data)[:300]))

    # ---- intern ----------------------------------------------------------------

    def _request(self, url, key, method="GET", body=None):
        headers = {
            "x-magnific-api-key": key,
            "Accept": "application/json",
        }
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(url, data=raw, headers=headers, method=method)
        try:
            with urlopen(req, timeout=120) as resp:
                text = resp.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise BackendError(http_error_message("Magnific", detail, code=exc.code, url=url),
                               transient=is_transient_http(exc.code))
        except URLError as exc:
            raise BackendError(format_error("network error: {r}".format(r=exc.reason), "Magnific"),
                               transient=True)

        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise BackendError(format_error("invalid JSON in the response", "Magnific", text[:300]))


# ---- Hilfsfunktionen (rein, ohne State) --------------------------------------

def _apply_inputs(payload, input_map, inputs, to_url):
    """
    Wie fal._apply_inputs: angeschlossene Eingänge ins Payload einsetzen.
    "wrap" packt die URL in ein Objekt (Kling elements -> {"frontal_image_url": url}),
    "list" hängt an eine Liste an.
    """
    for idx, path in sorted((inputs or {}).items()):
        if idx >= len(input_map):
            continue
        spec = input_map[idx]
        if not spec or not spec.get("field"):
            continue
        url = to_url(path)
        wrap = spec.get("wrap")
        if wrap:
            value = {wrap: url}
            # Statische Zusatzfelder je Objekt (z. B. mime_type bei reference_images).
            value.update(spec.get("wrap_static") or {})
        else:
            value = url
        if spec.get("list"):
            payload.setdefault(spec["field"], []).append(value)
        else:
            payload[spec["field"]] = value


def _to_url(path):
    """
    http(s)-URLs unverändert durchreichen; lokale Dateien zu fal-Storage hochladen
    und die öffentliche URL liefern (Magnific akzeptiert kein base64).
    """
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return video.upload_file(path)


def _dig(data, *keys):
    """Verschachtelten Wert holen (z. B. data.task_id), None wenn nicht vorhanden."""
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur
