# GenAI — Generative AI node for Foundry Nuke

A native Nuke gizmo that generates images and video through your own Magnific login
(or your own API keys), directly inside the comp. Color accurate (ACES), shot aware, standalone.

---

## 1. Install (one time)

> **Need Python first?** The installer is a Python program (Nuke's built-in Python
> does not count). Get a standard one from **python.org** — on Windows, tick
> **"Add python.exe to PATH"** during setup. Use the python.org build, not the
> Microsoft Store / Homebrew one, so **Tkinter** (the installer window) is included.
> On macOS run `python3 install.py`; the `.bat` file is Windows-only.

1. Copy the whole `NukeAINode` folder somewhere on your machine.
2. Run the installer with any Python:
   ```
   python install.py
   ```
   A small window opens. It:
   - copies `GenAI/` into your `~/.nuke/` and adds the **Nodes → GenAI** menu entry,
   - optionally stores your own **API keys** (not needed with the Magnific login),
   - asks for one **example `.nk` script** and the **base folder** where its GenAI
     folder should go, and works out the *relative* path so it applies to every
     shot automatically (see section 3).
3. Click **Install**, then restart Nuke.
4. In Nuke: **Nodes → GenAI**. The **setup window opens by itself** the first time:
   click **Anmelden** (log in), confirm the code once in the browser — done. You need
   a Magnific account with credits and nothing else: no API key, no other account.

Keys and settings are saved locally in `~/.nuke/GenAI_keys.json` (this file never
leaves your machine — do not share it). Re-run `install.py` anytime to change keys
or paths. The setup window also opens by itself whenever the login or a key is missing
or has expired (manually: `import GenAI.setup_panel as S; S.show_setup()` in the Script
Editor).

---

## 2. Accounts and API keys

**Default (recommended): one Magnific account, logged in inside Nuke (interface *MCP*).**
No API keys, no other accounts. Credits of your own Magnific account are used — each
person needs their own login, do not share one.

Everything below is **optional**: your own API keys for the few models that exist only in the
*API* interface (Wan 2.7 Video Edit, Alpha Gen, Upscale). Enter them in the installer or in the
setup window. **Only models whose key you entered (or that the Magnific login covers) appear in
the Model dropdown.**

### Where to get each key

| Key | Needed for | Get it at |
|-----|------------|-----------|
| `MAGNIFIC_API_KEY` | Wan 2.7 Video Edit | magnific.com → account → **API** |
| `FAL_KEY` | **file uploads** for Wan 2.7 Video Edit | fal.ai → **Keys** (format `id:secret`) |
| `LTX_API_KEY` | Alpha Gen, Upscale (LTX) | console.ltx.io (LTX **Developer Console**, separate from LTX Studio; buy credits first, min. USD 5) |
| `OPENAI_API_KEY` | none at the moment | platform.openai.com → **API keys** |
| `GOOGLE_API_KEY` | none at the moment | aistudio.google.com → **Get API key** |

> **Note:** `FAL_KEY` uploads your input video to a public URL for Wan 2.7 Video Edit (the
> Magnific API only takes URLs). The *MCP* interface uploads through Magnific itself and never
> needs it. The installer adds the `fal-client`
> package only if you enter a fal key.

### Manual alternative
Create/edit `~/.nuke/GenAI_keys.json`:
```json
{
  "MAGNIFIC_API_KEY": "your-magnific-key",
  "FAL_KEY": "id:secret",
  "OPENAI_API_KEY": "sk-...",
  "output_dir": ""
}
```
Environment variables of the same name also work and take priority.

---

## 3. Output folder (relative, set once)

In the installer you point at one example `.nk` and the base folder where its
GenAI folder should go. The installer stores the **relative offset** between them,
so it works for *every* shot without a fixed path.

Example: script `…/PROJECT/SHOTS/SH010/NK/comp.nk` + base `…/PROJECT/SHOTS/SH010/FOOTAGE`
→ offset `../FOOTAGE` (so the GenAI folder is created at `…/PROJECT/SHOTS/SH010/FOOTAGE`).
For any other shot, a `GenAI` folder is created under that base automatically.
Uploaded inputs go to `…/GenAI/UPLOAD`, results to `…/GenAI/DOWNLOAD`, and each run
is logged to `generation_log.txt` next to them.

Resolution order per generation:
1. the node's own **Output Folder** knob (fixed path, if set),
2. the **relative offset** from the installer,
3. otherwise automatic: `GenAI/UPLOAD` + `DOWNLOAD` next to the `.nk`.

Leaving both installer path fields empty keeps option 3 (fully automatic).

---

## 4. Models

| Model | Provider | Type | Notes |
|-------|----------|------|-------|
| Wan 2.7 Video Edit | Magnific API | video -> video | **API interface**, needs `MAGNIFIC_API_KEY` + `FAL_KEY` |
| Alpha Gen | LTX | video -> matte | `in1` = video over the comp range (max 3072 frames, up to 3840x2160). Result = grayscale matte as sequence; the ProRes 4444 cutout `.mov` (alpha) is saved next to it in `GenAI/DOWNLOAD`. USD 0.10 / 0.25 / 1.00 per input second (short side up to 1080 / 1440 / 2160) |
| Upscale | LTX | video -> video | `in1` = video; mode = target 1080p / 4K / 8K (USD 0.08 / 0.30 / 1.20 per input second; max ~80 s / 20 s / 8 s) |
| Generate Only | local | export | no API call: writes in1..in4 into one new folder `GenAI/UPLOAD/<date_time>_<node name>/` — mode **Video** = `in1.mp4` over the comp range (comp fps) plus `in2..in4.png` from the current frame (in1 is always the video), mode **Still frame** = `in1.png..` from the current frame (the single frames are deleted after encoding) |

### Interface: API or MCP

The **MCP / API** dropdown right next to *Model* switches the *Model* list between
**MCP** (Magnific's MCP server, the default) and **API**. The API list holds only what MCP does not
offer: **Wan 2.7 Video Edit** (Magnific API key), **Alpha Gen** and **Upscale** (LTX key); "Generate
Only" is in both lists. All other models (Seedance, Kling, Gemini Omni, MiniMax, GPT, Nano Banana,
Seedream) are MCP-only; nodes that still carry an old API entry of one of them switch to the MCP
model automatically when the script opens.

MCP models: GPT 2.6, Nano Banana 2.1, Seedream 5.5 / 5 Pro (images); Seedance 2.5 / 2.0, Kling 4.0 /
4.0 Flash / 3.0, Gemini Omni 1.2 Flash, MiniMax H3 (video), Video Depth. **Video Modify** is the mode **Modify** of each video
model (default mode of Kling 4.0): `in1` = footage over the comp frame range, `in2..in4` = reference
images; the result keeps length and fps.

**Video Depth** (MCP, Video Depth Anything): `in1` = footage over the comp frame range (max 30 s /
200 MB). Result = a per-frame, temporally consistent depth video with the same length and fps
(near = bright, far = dark). The Mode picks the colormap: **Grayscale** (for compositing), Turbo,
Inferno, Viridis. No prompt, no extra key — it uses the Magnific login. Those results are 8-bit videos.
Mode **Float (EXR)** returns the float depth instead: an EXR sequence (`R=G=B`, relative depth, near =
high like `depth.Z`, not metres) without banding, for ZDefocus and similar. It is big (~25 MB per 1080p
frame) and Magnific keeps the float data only a few minutes, so Nuke downloads it right after the job
(`GenAI/DOWNLOAD/<job>_depth_exr/depth.####.exr`). If the server has no float data you get the 8-bit
video and a note. No Display-transform is applied to it.

* **Login:** the setup window (opens by itself on first use) > Anmelden — confirm the code once in the browser
  (OAuth, no API key). The session lasts ~30 days and renews itself while you use it
  (`~/.nuke/GenAI_mcp.json`). Without login the MCP list only shows "Generate Only".
* **Credits:** MCP bills the **personal Magnific account you log in with**, not the Enterprise API
  key (and "Unlimited" does not apply). Check the studio agreement before using it on jobs.
* Local files are uploaded to Magnific as hidden creations; guide videos are encoded locally.
* Nodes from older scripts are upgraded automatically when the script is opened (prompt, mode,
  parameters and history are kept).

The rights flag (🟢/🟡/🔴) and an estimated cost show for each model. Red-flagged
models (e.g. Kling) ask for confirmation before generating.

---

## 5. Video helper Python (cv2 + fal-client)

Video models need a Python (not Nuke's) that has **`opencv-python`**,
**`fal-client`** and **`imageio-ffmpeg`** installed — used to transcode results to
frames and to encode/upload guide/reference videos (imageio-ffmpeg provides an
h264 encoder that video models like Kling require):
```
pip install opencv-python fal-client imageio-ffmpeg
```
The node auto-detects it (via `PATH`, common install paths, or the env var
`GENAI_CV2_PYTHON` pointing at such a `python.exe`). Image-only use does not
need this.

---

## 6. Usage

1. **GenAI Setup…** → enter keys (first time only).
2. Save your `.nk` in the shot's `NK/` folder (for automatic output paths).
3. **Nodes → GenAI**, pick a model, connect inputs to `in1…in4` (the *Inputs* hint
   shows their role per model), write a prompt (`@ref1…@ref4` refer to the inputs;
   **Expand editor** opens a large prompt window).
4. **Generate.** Watch the status; the result loads back into the node in your
   comp's color space. Use **History** to switch between generations and
   **Extract to Read** to bake one into a standalone Read.
