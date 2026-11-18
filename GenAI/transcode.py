"""
transcode.py — eigenständiges Script: wandelt ein Video (mp4) in eine PNG-Sequenz.

Wird von einem Python mit OpenCV (cv2) ausgeführt — NICHT von Nukes Python (das
hat kein cv2). Bewusst ohne Abhängigkeit zum GenAI-Paket, damit es standalone läuft.

    python transcode.py <input.mp4> <out_dir> [target_fps]

Schreibt <out_dir>/frame.0001.png ... und gibt als letzte Zeile JSON aus:
    {"first": 1, "last": N, "count": N, "fps": 24.0, "native_fps": 24.0, "retimed": false}

Mit target_fps (Comp-Framerate) wird zeitrichtig umgetaktet: Das Video wird mit
dem naechstliegenden Quellbild auf das Ziel-Raster gelegt (Bilder doppelt/ausgelassen),
damit ein 24-fps-Clip in einer 25/30-fps-Comp in Echtzeit statt zu schnell/langsam
laeuft. Ohne target_fps (oder bei gleicher Rate) bleiben alle Bilder 1:1 erhalten.

Grund: Nukes mov-Reader indiziert manche h264-mp4s (z. B. von fal/Seedance) falsch
und sieht nur 1 Frame; eine PNG-Sequenz liest Nuke zuverlaessig.
"""

import json
import math
import os
import sys

import cv2

_FPS_TOLERANCE = 0.005  # 23.976 vs 24 gilt als gleich


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: transcode.py <mp4> <out_dir> [target_fps]"}))
        return 2
    mp4, out_dir = sys.argv[1], sys.argv[2]
    target_fps = None
    if len(sys.argv) > 3:
        try:
            target_fps = float(sys.argv[3]) or None
        except ValueError:
            target_fps = None
    os.makedirs(out_dir, exist_ok=True)

    cap = cv2.VideoCapture(mp4)
    if not cap.isOpened():
        print(json.dumps({"error": "cannot open " + mp4}))
        return 1

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    retime = bool(target_fps) and abs(native_fps / target_fps - 1.0) > _FPS_TOLERANCE
    ratio = native_fps / target_fps if retime else 1.0   # Quell-Bilder pro Ziel-Bild

    count = 0   # geschriebene Bilder
    out_k = 0   # naechstes Ziel-Bild (0-basiert, nur bei retime)
    src_i = -1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        src_i += 1
        if not retime:
            count += 1
            cv2.imwrite(os.path.join(out_dir, "frame.%04d.png" % count), frame)
            continue
        # Ziel-Bild k zeigt das Quellbild round(k * native/target).
        while int(math.floor(out_k * ratio + 0.5)) == src_i:
            count += 1
            out_k += 1
            cv2.imwrite(os.path.join(out_dir, "frame.%04d.png" % count), frame)
    cap.release()

    if count == 0:
        print(json.dumps({"error": "no frames decoded"}))
        return 1
    print(json.dumps({
        "first": 1, "last": count, "count": count,
        "fps": target_fps if retime else native_fps,
        "native_fps": native_fps, "retimed": retime,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
