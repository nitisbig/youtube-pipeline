# SFX Adder Worker

The **SFX Adder** worker enhances the final video by automatically layering beat-synchronized sound effects and contextually intelligent audio cues from a local sound effects library.

---

## Capabilities

1. **Beat Transition Pops**:
   - Reads `beat.json` (or `beat.md`).
   - Automatically synchronizes a tactile `soft-pop.mp3` sound effect at every scene / image beat transition.
   - Smoothly handles first-frame transitions (toggled via `--include-first-beat`).

2. **Semantic & Narrative SFX**:
   - Reads the subtitle timing (`<slug>.srt`) and voiceover narrative (`voiceover.md`).
   - Discovers any audio file (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`) in `library/`.
   - Uses an OpenAI-compatible LLM (e.g. Groq `qwen/qwen3.8-27b`) configured in `.env` to analyze the narrative and identify high-impact moments that naturally call for sound design (e.g., `camera-flash`, `woosh`, `key-collect`, `Perkutut-bird`, `acceptance`).
   - **Intelligent Deterministic Fallback**: If the LLM API is unavailable, network is offline, or rate-limited, the worker automatically falls back to deterministic keyword matching derived from the SFX file names so the pipeline never fails or stalls.

3. **Lossless, Blazing-Fast FFmpeg Mixing**:
   - Generates an atomic FFmpeg filtergraph using `adelay` and `amix` (`normalize=0` to preserve voiceover loudness).
   - Maps video using `-c:v copy`, taking mere seconds (even on 4K multi-minute videos) without re-encoding video frames.
   - Exports `sfx_cues.json` so you can inspect, review, or manually tune every scheduled sound event.

---

## Standalone CLI Usage

```bash
python3 sfx-adder/sfx_adder.py \
    --video out/my-project/final.mp4 \
    --srt out/my-project/my-project.srt \
    --beat out/my-project/beat.json \
    --voiceover out/my-project/voiceover.md \
    --library sfx-adder/library \
    --out out/my-project/final_sfx.mp4 \
    --cues-out out/my-project/sfx_cues.json \
    --sfx-volume 0.5 \
    --pop-volume 0.4
```

### CLI Arguments

| Flag | Default | Description |
|---|---|---|
| `--video` | *(Required)* | Input video file (e.g. `final.mp4`) |
| `--srt` | *(Required)* | Subtitle file (`<slug>.srt`) |
| `--beat` | `beat.json` | Beat timings file |
| `--voiceover` | `voiceover.md` | Narration text file |
| `--library` | `sfx-adder/library` | Folder containing sound effect audio files |
| `--out` | `final_sfx.mp4` | Output video path |
| `--cues-out` | `sfx_cues.json` | Path where scheduled timeline JSON is saved |
| `--pop-volume` | `0.4` | Volume scale for beat pops (0.0 to 1.0) |
| `--sfx-volume` | `0.5` | Volume scale for semantic sound effects (0.0 to 1.0) |
| `--include-first-beat` | `False` | Play beat pop at timestamp 0.0s (default starts from beat 2) |
| `--min-interval` | `3.0` | Minimum seconds between semantic sound effects |
| `--dry-run` | `False` | Inspect scheduled timeline without running FFmpeg |
| `-v`, `--verbose` | `False` | Show verbose diagnostic output |

---

## Sound Library Conventions

Place any `.mp3`, `.wav`, or `.ogg` sound files inside `sfx-adder/library/`:
- `soft-pop.mp3`: Dedicated sound for image/scene cuts.
- Semantic sounds: Name them descriptively (e.g., `camera-flash.mp3`, `woosh.mp3`, `key-collect.mp3`, `Perkutut-bird.mp3`, `acceptance.mp3`). The worker uses these names to prompt the LLM or infer keyword triggers.
