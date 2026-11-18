"""
encode_upload.py — eigenständiges Script: PNG-Sequenz -> mp4 -> fal-Upload.

Wird von einem Python mit OpenCV (cv2) UND fal_client ausgeführt (nicht Nukes
Python). Für Guide-/Referenz-Videos: die in Nuke gerenderte Sequenz wird zu mp4
encodiert und zu fal hochgeladen, damit sie als video_url(s) an die API geht.

    python encode_upload.py <seq_dir> <first> <last> <fps> <out_mp4> [src_fps] [pad_frames]

pad_frames = Mindestlaenge in Bildern; kuerzere Sequenzen werden mit dem letzten Bild
aufgefuellt (Standbild).

src_fps = Framerate der gerenderten Sequenz (Comp-fps). Weicht sie von <fps> (der vom
Modell erwarteten Rate, i. d. R. 24) ab, wird zeitrichtig umgetaktet (naechstliegendes
Quellbild), sodass die Laufzeit in Sekunden erhalten bleibt. Sonst stuende das
Referenzvideo bei 25/30-fps-Comps in falschem Tempo.

Encodiert bevorzugt als **h264** (yuv420p, faststart) via ffmpeg (imageio-ffmpeg),
weil manche APIs (Kling) MPEG-4/mp4v ablehnen. Fällt auf cv2-mp4v zurück, wenn
ffmpeg nicht verfügbar ist.

Braucht FAL_KEY in der Umgebung (setzt der Aufrufer). Gibt als letzte Zeile JSON:
    {"url": "https://...", "frames": N, "codec": "h264|mp4v"}
"""

import glob
import json
import math
import os
import subprocess
import sys

import cv2

try:                      # encode_local.py nutzt nur die Encoder und braucht kein fal_client
    import fal_client
except ImportError:
    fal_client = None

_FPS_TOLERANCE = 0.005  # 23.976 vs 24 gilt als gleich


def _resample(files, src_fps, dst_fps):
    """
    Bilderliste von src_fps auf dst_fps umtakten (naechstliegendes Quellbild).
    Gleiche Rate (oder src_fps unbekannt) -> unveraendert.
    """
    if not src_fps or not dst_fps or abs(src_fps / dst_fps - 1.0) <= _FPS_TOLERANCE:
        return files
    n_out = max(1, int(round(len(files) * dst_fps / src_fps)))
    last = len(files) - 1
    return [files[min(last, int(math.floor(k * src_fps / dst_fps + 0.5)))] for k in range(n_out)]


# Referenzvideos sind kurz -> Qualitaet vor Dateigroesse (libx264-Standard waere crf 23, das
# macht Wald/Korn matschig). Obergrenze wegen der Upload-Limits der APIs (Seedance 50 MB).
_CRF = 10
_FALLBACK_CRF = 18
_MAX_BYTES = 40 * 1024 * 1024


def _encode_h264(files, fps, out_mp4):
    """PNG-Sequenz -> h264-mp4 via ffmpeg. True bei Erfolg; zu grosse Datei -> hoeheres crf."""
    if not _encode_h264_crf(files, fps, out_mp4, _CRF):
        return False
    if os.path.getsize(out_mp4) > _MAX_BYTES:
        return _encode_h264_crf(files, fps, out_mp4, _FALLBACK_CRF)
    return True


def _encode_h264_crf(files, fps, out_mp4, crf):
    """PNG-Sequenz -> h264-mp4 via ffmpeg (BGR-Frames per stdin) mit festem crf."""
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return False
    first = cv2.imread(files[0])
    if first is None:
        return False
    h, w = first.shape[:2]
    cmd = [
        ff, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", "{w}x{h}".format(w=w, h=h), "-r", str(fps), "-i", "-",
        "-an", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_color_matrix=bt709:out_range=tv",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv", "-movflags", "+faststart",
        out_mp4,
    ]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for f in files:
            im = cv2.imread(f)
            if im is not None:
                p.stdin.write(im.tobytes())
        p.stdin.close()
        p.wait()
    except Exception:
        return False
    return os.path.isfile(out_mp4) and os.path.getsize(out_mp4) > 1000


def _encode_mp4v(files, fps, out_mp4):
    """Fallback: cv2 mp4v (MPEG-4 Part 2)."""
    first = cv2.imread(files[0])
    h, w = first.shape[:2]
    writer = cv2.VideoWriter(out_mp4, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in files:
        img = cv2.imread(f)
        if img is not None:
            writer.write(img)
    writer.release()


def _fal_error(exc):
    """
    Menschenlesbaren Grund aus einer fal/httpx-Exception ziehen — inkl. Response-Body,
    denn dort steht der eigentliche Grund (z. B. 'User is locked. Exhausted balance').
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        detail = None
        try:
            detail = resp.json().get("detail")
        except Exception:
            pass
        try:
            body = resp.text
        except Exception:
            body = ""
        msg = detail or body or str(exc)
        return "fal upload failed (HTTP {c}): {m}".format(c=resp.status_code, m=str(msg)[:300])
    return "fal upload failed: " + str(exc)


def main():
    if len(sys.argv) < 6:
        print(json.dumps({"error": "usage: encode_upload.py <seq_dir> <first> <last> <fps> <out_mp4>"}))
        return 2
    seq_dir = sys.argv[1]
    fps = float(sys.argv[4]) or 24.0
    out_mp4 = sys.argv[5]
    src_fps = None
    if len(sys.argv) > 6:
        try:
            src_fps = float(sys.argv[6]) or None
        except ValueError:
            src_fps = None

    files = sorted(glob.glob(os.path.join(seq_dir, "*.png")))
    if not files:
        print(json.dumps({"error": "no frames in " + seq_dir}))
        return 1
    files = _resample(files, src_fps, fps)
    # Referenz kuerzer als die Output-Dauer: letztes Bild als Standbild halten, damit
    # das Modell die Referenz 1:1 (nicht gedehnt) auf die Output-Dauer legt.
    pad_frames = 0
    if len(sys.argv) > 7:
        try:
            pad_frames = int(float(sys.argv[7]))
        except ValueError:
            pad_frames = 0
    if pad_frames > len(files):
        files = files + [files[-1]] * (pad_frames - len(files))

    codec = "h264"
    if not _encode_h264(files, fps, out_mp4):
        codec = "mp4v"
        _encode_mp4v(files, fps, out_mp4)

    if fal_client is None:
        print(json.dumps({"error": "fal_client is not installed (pip install fal-client)"}))
        return 1
    try:
        url = fal_client.upload_file(out_mp4)
    except Exception as exc:
        print(json.dumps({"error": _fal_error(exc)}))
        return 1
    print(json.dumps({"url": url, "frames": len(files), "codec": codec}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
