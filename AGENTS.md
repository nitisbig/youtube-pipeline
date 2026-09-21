# AGENTS.md — Developer & AI Agent Guide

Welcome to the **YouTube Video-Gen Pipeline** codebase. This document is written specifically for AI agents and developers to quickly understand the project architecture, operational workflows, coding standards, and extension points.

---

## 1. Architectural Philosophy: Modular, Clean & Self-Sufficient

This repository is engineered around **independent, decoupled worker modules** coordinated by a central orchestrator.

### Key Tenets:
1. **Self-Sufficiency & No Single Point of Failure (SPOF)**:
   - Each worker (`script-gen`, `audio-gen`, `image-gen`, `subtitle-gen`, `beat-gen`, `editor`) is an isolated CLI tool designed to execute independently in its own directory.
   - The orchestrator does **not** import worker code directly into memory. Instead, it runs workers as real external OS subprocesses with distinct process groups. If one worker crashes, memory corruption or exceptions cannot leak into the orchestrator or kill other jobs.
   - Every step communicates strictly through explicit filesystem contracts (files in `out/<project-slug>/`).
   - Upstream expensive assets (such as LLM generation, TTS voice synthesis, or downloaded images) are strictly preserved on disk. If a downstream step (e.g. video editing or subtitle burning) fails or needs parameter tuning, it can be re-run in isolation without re-incurring TTS costs or re-generating images.

2. **Graceful Degradation & Fallbacks**:
   - **Hardware Acceleration**: GPU acceleration (NVIDIA NVENC, Intel/AMD VAAPI, Apple VideoToolbox, Intel QSV) probes hardware capability at runtime. If GPU encoding fails, it transparently falls back to CPU encoding (`libx264`).
   - **Whisper Subtitles**: Whisper CLI attempts GPU execution (`-dev 0`) and automatically falls back to CPU (`-ng`) if GPU support is absent.
   - **Beat Alignment**: If the LLM alignment fails validation across configured retries, `beatalign.py` automatically falls back to a deterministic proportional word-count distribution so the pipeline never stalls.
   - **Script Generation**: Chunking mechanisms with single-item repair calls and local visual prompt synthesis prevent hallucination or miscount halts.

3. **Resilience & State Persistence**:
   - Step progress is written atomically to `.pipeline_state.json` on every state transition (`pending` -> `running` -> `success` / `failed` / `stopped`).
   - The pipeline can be stopped, resumed, or recovered across application restarts without data loss.

---

## 2. Directory Structure & Layout

```
youtube-pipeline/
├── main.py                     # GUI application entry point (Tkinter)
├── settings.json               # Global configuration & defaults (auto-generated if missing)
├── README.md                   # User-facing documentation
├── example-cmd-full.md         # Full standalone CLI execution examples
├── AGENTS.md                   # This guide for AI agents
│
├── orchestrator/               # Central pipeline execution & UI (Standard Library only)
│   ├── config.py               # Settings loader, defaults schema & migration
│   ├── jobs.py                 # Definition of the 9 pipeline steps, builders & prechecks
│   ├── state.py                # Atomic per-project state persistence (.pipeline_state.json)
│   ├── pipeline.py             # Multithreaded execution engine (subprocesses, retries, logs)
│   └── gui.py                  # Tkinter desktop interface
│
├── script-gen/                 # Step 1: Script, Beats & Image Prompts Generator
│   ├── ytscript/               # Modular script generator package (stdlib only)
│   │   ├── cli.py              # CLI entry point
│   │   ├── models.py           # Core dataclasses: ScriptRequest, Beat, Outline, Script
│   │   ├── pipeline.py         # Generation stages (research -> outline -> beats -> prompts)
│   │   ├── providers/          # LLM integrations (OpenAI compatible, Anthropic, Mock)
│   │   └── generators/         # Chunked outline, beat, and prompt generation
│   └── docs/                   # Detailed package architecture and extension docs
│
├── audio-gen/                  # Steps 2 & 3: Voiceover Generation & Mastering
│   ├── tts.py                  # Step 2: Fish Audio API client with retry & tempfile safety
│   └── enhancer.py             # Step 3: Pure FFmpeg voice mastering (EQ, de-esser, EBU R128)
│
├── image-gen/                  # Step 4: Visual Generation Automation
│   └── autoimg.py              # Raw WebSocket Chrome DevTools Protocol automation for browser
│
├── subtitle-gen/               # Steps 5 & 9: Subtitle Transcription & Stylized Burn-in
│   ├── generate_subtitles.py   # Step 5: whisper.cpp runner (16kHz audio extract -> whisper-cli)
│   ├── subtitle_worker.py      # Step 9: Advanced SubStation Alpha (.ass) generator & FFmpeg burn-in
│   └── whisper.cpp/            # High-performance C/C++ Whisper inference engine
│
├── beat-gen/                   # Step 6: Beat & Timeline Alignment
│   └── beatalign.py            # Aligns beat.md cues with .srt timestamps (LLM + proportional fallback)
│
├── editor/                     # Steps 7 & 8: Video Assembly & Audio Muxing
│   ├── editor.py               # Step 7: Stills + beat.json -> animated MP4 (pan/zoom/drift/transitions)
│   ├── add_audio.py            # Step 8: Final audio muxing + last-frame hold extension
│   └── ffmpeg_gpu.py           # Shared GPU probe and hardware acceleration config builder
│
├── sfx-adder/                  # Step 9: Beat Transitions & Contextual Sound Design
│   ├── sfx_adder.py            # Step 9: Beat pop synchronizer & LLM semantic audio design
│   ├── library/                # Sound effects library (soft-pop.mp3, camera-flash, woosh, etc.)
│   └── .env                    # Groq/OpenAI configuration for semantic sound design
│
└── out/                        # Default output directory for projects
    └── <slug>/                 # Single folder containing all project artifacts and state
```

---

## 3. The 10 Pipeline Steps & Contracts

Every step reads from and writes to the project directory (`out/<slug>/`). The table below outlines the precise data flow:

| Step # | Job ID | Label | Working Directory | Primary Command / Binary | Inputs | Outputs |
|---|---|---|---|---|---|---|
| **1** | `script` | Script Generator | `script-gen/` | `python3 -m ytscript` | Topic, duration, tone, custom instructions | `voiceover.md`, `beat.md`, `image_prompts.md` |
| **2** | `audio` | Audio Generator | `audio-gen/` | `python3 tts.py` | `voiceover.md`, voice reference ID | `audio.mp3` |
| **3** | `enhance` | Audio Enhancer | `audio-gen/` | `uv run enhancer.py` | `audio.mp3`, preset | `enhanced_audio.mp3` |
| **4** | `image` | Image Generator | `image-gen/` | `uv run autoimg.py` | `image_prompts.md` | Hand-off to Brave/Chrome (downloads images into folder) |
| **5** | `subtitle` | Subtitle Generator | `subtitle-gen/whisper.cpp` | `ffmpeg` + `whisper-cli` (or `generate_subtitles.py`) | `enhanced_audio.mp3`, whisper model | `<slug>.srt` |
| **6** | `beat` | Beat Aligner | `beat-gen/` | `uv run python beatalign.py` | `beat.md`, `<slug>.srt` | `beat.json` |
| **7** | `editor` | Video Editor | `editor/` | `python3 editor.py` | `beat.json`, downloaded images | `<slug>.mp4` |
| **8** | `audio_add` | Audio Adder | `editor/` | `python3 add_audio.py` | `<slug>.mp4`, `enhanced_audio.mp3` | `final.mp4` |
| **9** | `sfx` | SFX Adder | `sfx-adder/` | `python3 sfx_adder.py` | `final.mp4`, `<slug>.srt`, `beat.json`, `voiceover.md`, `library/` | `final_sfx.mp4`, `sfx_cues.json` |
| **10** | `subtitle_burn` | Subtitle Worker | `subtitle-gen/` | `python3 subtitle_worker.py` | `final_sfx.mp4` (or `final.mp4`), `<slug>.srt` | `final_subtitled.mp4` |

### Important Pipeline Rules:
- **Enhanced Audio Standard**: Step 3 produces `enhanced_audio.mp3`. All downstream audio consumers (Step 5 Subtitles, Step 8 Audio Adder) MUST consume `enhanced_audio.mp3`, keeping raw `audio.mp3` untouched.
- **Auto-Pause Point**: Step 4 (`image`) triggers the browser extension via DevTools protocol and terminates immediately. It does **not** wait for downloads to finish. The orchestrator automatically pauses after Step 4 so the user or agent can verify that images are downloaded before resuming Step 5.
- **Continuous Timeline**: Step 7 (`editor.py`) guarantees a contiguous visual timeline: beat gaps are held by repeating the previous image, and overlaps are clamped to the subsequent beat start time.
- **Lossless SFX Passthrough**: Step 9 (`sfx_adder.py`) mixes beat pops and contextual sound effects directly into the audio stream with `-c:v copy`, executing in seconds without degrading or re-encoding visual frames.
- **Adaptive Subtitle Burning**: Step 10 (`subtitle_burn`) burns subtitles onto `final_sfx.mp4` if present, falling back to `final.mp4` if the SFX step was skipped.

---

## 4. Coding Style, Standards & Conventions

When modifying or adding code to this codebase, follow these non-negotiable standards:

### 4.1. Modularity & Zero Sibling Coupling
- **Never create cross-module Python imports between worker folders.** For example, `editor` must never `import ytscript`, and `audio-gen` must never depend on `beat-gen`.
- Shared utility modules (such as `ffmpeg_gpu.py`) should either remain isolated within their domain or be accessed via standard CLI interfaces and environment variables.
- Standard Library is preferred wherever possible (e.g. `urllib.request` over `requests` in `script-gen` and `autoimg.py`).

### 4.2. Atomic File Writes
- Never write directly to a destination path if an interrupted write could leave a corrupted file.
- Use the pattern: write to a temporary sibling file (e.g. `f"{target}.tmp"`), flush/sync, and atomically move using `os.replace(temp_path, target_path)`.

### 4.3. Hardware Acceleration & Graceful Fallback
- Any component running FFmpeg or deep learning inference (`editor.py`, `add_audio.py`, `subtitle_worker.py`, `generate_subtitles.py`) must accept a `--gpu true/false` flag.
- Always utilize `ffmpeg_gpu.py` (`probe_gpu_encoder` / `get_video_encoder_config`) to detect NVENC, VAAPI, VideoToolbox, or QSV.
- If hardware initialization fails, catch the exception, log a warning to `stderr`, and seamlessly fall back to CPU (`libx264`).

### 4.4. CLI and Output Hygiene
- **stdout is for data/clean output; stderr is for diagnostic chatter/logging.**
- Use `argparse` with explicit types and descriptive help messages.
- Use explicit POSIX exit codes:
  - `0`: Success
  - `1`: General runtime error
  - `2`: Invalid CLI arguments / syntax
  - `64`–`78`: Standard sysexits (e.g., config error, missing input file)
- Never allow unhandled tracebacks to escape to top-level CLI without clean error formatting.

### 4.5. Fast Pre-flight Checks
- The orchestrator validates inputs **before** spawning a subprocess (`precheck` functions in `orchestrator/jobs.py`).
- If you add a new input requirement or step, implement a corresponding pre-flight check in `orchestrator/jobs.py` to prevent spawning doomed processes.

---

## 5. Guide for AI Agents: How to Perform Common Tasks

### 5.1. Adding a New Animation Preset or Video Filter
1. **Implement Effect in `editor/editor.py`**:
   - Add the preset name to `MOTION_ANIMATIONS` or `STATIC_ANIMATIONS`.
   - Update `build_filtergraph()` to construct the FFmpeg zoompan/filter expression for each scene.
2. **Update Orchestrator Presets**:
   - Add the new preset name to `ANIMATIONS` in `orchestrator/jobs.py`.
   - The GUI dropdown and validation logic will automatically pick it up.
3. **Test Standalone**:
   ```bash
   python3 editor/editor.py --imagesource /path/to/imgs --beatpath /path/to/beat.json \
       --out /tmp/test.mp4 --animation <new_preset> --dry-run
   ```

### 5.2. Adding a Subtitle Style or Animation
1. **Implement Style in `subtitle-gen/subtitle_worker.py`**:
   - Add style name to `STYLES` or animation name to `ANIMATIONS`.
   - Define styling rules (font, primary/secondary colors, border, shadow, margins) in `build_ass_header()` or `format_event()`.
2. **Update Orchestrator Constants**:
   - Add the identifier to `SUBTITLE_STYLES` or `SUBTITLE_ANIMATIONS` in `orchestrator/jobs.py`.
3. **Test Standalone**:
   ```bash
   python3 subtitle-gen/subtitle_worker.py --video /path/to/video.mp4 --srt /path/to/sub.srt \
       --out /tmp/subtitled.mp4 --style <new_style> --animation <new_anim>
   ```

### 5.3. Adding a New Pipeline Step
1. **Create the Worker CLI**:
   - Place the script in an appropriate subfolder (or create a new one).
   - Ensure it accepts CLI flags for input/output paths and returns exit code `0` on success.
2. **Register in `orchestrator/jobs.py`**:
   - Define `build_<job>_cmd(ctx, settings)` returning `(cmd_list, job_cwd, is_shell)`.
   - Define `precheck_<job>(ctx, settings)` returning `(errors_list, warnings_list)`.
   - Insert the step into `JOBS` list at the appropriate sequence index.
3. **Register Directory in `orchestrator/config.py` & `settings.json`**:
   - Add the default directory to `DEFAULT_SETTINGS["job_dirs"]`.
   - Add retry settings to `DEFAULT_SETTINGS["retries"]`.
4. **Update `orchestrator/gui.py`** (if new UI controls or tabs are needed).

### 5.4. Debugging & Troubleshooting Issues
- **Subprocess Failures**: Inspect `<project_dir>/pipeline.log` to view the raw stdout and stderr of all executed steps.
- **State Inconsistencies**: Check `<project_dir>/.pipeline_state.json`. If corrupted, the state loader automatically backs it up as `.pipeline_state.json.corrupt.<timestamp>` and re-initializes safe defaults.
- **Dry-run FFmpeg Commands**: `editor.py` supports `--dry-run` to print the generated FFmpeg command and complex filtergraph without rendering video.

---

## 6. Testing & Validation Checklist

Before committing any changes, verify:
- [ ] Worker runs standalone without imports from sibling folders.
- [ ] No regression on CPU-only machines (verify GPU fallback path).
- [ ] Atomic file creation (no partial writes on SIGINT / kill).
- [ ] Pre-flight checks in `orchestrator/jobs.py` detect missing inputs before running.
- [ ] `python3 main.py` opens without errors, loads existing projects, and reflects any new presets.
