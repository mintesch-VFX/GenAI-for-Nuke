"""
install.py — one-time installer for the GenAI node (with a small window).

What it does:
  1) copies `GenAI/` (next to this file) to  ~/.nuke/GenAI
  2) adds the menu entry to ~/.nuke/menu.py  (Nodes > GenAI)
  3) optionally stores your own API keys in  ~/.nuke/GenAI_keys.json  (NOT needed for the
     default Magnific login -- you log in inside Nuke, see below)
  4) asks for one example .nk script + the folder where UPLOAD/DOWNLOAD should go,
     and derives the RELATIVE path from it — which then applies to every shot.

Run it (any Python) via install.bat, or:
    python install.py

Then (re)start Nuke -> Nodes > GenAI. The setup window opens by itself the first time:
log in with your Magnific account (once, in the browser) and you are ready.
For video the installer adds opencv-python + imageio-ffmpeg to this Python (fal-client
only if you enter a fal key). Uses only the standard library (tkinter).
"""

import json
import os
import shutil
import subprocess
import sys

import tkinter as tk
from tkinter import filedialog, messagebox


VIDEO_HELPER_PACKAGES = ("opencv-python", "imageio-ffmpeg")
VIDEO_HELPER_IMPORTS = ("cv2", "imageio_ffmpeg")
# Nur fuer den API-Modus mit fal.ai (Upload-Host bzw. fal-Modelle): wird nur installiert, wenn ein fal-Key eingetragen ist.
FAL_HELPER_PACKAGES = ("fal-client",)
FAL_HELPER_IMPORTS = ("fal_client",)


def check_video_helpers(python_exe, with_fal=False):
    """True, wenn cv2/imageio_ffmpeg (und mit with_fal auch fal_client) im gegebenen Python importierbar sind."""
    try:
        code = "import " + ",".join(VIDEO_HELPER_IMPORTS + (FAL_HELPER_IMPORTS if with_fal else ()))
        res = subprocess.run(
            [python_exe, "-c", code], capture_output=True, timeout=30
        )
        return res.returncode == 0
    except Exception:
        return False


def install_video_helpers(python_exe, with_fal=False):
    """
    pip install der Video-Helper-Pakete IN GENAU DEN Python, mit dem der Installer laeuft.
    Damit landet cv2 & Co. garantiert dort, wo die Node sie spaeter auch sucht (der
    Pfad wird in die Config gespeichert). Rueckgabe: (ok: bool, message: str).
    """
    if check_video_helpers(python_exe, with_fal):
        return True, "already installed"
    try:
        res = subprocess.run(
            [python_exe, "-m", "pip", "install"] + list(VIDEO_HELPER_PACKAGES)
            + (list(FAL_HELPER_PACKAGES) if with_fal else []),
            capture_output=True, text=True, timeout=600,
        )
        if res.returncode == 0:
            return True, "installed"
        tail = (res.stderr or res.stdout or "").strip().splitlines()[-4:]
        return False, "pip failed:\n" + "\n".join(tail)
    except Exception as exc:
        return False, "pip crashed: " + repr(exc)


KEY_FIELDS = [
    ("MAGNIFIC_API_KEY", "Magnific API Key", "Magnific in API mode (instead of the login)  ·  magnific.com > API"),
    ("FAL_KEY", "fal Key", "API mode: needed when images/video are sent as input, and for Seedance 2 / Gemini Omni / Kling 4.0 (API)  ·  fal.ai"),
    ("OPENAI_API_KEY", "OpenAI API Key", "GPT 2 in API mode  ·  platform.openai.com"),
    ("GOOGLE_API_KEY", "Google AI Studio Key", "optional  ·  aistudio.google.com"),
    ("LTX_API_KEY", "LTX API Key", "Alpha Gen, Upscale  ·  console.ltx.io (Developer Console)"),
]

MENU_SNIPPET = '''
# --- GenAI node (auto-added by install.py) ---
try:
    import os as _os
    import nuke
    import GenAI as _genai_pkg
    import GenAI.nuke_node as _genai_node
    _genai_node.install_callbacks()
    # Icons-Ordner in den Plugin-Pfad; Icon per DATEINAME (Nuke loest Menue-Icons
    # ueber den Plugin-Pfad auf, absolute Pfade mit Leerzeichen scheitern dabei).
    _genai_icons = _os.path.join(_os.path.dirname(_genai_pkg.__file__), "icons")
    if _os.path.isdir(_genai_icons):
        nuke.pluginAddPath(_genai_icons.replace("\\\\", "/"))
    # Direkt hinter den Standard-Nodes einsortieren (statt ganz unten bei den
    # Drittanbietern). "Other" ist der letzte Standard-Eintrag; Index dynamisch
    # ermitteln, damit es auf jedem Rechner passt (Plugin-Sets sind verschieden).
    _genai_kw = {"icon": "GenAI.png"}
    try:
        _genai_names = [_i.name() for _i in nuke.menu("Nodes").items()]
        if "Other" in _genai_names:
            _genai_kw["index"] = _genai_names.index("Other") + 1
    except Exception:
        pass
    nuke.menu("Nodes").addCommand(
        "GenAI",
        "import GenAI.nuke_node as N; N.build_node()",
        **_genai_kw
    )
except Exception as _e:
    try:
        nuke.tprint("GenAI menu failed: %r" % _e)
    except Exception:
        pass
# --- /GenAI node ---
'''


def nuke_home():
    return os.path.join(os.path.expanduser("~"), ".nuke")


def keys_file():
    return os.path.join(nuke_home(), "GenAI_keys.json")


def _legacy_keys_file():
    """Frueherer Dateiname; wird beim ersten Zugriff umbenannt (siehe _migrate_keys)."""
    return os.path.join(nuke_home(), "ai_gen_keys.json")


def _migrate_keys():
    """
    Alte ai_gen_keys.json (falls vorhanden und neue Datei fehlt) auf den neuen Namen
    umbenennen — damit bestehende Installationen ihre Keys behalten.
    """
    new = keys_file()
    old = _legacy_keys_file()
    if os.path.isfile(old) and not os.path.exists(new):
        try:
            os.rename(old, new)
        except OSError:
            pass


def load_keys():
    _migrate_keys()
    try:
        with open(keys_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def copy_package():
    """GenAI next to install.py -> ~/.nuke/GenAI. Returns destination path.
    Räumt ausserdem einen Rest der alten 'ai_gen'-Installation ab, damit der
    Import nach dem Rename nicht die alte Kopie zieht."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "GenAI")
    if not os.path.isdir(src):
        raise RuntimeError("'GenAI/' is not next to install.py: " + here)
    home = nuke_home()
    os.makedirs(home, exist_ok=True)
    dst = os.path.join(home, "GenAI")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Alten Paketordner der Vorversion wegräumen, falls vorhanden.
    legacy = os.path.join(home, "ai_gen")
    if os.path.isdir(legacy):
        try:
            shutil.rmtree(legacy)
        except OSError:
            pass
    return dst


def patch_menu():
    """Add the GenAI menu to ~/.nuke/menu.py (only if not already there)."""
    menu = os.path.join(nuke_home(), "menu.py")
    existing = ""
    if os.path.isfile(menu):
        with open(menu, "r", encoding="utf-8") as fh:
            existing = fh.read()
    if "GenAI.nuke_node" in existing or "ai_gen.nuke_node" in existing:
        return False
    with open(menu, "a", encoding="utf-8") as fh:
        fh.write("\n" + MENU_SNIPPET)
    return True


def _fwd(p):
    """Auf Forward-Slashes normalisieren + Trailing-Slash weg (kein JSON-Backslash-
    Problem, Nuke-nativ)."""
    return p.replace("\\", "/").rstrip("/")


def compute_output_rel(script_path, out_folder):
    """
    Relative path from the script FOLDER to the BASE folder, in forward slashes.
    The node creates GenAI/UPLOAD + GenAI/DOWNLOAD UNDER that base, so a trailing
    GenAI/UPLOAD/DOWNLOAD in the chosen folder is stripped (avoids doubling).
    Example: <shot>/NK/x.nk + <shot>  ->  ".." (-> <shot>/GenAI/UPLOAD + DOWNLOAD).
    Returns (mode, value): ("rel", "..") or ("abs", ".../base") on different drives.
    """
    script_dir = os.path.dirname(_fwd(script_path))
    out = _fwd(out_folder)
    while os.path.basename(out).lower() in ("genai", "upload", "download", "in", "out"):
        out = os.path.dirname(out)
    try:
        return "rel", _fwd(os.path.relpath(out, script_dir))
    except ValueError:
        return "abs", out


class InstallerApp(object):
    def __init__(self, root):
        self.root = root
        root.title("GenAI — Installer")
        root.resizable(False, False)
        existing = load_keys()
        pad = {"padx": 8, "pady": 3}
        row = 0

        tk.Label(root, text="Install GenAI node", font=("", 12, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(10, 2)); row += 1
        tk.Label(root, text="Copies to  %s  and stores keys + settings there." % nuke_home(),
                 fg="grey").grid(row=row, column=0, columnspan=3, sticky="w", padx=8); row += 1
        tk.Label(root, text="Keys & settings file:  %s" % keys_file(), fg="grey").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8); row += 1

        # ---- Quick start: no keys needed ----
        tk.Label(root, text="Quick start", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(10, 0)); row += 1
        tk.Label(
            root, justify="left",
            text="1. Click Install   2. Restart Nuke   3. Nodes > GenAI: the setup window opens,\n"
                 "log in with your Magnific account (once, in the browser). No API keys needed.",
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=8); row += 1

        # ---- API keys (optional, collapsed unless one is already stored) ----
        self.keys_open = tk.IntVar(value=1 if any(existing.get(n) for n, _l, _h in KEY_FIELDS) else 0)
        tk.Checkbutton(root, text="Enter my own API keys (optional: API mode, other providers)",
                       variable=self.keys_open, command=self._toggle_keys_panel).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 0)); row += 1
        self.keys_frame = tk.Frame(root)
        self.keys_frame.grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        self.key_entries = {}
        krow = 0
        for name, label, hint in KEY_FIELDS:
            tk.Label(self.keys_frame, text=label + ":").grid(row=krow, column=0, sticky="e", **pad)
            e = tk.Entry(self.keys_frame, width=50, show="*")
            e.insert(0, existing.get(name, ""))
            e.grid(row=krow, column=1, columnspan=2, sticky="w", **pad)
            self.key_entries[name] = e; krow += 1
            tk.Label(self.keys_frame, text=hint, fg="grey", wraplength=420, justify="left").grid(
                row=krow, column=1, columnspan=2, sticky="w", padx=8); krow += 1
        self.show_keys = tk.IntVar(value=0)
        tk.Checkbutton(self.keys_frame, text="Show keys", variable=self.show_keys,
                       command=self._toggle).grid(row=krow, column=1, sticky="w", padx=6); krow += 1
        if not self.keys_open.get():
            self.keys_frame.grid_remove()

        # ---- relative path ----
        tk.Label(root, text="Relative path (pipeline conform)", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(12, 0)); row += 1
        tk.Label(root, text="Example: Nuke script (.nk):").grid(row=row, column=0, sticky="e", **pad)
        self.script_entry = tk.Entry(root, width=50)
        self.script_entry.insert(0, existing.get("_example_script", ""))
        self.script_entry.grid(row=row, column=1, sticky="w", **pad)
        tk.Button(root, text="...", width=3, command=self._browse_script).grid(row=row, column=2, sticky="w"); row += 1
        tk.Label(root, text="Where GenAI/UPLOAD + GenAI/DOWNLOAD should be created:").grid(row=row, column=0, sticky="e", **pad)
        self.out_entry = tk.Entry(root, width=50)
        self.out_entry.insert(0, existing.get("_example_out", ""))
        self.out_entry.grid(row=row, column=1, sticky="w", **pad)
        tk.Button(root, text="...", width=3, command=self._browse_out).grid(row=row, column=2, sticky="w"); row += 1
        self.rel_var = tk.StringVar(value="")
        tk.Label(root, textvariable=self.rel_var, fg="#227722").grid(
            row=row, column=1, columnspan=2, sticky="w", padx=8); row += 1
        tk.Label(root, text="Leave both empty = a GenAI folder next to the .nk  (<script>/GenAI/UPLOAD + DOWNLOAD).",
                 fg="grey").grid(row=row, column=1, columnspan=2, sticky="w", padx=8); row += 1
        for e in (self.script_entry, self.out_entry):
            e.bind("<KeyRelease>", lambda _ev: self._update_rel())
        self._update_rel()

        # ---- video helper packages ----
        tk.Label(root, text="Video helper packages", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(12, 0)); row += 1
        with_fal = bool(existing.get("FAL_KEY"))
        already_present = check_video_helpers(sys.executable, with_fal)
        default_check = 0 if already_present else 1
        self.install_helpers = tk.IntVar(value=default_check)
        note = ("already installed" if already_present else
                "will be installed on click (opencv-python, imageio-ffmpeg; fal-client only with a fal key)")
        tk.Checkbutton(
            root, text="Install into this Python  (" + note + ")",
            variable=self.install_helpers,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=6); row += 1
        tk.Label(
            root, text="Uses  " + sys.executable + "  (the Python running this installer).",
            fg="grey",
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=26); row += 1

        tk.Button(root, text="Install", width=16, command=self._install).grid(
            row=row, column=1, sticky="w", padx=8, pady=(14, 6)); row += 1
        self.status = tk.Label(root, text="", fg="grey")
        self.status.grid(row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 10))

    def _toggle_keys_panel(self):
        if self.keys_open.get():
            self.keys_frame.grid()
        else:
            self.keys_frame.grid_remove()
        self.root.update_idletasks()

    def _toggle(self):
        show = "" if self.show_keys.get() else "*"
        for e in self.key_entries.values():
            e.config(show=show)

    def _browse_script(self):
        p = filedialog.askopenfilename(title="Example Nuke script", filetypes=[("Nuke", "*.nk"), ("All", "*.*")])
        if p:
            self.script_entry.delete(0, tk.END); self.script_entry.insert(0, p); self._update_rel()

    def _browse_out(self):
        p = filedialog.askdirectory(title="Folder for GenAI/UPLOAD + DOWNLOAD")
        if p:
            self.out_entry.delete(0, tk.END); self.out_entry.insert(0, p); self._update_rel()

    def _update_rel(self):
        s = self.script_entry.get().strip(); o = self.out_entry.get().strip()
        if s and o:
            mode, val = compute_output_rel(s, o)
            if mode == "rel":
                self.rel_var.set("→ relative base: %s   (creates GenAI/UPLOAD + GenAI/DOWNLOAD per shot)" % val)
            else:
                self.rel_var.set("→ fixed base (different drive): %s   (+ GenAI/UPLOAD + DOWNLOAD)" % val)
        else:
            self.rel_var.set("")

    def _install(self):
        try:
            dst = copy_package()
            patch_menu()
        except Exception as exc:
            messagebox.showerror("GenAI", "Copy failed:\n%s" % exc); return

        # Video-Helper-Pakete in DIESEN Python installieren (falls Checkbox an ist),
        # damit die Node cv2 & Co. spaeter garantiert findet.
        helper_msg = None
        if self.install_helpers.get():
            self.status.config(text="Installing video helper packages (can take 1-2 min) ...", fg="grey")
            self.status.update_idletasks()
            ok, msg = install_video_helpers(
                sys.executable, with_fal=bool(self.key_entries["FAL_KEY"].get().strip()))
            if not ok:
                helper_msg = msg
                # Kein return — Keys + Ordner werden trotzdem geschrieben. User kann
                # cv2 spaeter manuell nachinstallieren.

        data = load_keys()
        for name, e in self.key_entries.items():
            v = e.get().strip()
            if v:
                data[name] = v
            else:
                data.pop(name, None)
        # Video-Helper-Python-Pfad speichern — auch wenn die Checkbox aus war, damit
        # die Node einen deterministischen Pfad hat und nicht auf PATH raten muss.
        data["video_helper_python"] = _fwd(sys.executable)
        s = self.script_entry.get().strip(); o = self.out_entry.get().strip()
        data.pop("output_rel", None); data.pop("output_dir", None)
        data.pop("_example_script", None); data.pop("_example_out", None)
        if s and o:
            mode, val = compute_output_rel(s, o)
            data["output_rel" if mode == "rel" else "output_dir"] = val
            data["_example_script"] = _fwd(s)
            data["_example_out"] = _fwd(o)
        try:
            os.makedirs(nuke_home(), exist_ok=True)
            with open(keys_file(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            messagebox.showerror("GenAI", "Saving keys failed:\n%s" % exc); return

        if helper_msg:
            messagebox.showwarning(
                "GenAI",
                "Node installed, but video helper install FAILED:\n\n" + helper_msg +
                "\n\nYou can install manually later:\n" +
                sys.executable + " -m pip install " + " ".join(
                    VIDEO_HELPER_PACKAGES + (FAL_HELPER_PACKAGES if self.key_entries["FAL_KEY"].get().strip() else ())),
            )
            self.status.config(text="Installed. Video helpers FAILED — see message. Restart Nuke.", fg="#883300")
        else:
            messagebox.showinfo(
                "GenAI",
                "Installed.\n\nGenAI  ->  %s\nkeys/settings  ->  %s\nvideo helper Python  ->  %s\n\n"
                "Next: (re)start Nuke, then  Nodes > GenAI.\n"
                "The setup window opens by itself -- log in with your Magnific account there."
                % (dst, keys_file(), sys.executable))
            self.status.config(text="Done — restart Nuke.", fg="#227722")


def main():
    root = tk.Tk()
    InstallerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
