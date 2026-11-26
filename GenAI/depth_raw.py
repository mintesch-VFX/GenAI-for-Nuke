"""
depth_raw.py -- Float-Depth (Magnific video_depth_map mit rawDepth) als EXR-Sequenz.

Der Server haelt zu einem Depth-Clip optional die rohen Float-Werte als .npz bereit
(Archiv mit "depths.npy", Form [N, H, W]). Das 8-Bit-Video bandet in ZDefocus & Co.;
hier wird daraus pro Frame eine EXR-Datei geschrieben, die ein Nuke-Read direkt liest.

Bewusst reines Python (Nuke bringt weder numpy noch einen EXR-Writer mit):
  * .npy-Header wird mit ast.literal_eval gelesen (nie eval), Daten werden Frame fuer
    Frame aus dem Zip gestreamt -- ein 1080p-Clip belegt nie mehr als ein paar Frames im RAM.
  * EXR: unkomprimiert (NO_COMPRESSION), Scanlines, Kanaele B, G, R als 32-bit float
    (alle drei = Depth, damit der Viewer ein Graubild zeigt und rgba.red die Werte traegt).
"""

import ast
import os
import struct
import urllib.request
import zipfile
from array import array

_MEMBER = "depths.npy"
_CHUNK = 1 << 20


class DepthError(RuntimeError):
    pass


# ---- Download (gestreamt, kann mehrere GB gross sein) ----------------------------------

def download_to_file(url, path, should_stop=None, timeout=120):
    """HTTP-GET in Stuecken in eine Datei schreiben. Bricht bei should_stop() sauber ab."""
    req = urllib.request.Request(url, headers={"User-Agent": "nuke-genai/0.1"})
    part = path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
            while True:
                if should_stop and should_stop():
                    raise DepthError("stopped")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(part, path)
    except BaseException:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise
    return path


# ---- .npy lesen -------------------------------------------------------------------------

def _read_npy_header(fh):
    magic = fh.read(6)
    if magic != b"\x93NUMPY":
        raise DepthError("depths.npy is not a .npy file")
    major = fh.read(2)[0]
    if major == 1:
        hlen = struct.unpack("<H", fh.read(2))[0]
    elif major in (2, 3):
        hlen = struct.unpack("<I", fh.read(4))[0]
    else:
        raise DepthError("unsupported .npy version {v}".format(v=major))
    header = ast.literal_eval(fh.read(hlen).decode("latin1").strip())
    descr, shape = header.get("descr"), tuple(header.get("shape") or ())
    if header.get("fortran_order"):
        raise DepthError("depths.npy is Fortran-ordered (unsupported)")
    if len(shape) == 4 and shape[1] == 1:       # [N, 1, H, W]
        shape = (shape[0], shape[2], shape[3])
    if len(shape) != 3:
        raise DepthError("unexpected depth shape {s}".format(s=shape))
    if descr not in ("<f4", "<f2", "<f8", "=f4", "|f4"):
        raise DepthError("unsupported depth dtype {d}".format(d=descr))
    return descr, shape


def _to_float32_le(raw, descr, count):
    """Rohbytes eines Frames -> little-endian float32-Bytes."""
    if descr in ("<f4", "=f4", "|f4"):
        return raw
    if descr == "<f2":
        vals = struct.unpack("<{n}e".format(n=count), raw)
    else:  # "<f8"
        vals = struct.unpack("<{n}d".format(n=count), raw)
    arr = array("f", vals)
    if struct.pack("=H", 1) != struct.pack("<H", 1):    # Big-Endian-Host
        arr.byteswap()
    return arr.tobytes()


# ---- EXR schreiben ----------------------------------------------------------------------

def _attr(name, typ, payload):
    return name.encode() + b"\0" + typ.encode() + b"\0" + struct.pack("<i", len(payload)) + payload


def _exr_header(width, height):
    chans = b""
    for name in (b"B", b"G", b"R"):                  # alphabetisch, wie die Spec verlangt
        chans += name + b"\0" + struct.pack("<iB3xii", 2, 0, 1, 1)   # FLOAT, pLinear 0, Sampling 1/1
    chans += b"\0"
    box = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    out = b"\x76\x2f\x31\x01" + struct.pack("<I", 2)
    out += _attr("channels", "chlist", chans)
    out += _attr("compression", "compression", b"\x00")            # NO_COMPRESSION
    out += _attr("dataWindow", "box2i", box)
    out += _attr("displayWindow", "box2i", box)
    out += _attr("lineOrder", "lineOrder", b"\x00")                # INCREASING_Y
    out += _attr("pixelAspectRatio", "float", struct.pack("<f", 1.0))
    out += _attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
    out += _attr("screenWindowWidth", "float", struct.pack("<f", 1.0))
    return out + b"\0"


def write_exr_gray(path, width, height, row_bytes_f32):
    """Eine EXR schreiben; row_bytes_f32 = width*height little-endian float32 (zeilenweise)."""
    header = _exr_header(width, height)
    stride = width * 4
    line_size = 8 + stride * 3                       # y + Datengroesse + B,G,R
    base = len(header) + 8 * height                  # Offset-Tabelle: ein uint64 pro Scanline
    parts = [header, struct.pack("<{n}Q".format(n=height), *[base + y * line_size for y in range(height)])]
    for y in range(height):
        row = row_bytes_f32[y * stride:(y + 1) * stride]
        parts.append(struct.pack("<ii", y, stride * 3))
        parts.append(row * 3)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(b"".join(parts))
    os.replace(tmp, path)


# ---- Hauptfunktion ----------------------------------------------------------------------

def npz_to_exr_sequence(npz_path, out_dir, progress=None, should_stop=None):
    """
    .npz -> <out_dir>/depth.####.exr (Frame 1..N). Liefert (pattern, first, last).
    progress(done, total) wird je Frame gemeldet; should_stop() bricht ab (Aufraeumen: Aufrufer).
    """
    os.makedirs(out_dir, exist_ok=True)
    try:
        zf = zipfile.ZipFile(npz_path)
    except zipfile.BadZipFile:
        raise DepthError("the depth archive is not a valid .npz")
    with zf:
        if _MEMBER not in zf.namelist():
            raise DepthError("the depth archive has no {m}".format(m=_MEMBER))
        with zf.open(_MEMBER) as fh:
            descr, (n, h, w) = _read_npy_header(fh)
            if n < 1 or h < 1 or w < 1:
                raise DepthError("empty depth data")
            itemsize = {"<f4": 4, "=f4": 4, "|f4": 4, "<f2": 2, "<f8": 8}[descr]
            frame_bytes = h * w * itemsize
            for i in range(n):
                if should_stop and should_stop():
                    raise DepthError("stopped")
                raw = fh.read(frame_bytes)
                while len(raw) < frame_bytes:        # Zip-Streams liefern teils weniger als gefragt
                    more = fh.read(frame_bytes - len(raw))
                    if not more:
                        raise DepthError("depth data ends early (frame {i} of {n})".format(i=i + 1, n=n))
                    raw += more
                data = _to_float32_le(raw, descr, h * w)
                write_exr_gray(os.path.join(out_dir, "depth.{k:04d}.exr".format(k=i + 1)), w, h, data)
                if progress:
                    progress(i + 1, n)
    return os.path.join(out_dir, "depth.####.exr").replace("\\", "/"), 1, n
