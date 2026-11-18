"""
video.py — Video (mp4) in eine PNG-Sequenz transcoden, damit Nuke es zuverlässig
liest. Ruft transcode.py über ein Python mit OpenCV (cv2) auf; Nukes Python hat
kein cv2.

Der cv2-Python wird gesucht in dieser Reihenfolge:
  1) Umgebungsvariable GENAI_CV2_PYTHON (falls gesetzt) — frueher AI_GEN_CV2_PYTHON, wird
     weiter unterstuetzt fuer alte Installationen
  2) 'python' / 'python3' aus dem PATH
  3) übliche Windows-Installationspfade
und dabei jeweils getestet, ob 'import cv2' klappt.
"""

import json
import os
import shutil
import subprocess

from . import config


CV2_PYTHON_ENV = "GENAI_CV2_PYTHON"
CV2_PYTHON_ENV_LEGACY = "AI_GEN_CV2_PYTHON"  # frueherer Name, weiter unterstuetzt


def _tail_error(text, limit=300):
    """
    Letzte aussagekräftige Zeile aus stderr/stdout ziehen (bei Python-Tracebacks die
    Exception-Zeile). Besser als die ersten N Zeichen, die nur den Traceback-Kopf
    zeigen und den eigentlichen Grund abschneiden.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][:limit] if lines else ""

_CANDIDATE_PATHS = [
    r"C:\Program Files\Python310\python.exe",
    r"C:\Program Files\Python311\python.exe",
    r"C:\Program Files\Python312\python.exe",
    r"C:\Program Files\Python313\python.exe",
]


def _candidates():
    seen = []
    # 1) Vom Installer gespeicherter Pfad (der Python, mit dem install.py lief und
    #    in dem die Video-Helper-Pakete garantiert installiert sind).
    from_config = config.get_value("video_helper_python")
    if from_config:
        seen.append(from_config)
    # 2) Legacy: env-Var (frueher der einzige Weg).
    env = os.environ.get(CV2_PYTHON_ENV) or os.environ.get(CV2_PYTHON_ENV_LEGACY)
    if env:
        seen.append(env)
    # 3) PATH-Fallback.
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            seen.append(found)
    # 4) Uebliche Windows-Installationspfade als letzter Rettungsanker.
    seen.extend(_CANDIDATE_PATHS)
    # Duplikate/leere entfernen, Reihenfolge erhalten
    out = []
    for c in seen:
        if c and c not in out:
            out.append(c)
    return out


def _pip_hint(packages):
    """Fertiger pip-Befehl fuer das Helfer-Python (der vom Installer gespeicherte, sonst 'python')."""
    exe = config.get_value("video_helper_python") or "python"
    return '"{exe}" -m pip install {pk}'.format(exe=exe, pk=packages)


def find_cv2_python(modules=("cv2",)):
    """Ersten Python-Interpreter finden, der die genannten Module importieren kann."""
    check = "import " + ",".join(modules)
    for exe in _candidates():
        try:
            if exe.lower().endswith(".exe") and not os.path.isfile(exe):
                continue
            res = subprocess.run([exe, "-c", check], capture_output=True, timeout=30)
            if res.returncode == 0:
                return exe
        except Exception:
            continue
    return None


def encode_and_upload(seq_dir, first, last, fps, src_fps=None, pad_frames=None):
    """
    Guide-Video: PNG-Sequenz -> mp4 -> fal-Upload. Liefert die fal-URL.
    Läuft über ein Python mit cv2 UND fal_client (externes Helfer-Python).

    fps     = vom Modell erwartete Rate (meist 24).
    src_fps = Rate der gerenderten Sequenz (Comp-fps); weicht sie von fps ab, wird
              zeitrichtig umgetaktet (Laufzeit in Sekunden bleibt erhalten).
    pad_frames = Mindestlaenge; kuerzere Referenzen werden mit Standbild aufgefuellt.
    """
    fal_key = config.get_key("FAL_KEY", required=True)     # zuerst: ohne Key hilft auch kein pip install
    py = find_cv2_python(("cv2", "fal_client"))
    if not py:
        raise RuntimeError(
            "No Python with OpenCV (cv2) AND fal_client found for the guide-video upload. "
            "Install the missing package: {cmd}  (or set env {env})."
            .format(cmd=_pip_hint("opencv-python fal-client"), env=CV2_PYTHON_ENV)
        )
    out_mp4 = seq_dir.rstrip("/\\") + ".mp4"
    script = os.path.join(os.path.dirname(__file__), "encode_upload.py")
    env = dict(os.environ)
    env["FAL_KEY"] = fal_key
    cmd = [py, script, seq_dir, str(first), str(last), str(fps), out_mp4]
    if src_fps or pad_frames:
        cmd.append(str(src_fps or 0))
    if pad_frames:
        cmd.append(str(int(pad_frames)))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, env=env)
    lines = [l for l in (res.stdout or "").strip().splitlines() if l.strip().startswith("{")]
    info = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or info.get("error"):
        detail = info.get("error") or _tail_error(res.stderr or res.stdout)
        raise RuntimeError("Guide-video encode/upload failed: " + str(detail))
    return info["url"]


def encode_local(seq_dir, fps, out_mp4, src_fps=None, pad_frames=None):
    """
    PNG-Sequenz -> mp4 auf der Platte (kein Upload, kein fal_client noetig). Gleiche
    Bildzahl und fps wie die Sequenz (frame-genau), gleiche h264-Qualitaet wie beim
    Guide-Video-Upload. Liefert out_mp4.
    src_fps/pad_frames wie bei encode_and_upload (Umtakten bzw. Standbild-Auffuellen).
    """
    py = find_cv2_python(("cv2", "imageio_ffmpeg")) or find_cv2_python(("cv2",))
    if not py:
        raise RuntimeError(
            "No Python with OpenCV (cv2) found for the video export. Install it: "
            "{cmd}  (or set env {env})."
            .format(cmd=_pip_hint("opencv-python imageio-ffmpeg"), env=CV2_PYTHON_ENV)
        )
    script = os.path.join(os.path.dirname(__file__), "encode_local.py")
    cmd = [py, script, seq_dir, str(fps), out_mp4]
    if src_fps or pad_frames:
        cmd.append(str(src_fps or 0))
    if pad_frames:
        cmd.append(str(int(pad_frames)))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    lines = [l for l in (res.stdout or "").strip().splitlines() if l.strip().startswith("{")]
    info = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or info.get("error"):
        detail = info.get("error") or _tail_error(res.stderr or res.stdout)
        raise RuntimeError("Video export failed: " + str(detail))
    return out_mp4


def upload_file(path):
    """
    Beliebige lokale Datei zu fal-Storage hochladen und die öffentliche URL liefern.
    Für APIs, die nur echte URLs akzeptieren (z. B. Magnific/Kling), kein base64.
    Läuft über ein Python mit fal_client (externes Helfer-Python).
    """
    fal_key = config.get_key("FAL_KEY", required=True)     # zuerst: ohne Key hilft auch kein pip install
    py = find_cv2_python(("fal_client",))
    if not py:
        raise RuntimeError(
            "No Python with fal_client found for the file upload. "
            "Install it: {cmd}  (or set env {env}).".format(
                cmd=_pip_hint("fal-client"), env=CV2_PYTHON_ENV)
        )
    script = os.path.join(os.path.dirname(__file__), "upload.py")
    env = dict(os.environ)
    env["FAL_KEY"] = fal_key
    res = subprocess.run(
        [py, script, path],
        capture_output=True, text=True, timeout=600, env=env,
    )
    lines = [l for l in (res.stdout or "").strip().splitlines() if l.strip().startswith("{")]
    info = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or info.get("error"):
        detail = info.get("error") or _tail_error(res.stderr or res.stdout)
        raise RuntimeError("File upload failed: " + str(detail))
    return info["url"]


def mp4_to_sequence(mp4_path, seq_dir, target_fps=None):
    """
    Transcodiert mp4_path -> <seq_dir>/frame.####.png.
    Liefert (nuke_file_pattern, first, last, fps, native_fps): fps ist die Rate der
    geschriebenen Sequenz, native_fps die des Videos. Mit target_fps (Comp-fps) wird
    zeitrichtig umgetaktet, sodass der Clip in Echtzeit statt zu schnell/langsam laeuft.
    Wirft RuntimeError mit klarer Anleitung, wenn kein cv2-Python gefunden wird.
    """
    py = find_cv2_python()
    if not py:
        raise RuntimeError(
            "No Python with OpenCV (cv2) found for video transcode. Install it "
            "(pip install opencv-python) or set the env var {env} to such a python.exe."
            .format(env=CV2_PYTHON_ENV)
        )
    script = os.path.join(os.path.dirname(__file__), "transcode.py")
    cmd = [py, script, mp4_path, seq_dir]
    if target_fps:
        cmd.append(str(target_fps))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    lines = [l for l in (res.stdout or "").strip().splitlines() if l.strip().startswith("{")]
    info = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or info.get("error"):
        detail = info.get("error") or _tail_error(res.stderr or res.stdout)
        raise RuntimeError("Video transcode failed: " + str(detail))

    pattern = os.path.join(seq_dir, "frame.####.png").replace("\\", "/")
    fps = float(info.get("fps", 24.0))
    return (pattern, int(info.get("first", 1)), int(info.get("last", 1)), fps,
            float(info.get("native_fps", fps)))
