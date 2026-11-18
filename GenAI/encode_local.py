"""
encode_local.py — eigenständiges Script: PNG-Sequenz -> mp4 (nur lokal, KEIN Upload).

Wird von einem Python mit OpenCV (cv2) ausgeführt (nicht Nukes Python); fuer h264 wird
zusätzlich imageio-ffmpeg gebraucht, sonst Fallback auf cv2-mp4v. Fuer "Generate Only":
die Eingaenge werden 1:1 (gleiche Bildzahl, Comp-fps, volle Aufloesung) als mp4 abgelegt,
damit man sie woanders hochladen kann.

    python encode_local.py <seq_dir> <fps> <out_mp4> [src_fps] [pad_frames]

src_fps  = Rate der gerenderten Sequenz (Comp-fps); weicht sie von <fps> ab, wird zeitrichtig
           umgetaktet (wie beim Guide-Video-Upload).
pad_frames = Mindestlaenge; kuerzere Sequenzen werden mit dem letzten Bild aufgefuellt.

Gibt als letzte Zeile JSON:  {"path": "...", "frames": N, "codec": "h264|mp4v"}
"""

import glob
import json
import os
import sys

# Die Encoder liegen in encode_upload.py (gleicher Ordner = sys.path[0]).
import encode_upload as eu


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"error": "usage: encode_local.py <seq_dir> <fps> <out_mp4>"}))
        return 2
    seq_dir = sys.argv[1]
    fps = float(sys.argv[2]) or 24.0
    out_mp4 = sys.argv[3]

    src_fps = None
    pad_frames = 0
    try:
        if len(sys.argv) > 4:
            src_fps = float(sys.argv[4]) or None
        if len(sys.argv) > 5:
            pad_frames = int(float(sys.argv[5]))
    except ValueError:
        pass

    files = sorted(glob.glob(os.path.join(seq_dir, "*.png")))
    if not files:
        print(json.dumps({"error": "no frames in " + seq_dir}))
        return 1
    files = eu._resample(files, src_fps, fps)
    if pad_frames > len(files):
        files = files + [files[-1]] * (pad_frames - len(files))

    codec = "h264"
    if not eu._encode_h264(files, fps, out_mp4):
        codec = "mp4v"
        eu._encode_mp4v(files, fps, out_mp4)
    if not (os.path.isfile(out_mp4) and os.path.getsize(out_mp4) > 1000):
        print(json.dumps({"error": "mp4 was not written: " + out_mp4}))
        return 1
    print(json.dumps({"path": out_mp4, "frames": len(files), "codec": codec}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
