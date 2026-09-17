# YouTube Video-Gen Pipeline Orchestrator

A single Tkinter GUI that drives the 7 workers (script → audio → images →
subtitles → beat-align → edit → add-audio) end to end, with resumable
progress, automatic retries, per-step pre-flight checks, manual per-step
re-runs and a pause point after image generation (that worker hands off to a
browser extension and exits immediately).

It does **not** reimplement the workers - it shells out to the same CLI
commands you run by hand, in the same folders.

## 1. Layout

```
youtube-pipeline/
├── main.py
├── settings.json             <- created automatically on first run
├── orchestrator/
│   ├── config.py             settings.json shape + defaults
│   ├── jobs.py               the 7 steps, their commands and pre-flight checks
│   ├── state.py              resumable per-project state
│   ├── pipeline.py           execution engine (threads, retries, stop/resume)
│   └── gui.py                Tkinter front-end
├── script-gen/               ytscript (voiceover.md, beat.md, image_prompts.md)
├── audio-gen/                tts.py (Fish Audio)
├── image-gen/                autoimg.py (ChatGPT bulk image extension)
├── subtitle-gen/whisper.cpp/ whisper-cli
├── beat-gen/                 beatalign.py (LLM alignment -> beat.json)
├── editor/                   editor.py (animated render) + add_audio.py
└── out/                      one folder per project
```

## 2. Requirements

- Python 3.9+ with Tkinter (`python3 -m tkinter` should open a window;
  Debian/Ubuntu: `sudo apt install python3-tk`)
- Everything the workers need: `uv`, `ffmpeg` (+ `ffprobe`), whisper.cpp built
  at `subtitle-gen/whisper.cpp/build/bin/whisper-cli`, API keys in the
  workers' `.env` files.

The orchestrator itself is standard library only.

## 3. Run it

```bash
cd youtube-pipeline
python3 main.py
```

On first run this creates `settings.json` next to `main.py`. When a newer
version adds settings, the missing keys are filled in automatically without
touching your values. Open it any time with **Edit settings.json**.

## 4. Workflow

The project panel has three tabs. Everything you see in them is written into
the project **right before every run**, so you can change e.g. the animation
and just click *Run* on step 6 again.

**Project** - Title, output folder (auto-slug, editable), duration, optional
exact beat count, **aspect ratio** (`16:9`, `9:16`, `1:1`, `4:5`) and art
style. The aspect ratio is passed to the script generator (image prompt
prefix) and to the editor (frame size), so one setting targets YouTube,
Shorts/Reels/TikTok or square feeds.

**Script & Voice** - a free-form **custom instructions** box for the script
writer (structure, beat pacing, narrator persona, facts to include, things to
avoid ...). The text is saved as `custom_instructions.md` in the project and
injected into every ytscript prompt with higher priority than the tags. Plus
tone, depth and the Fish Audio voice reference ID.

**Video & Animation** - animation preset, transition, smoothness (easing),
fit (`cover` crops, `contain` letterboxes), fps, zoom strength, the image
folder (Browse...) and an optional *per-project subfolder* mode that tells
the browser extension to download into `<Downloads>/<folder name>/` so
projects never share images.

Animation presets (`editor.py --list-animations`):

| preset | effect |
|---|---|
| `none`, `fadein`, `fade` | static image, optional fade in / in+out |
| `zoom_in`, `zoom_out` | slow push in / pull back |
| `ken_burns` | push in towards a random edge or corner |
| `drift` | gentle 2.5D float: light zoom with a soft drift |
| `pan_left/right/up/down` | camera pan across the image |
| `slide_left/right/up/down` | image slides into place |
| `random` | a different preset for every scene (never the same twice in a row) |

A scene can override the global preset with an `"animation"` key in its
`beat.json` entry.

Then:

1. **Create / Load Project**. Existing projects are loaded (progress kept)
   and their saved settings appear in the GUI.
2. **START** runs steps 1 → 7. Each step is pre-checked first (missing
   `voiceover.md`, no images yet, whisper not built ...) and fails with a
   clear message instead of a worker traceback. Failed steps are retried
   automatically (`settings.retries`, default: script 1x, audio 2x, beat 2x).
3. **After step 3 the pipeline pauses.** Wait for the downloads, then
   **RESUME**. The editor pre-check compares image ids with beat ids and
   warns about missing or extra images before rendering.
4. **STOP** kills the current worker (whole process group). **RESUME**
   continues from the first step that isn't done, also after restarting
   the app. **Run** re-runs one step; **Reset** marks it pending again.
5. **Open project folder** / **Open final video** open `out/<slug>/`.

## 5. What is tracked

Every project gets `.pipeline_state.json` (step status, parameters, pause
flag; written atomically, corrupted files are backed up), `pipeline.log`
(everything every worker printed) and `custom_instructions.md`.

## 6. Worker CLI additions

- `ytscript ... --instructions "..."` / `--instructions-file notes.md`
- `editor.py --animation random --transition fade --smoothness ease_in_out --aspect 9:16 --fit cover --zoom 0.18 --seed 42`
  (`--dry-run` prints the ffmpeg command and filter graph)
- `beatalign.py --retries 2` re-asks the model with the validation error;
  if it keeps failing a proportional alignment is used (`--no-fallback`
  to abort instead). Output is a continuous timeline starting at 0.
- `add_audio.py --extend-video` holds the last frame until the narration ends.
- `tts.py --retries 3 --speed 0.95`

## 7. Customizing / extending

- **`jobs.py`** - how each command is built and pre-checked; add an 8th step
  by adding a builder and one entry to `JOBS`.
- **`config.py`** - the shape of `settings.json` and its defaults.
- **`pipeline.py`** - execution engine (threading, retries, stop/resume).
- **`state.py`** - resumable per-project state.
- **`gui.py`** - Tkinter front-end; presets are read from `jobs.py`.

The subtitle step writes `<project_dir>/<slug>.srt` and the beat aligner
reads the same path; adjust `build_subtitle_cmd` / `build_beat_cmd` if your
whisper.cpp build names its output differently.
