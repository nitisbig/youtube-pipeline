# YouTube Video-Gen Pipeline Orchestrator

A single Tkinter GUI that drives your 7 existing workers (script → audio →
images → subtitles → beat-align → edit → add-audio) end to end, with
resumable progress, manual per-step retriggering, and a pause point after
image generation (since that worker hands off to a browser extension and
exits immediately).

It does **not** reimplement any of your workers — it only shells out to
the exact CLI commands you already run by hand, in the same folders, with
the same flags.

## 1. Where this goes

Drop these files into the root of your existing pipeline, next to your
worker folders:

```
youtube-pipeline/
├── main.py                 <- new
├── settings.json            <- created automatically on first run
├── orchestrator/            <- new
│   ├── __init__.py
│   ├── config.py
│   ├── jobs.py
│   ├── state.py
│   ├── pipeline.py
│   └── gui.py
├── script-gen/               <- your existing worker
├── audio-gen/                <- your existing worker
├── image-gen/                <- your existing worker
├── subtitle-gen/whisper.cpp/ <- your existing worker
├── beat-gen/                 <- your existing worker
├── editor/                   <- your existing worker (editor.py + add_audio.py)
└── out/                      <- created automatically, one folder per project
```

## 2. Requirements

- Python 3.9+
- Tkinter (`python3 -m tkinter` should open a blank window). On Debian/Ubuntu,
  if it's missing: `sudo apt install python3-tk`
- Everything your 7 workers already need (`uv`, `ffmpeg`, `whisper.cpp`
  built at `subtitle-gen/whisper.cpp/build/bin/whisper-cli`, etc.) — the
  orchestrator assumes those already work when you run them by hand.

No extra pip packages are required for the orchestrator itself — it's
standard library only (`tkinter`, `subprocess`, `threading`, `json`).

## 3. Run it

```bash
cd youtube-pipeline
python3 main.py
```

On first run this creates `settings.json` next to `main.py`, pre-filled
with the defaults from your example commands (tone, depth, palette,
keywords, avoid, reference-id, animation, image source, whisper model
path, and the sub-folder each worker runs from). `pipeline_root` is
auto-detected as the folder `main.py` lives in.

Open it any time with the **"Edit settings.json"** button in the GUI, or
in a text editor. Nothing needs to be recompiled — it's read fresh each
time you create a project.

## 4. Workflow

1. **Title / Duration / Art style** — the 3 fields exposed in the GUI.
   Everything else (tone, depth, palette, keywords, avoid, reference-id,
   animation, image source) comes from `settings.json`'s `"defaults"`
   block and gets snapshotted into the project the moment you create it,
   so later edits to `settings.json` won't retroactively change a
   project that's already in progress.
2. **Output folder name** auto-fills as a slug of the title (editable) —
   this becomes `out/<slug>/`, matching `-o` in your script-gen command.
3. Click **Create / Load Project**. If a project with that folder name
   already exists, it's loaded (its saved progress is *not* reset).
4. Click **START**. The pipeline runs steps 1→7 in order:
   1. Script Generator
   2. Audio Generator
   3. Image Generator
   4. Subtitle Generator
   5. Beat Aligner
   6. Video Editor
   7. Audio Adder
5. **After step 3 (Image Generator) it always pauses automatically.**
   That worker opens Brave, triggers the download extension, and exits
   immediately — long before the images actually exist. Verify the
   images finished downloading, then click **RESUME** to continue with
   subtitles → beat-align → edit → add-audio.
6. If a step fails (non-zero exit code), the pipeline halts there too.
   Fix whatever's wrong (in that worker's own folder/config) and click
   **RESUME** — it restarts from the first step that hasn't succeeded,
   it does not re-run steps already marked done.

### STOP / RESUME / QUIT

- **STOP** kills the currently-running worker (its whole process group,
  not just the top-level shell — matters for the subtitle step, which
  is a `mktemp && ffmpeg && whisper-cli` chain) and halts the sequence.
- **RESUME** restarts the sequence from the first step that isn't marked
  successful yet — whether that's because you stopped it, a step
  failed, or you closed the app after the auto-pause and reopened it
  later. Progress is stored on disk, not in memory, so this works even
  across app restarts: pick the project from **Existing projects** →
  **Load Selected**, then **RESUME**.
- **QUIT** stops any running worker (asks first) and closes the app.

### Manual per-step "Run" buttons

Each of the 7 rows has its own **Run** button. Use it to re-run a single
worker in isolation — e.g. you tweaked something in `editor/editor.py`
and just want to redo step 6 without touching the others, or you want to
regenerate subtitles after re-recording audio. It uses the same project
context (same `out/<slug>/` paths) as the automated run. You can't use a
manual Run while the automated sequence is running — Stop it first.

## 5. Where everything is tracked

Every project (`out/<slug>/`) gets two files the orchestrator manages
for you:

- `.pipeline_state.json` — status of each of the 7 steps
  (`pending` / `running` / `success` / `failed` / `stopped`), the
  parameters the project was created with, and whether it's paused
  awaiting manual verification. This is what makes Resume work after
  closing the app.
- `pipeline.log` — every line of stdout/stderr from every worker run for
  that project, in order, so you have a full history without needing
  the GUI open.

## 6. Customizing / extending

Everything is in `orchestrator/`:

- **`jobs.py`** — the 7 steps and exactly how each CLI command is built.
  If a worker's flags change, or you add an 8th step, this is the only
  file that needs new command-building logic (plus one entry in the
  `JOBS` list — order in that list is execution order).
- **`config.py`** — the shape of `settings.json` and its first-run
  defaults.
- **`pipeline.py`** — the execution engine (threading, subprocess
  handling, stop/resume/pause logic). Shouldn't need to change unless
  you want different pause/retry behavior.
- **`state.py`** — the resumable per-project state file.
- **`gui.py`** — the Tkinter front-end. Pure presentation; all the real
  logic lives in `pipeline.py`.

## 7. One assumption worth double-checking

The subtitle step calls `whisper-cli ... -osrt -of "<project_dir>/<slug>"`,
so the `.srt` is expected at `<project_dir>/<slug>.srt`, and the Beat
Aligner step is wired to read from that same path. Your two example
commands used slightly different folder/file names for this
(`dog-story` vs `dog-sad-story`), so if your build of whisper.cpp names
the output file differently, adjust `build_subtitle_cmd` and
`build_beat_cmd` in `orchestrator/jobs.py` (both are short, ~10 lines
each) to match.
