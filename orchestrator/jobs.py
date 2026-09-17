"""
Defines the 7 pipeline steps and how to build the exact CLI command for
each one, given a project context (ctx) and the loaded settings.

Each builder returns a 3-tuple:
    (command, cwd_relative_to_pipeline_root, is_shell)

- command is a list of argv tokens (normal case), or a single shell
  string when is_shell=True (needed for the subtitle step, which chains
  several commands with mktemp / && the way you run it by hand).
- cwd_relative_to_pipeline_root is looked up from settings["job_dirs"].

Each step may also define a `precheck(ctx, settings)` that returns
`(errors, warnings)` - two lists of strings. Errors stop the step before a
process is even spawned (with a message that says exactly which input is
missing); warnings are only logged.

To change how a step is invoked (new flag, different binary, etc.) edit
the matching build_*_cmd function below - nothing else needs to change.
"""

import json
import re
import shlex
from pathlib import Path

# --------------------------------------------------------------------------- #
# Presets shared with the GUI. Keep in sync with editor/editor.py.
# --------------------------------------------------------------------------- #

ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:5"]
ANIMATIONS = [
    "random",
    "none", "fadein", "fade",
    "zoom_in", "zoom_out", "ken_burns", "drift",
    "pan_left", "pan_right", "pan_up", "pan_down",
    "slide_left", "slide_right", "slide_up", "slide_down",
]
TRANSITIONS = ["none", "fadein", "fade"]
SMOOTHNESS = ["linear", "ease_in", "ease_out", "ease_in_out"]
FIT_MODES = ["cover", "contain"]
DEPTHS = ["light", "balanced", "deep"]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
INSTRUCTIONS_FILENAME = "custom_instructions.md"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _job_dir(job_id, settings):
    return settings.get("job_dirs", {}).get(job_id, ".")


def _python(settings):
    return settings.get("python_bin") or "python3"


def _uv(settings):
    return settings.get("uv_bin") or "uv"


def _clean(value):
    return str(value if value is not None else "").strip()


def _as_list(value):
    """Accept a list, a single string, or nothing."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _pick(value, choices, default):
    value = _clean(value)
    return value if value in choices else default


def _number(value, default, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def project_paths(ctx):
    """Every file the pipeline reads or writes for one project, in one place."""
    project_dir = Path(ctx["project_dir"])
    slug = _clean(ctx.get("slug")) or project_dir.name
    image_source = _clean(ctx.get("image_source"))
    return {
        "project_dir": project_dir,
        "slug": slug,
        "voiceover": project_dir / "voiceover.md",
        "beat_md": project_dir / "beat.md",
        "image_prompts": project_dir / "image_prompts.md",
        "instructions": project_dir / INSTRUCTIONS_FILENAME,
        "audio": project_dir / "audio.mp3",
        "srt_prefix": project_dir / slug,
        "srt": project_dir / f"{slug}.srt",
        "beat_json": project_dir / "beat.json",
        "video": project_dir / f"{slug}.mp4",
        "final": project_dir / "final.mp4",
        "image_source": Path(image_source).expanduser() if image_source else None,
    }


def image_id_from_name(path):
    """01.png -> 1, shot_007.webp -> 7, 'image (3).jpg' -> 3. None if no digits."""
    stem = Path(path).stem
    match = re.match(r"^(\d+)", stem)
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\d+", stem)
    return int(numbers[-1]) if numbers else None


def image_ids_in(folder):
    ids = set()
    folder = Path(folder)
    if not folder.is_dir():
        return ids
    for entry in folder.iterdir():
        if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
            image_id = image_id_from_name(entry)
            if image_id is not None:
                ids.add(image_id)
    return ids


def beat_ids_in(beat_json):
    """Set of image_ids in beat.json, or None if the file can't be parsed."""
    try:
        with open(beat_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(item["image_id"]) for item in data if isinstance(item, dict) and "image_id" in item}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _missing_files(pairs):
    return [f"{label} not found: {path}" for label, path in pairs if not Path(path).exists()]


def _summarise(ids, limit=15):
    ids = sorted(ids)
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown + (", ..." if len(ids) > limit else "")


# --------------------------------------------------------------------------- #
# 1. script
# --------------------------------------------------------------------------- #


def build_script_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [
        _python(settings), "-m", "ytscript",
        "-t", _clean(ctx.get("title")) or paths["slug"], "-g",
        "--duration", str(_number(ctx.get("duration"), 3)),
    ]

    beats = _number(ctx.get("beats"), 0, int)
    if beats > 0:
        cmd += ["--beats", str(beats)]

    # Only pass flags that have a value: ytscript's --depth has argparse
    # choices, so an empty string would be rejected outright.
    for flag, key in (("--tone", "tone"), ("--art-style", "art_style"), ("--palette", "palette")):
        value = _clean(ctx.get(key))
        if value:
            cmd += [flag, value]
    depth = _clean(ctx.get("depth"))
    if depth in DEPTHS:
        cmd += ["--depth", depth]

    aspect = _pick(ctx.get("aspect_ratio"), ASPECT_RATIOS, "")
    if aspect:
        cmd += ["--aspect-ratio", aspect]

    for keyword in _as_list(ctx.get("keywords")):
        cmd += ["--keyword", keyword]
    for avoid in _as_list(ctx.get("avoid")):
        cmd += ["--avoid", avoid]

    if _clean(ctx.get("custom_instructions")):
        cmd += ["--instructions-file", str(paths["instructions"])]

    cmd += ["-o", str(paths["project_dir"])]
    return cmd, _job_dir("script", settings), False


def precheck_script(ctx, settings):
    errors = []
    if not _clean(ctx.get("title")):
        errors.append("The project has no title - the script generator needs a topic.")
    if _number(ctx.get("duration"), 0) <= 0 and _number(ctx.get("beats"), 0, int) <= 0:
        errors.append("Duration must be greater than 0 (or set an explicit beat count).")
    return errors, []


# --------------------------------------------------------------------------- #
# 2. audio
# --------------------------------------------------------------------------- #


def build_audio_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [
        _python(settings), "tts.py",
        "--path", str(paths["voiceover"]),
        "--reference-id", _clean(ctx.get("reference_id")),
        "--out", str(paths["audio"]),
    ]
    return cmd, _job_dir("audio", settings), False


def precheck_audio(ctx, settings):
    paths = project_paths(ctx)
    errors = _missing_files([("voiceover.md", paths["voiceover"])])
    if not _clean(ctx.get("reference_id")):
        errors.append("No Fish Audio voice reference ID set (Script & Voice tab).")
    return errors, []


# --------------------------------------------------------------------------- #
# 3. image
# --------------------------------------------------------------------------- #


def build_image_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [_uv(settings), "run", "autoimg.py", "--source", str(paths["image_prompts"])]
    if ctx.get("image_subfolder"):
        # The extension saves into <browser download dir>/<folder>/ - the
        # editor step then reads from <downloads_dir>/<slug>/ (see
        # PipelineEngine._build_ctx).
        cmd += ["--folder", paths["slug"]]
    return cmd, _job_dir("image", settings), False


def precheck_image(ctx, settings):
    paths = project_paths(ctx)
    return _missing_files([("image_prompts.md", paths["image_prompts"])]), []


# --------------------------------------------------------------------------- #
# 4. subtitle
# --------------------------------------------------------------------------- #


def build_subtitle_cmd(ctx, settings):
    """
    Mirrors the manual one-liner:
        tmp=$(mktemp --suffix=.wav) && ffmpeg ... -i audio.mp3 ... "$tmp" \\
          && ./build/bin/whisper-cli -m <model> -f "$tmp" -osrt -of <prefix>

    -of is given "<project_dir>/<slug>" so whisper.cpp writes
    "<project_dir>/<slug>.srt". The exit status of the ffmpeg/whisper chain
    is captured *before* the temp file is removed - otherwise the step
    would always report success because `rm` exits 0.
    """
    paths = project_paths(ctx)
    whisper = settings.get("whisper", {})
    model = whisper.get("model") or "models/ggml-tiny.en.bin"
    whisper_cli = whisper.get("cli_path") or "./build/bin/whisper-cli"

    q = shlex.quote
    shell_cmd = (
        'tmp=$(mktemp --suffix=.wav) && '
        f'ffmpeg -hide_banner -loglevel error -y -i {q(str(paths["audio"]))} '
        '-ar 16000 -ac 1 -c:a pcm_s16le "$tmp" && '
        f'{q(whisper_cli)} -m {q(model)} -f "$tmp" -osrt -of {q(str(paths["srt_prefix"]))} ; '
        'status=$? ; rm -f "$tmp" ; exit $status'
    )
    return shell_cmd, _job_dir("subtitle", settings), True


def precheck_subtitle(ctx, settings):
    paths = project_paths(ctx)
    errors = _missing_files([("audio.mp3", paths["audio"])])
    root = Path(settings.get("pipeline_root", "."))
    job_dir = root / _job_dir("subtitle", settings)
    whisper = settings.get("whisper", {})
    cli = job_dir / (whisper.get("cli_path") or "./build/bin/whisper-cli")
    model = job_dir / (whisper.get("model") or "models/ggml-tiny.en.bin")
    if not cli.exists():
        errors.append(f"whisper-cli not found: {cli} (build whisper.cpp or fix settings.whisper.cli_path)")
    if not model.exists():
        errors.append(f"whisper model not found: {model} (download it or fix settings.whisper.model)")
    return errors, []


# --------------------------------------------------------------------------- #
# 5. beat aligner
# --------------------------------------------------------------------------- #


def build_beat_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [
        _uv(settings), "run", "python", "beatalign.py",
        "--beatsource", str(paths["beat_md"]),
        "--srtsource", str(paths["srt"]),
        "--out", str(paths["beat_json"]),
    ]
    return cmd, _job_dir("beat", settings), False


def precheck_beat(ctx, settings):
    paths = project_paths(ctx)
    return _missing_files([("beat.md", paths["beat_md"]), ("subtitles (.srt)", paths["srt"])]), []


# --------------------------------------------------------------------------- #
# 6. editor
# --------------------------------------------------------------------------- #


def build_editor_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [
        _python(settings), "editor.py",
        "--imagesource", str(paths["image_source"] or ""),
        "--beatpath", str(paths["beat_json"]),
        "--out", str(paths["video"]),
        "--animation", _pick(ctx.get("animation"), ANIMATIONS, "none"),
        "--transition", _pick(ctx.get("transition"), TRANSITIONS, "none"),
        "--smoothness", _pick(ctx.get("smoothness"), SMOOTHNESS, "ease_in_out"),
        "--aspect", _pick(ctx.get("aspect_ratio"), ASPECT_RATIOS, "16:9"),
        "--fit", _pick(ctx.get("fit"), FIT_MODES, "cover"),
        "--fps", str(max(1, _number(ctx.get("fps"), 30, int))),
        "--zoom", str(_number(ctx.get("zoom"), 0.18)),
    ]
    seed = _clean(ctx.get("seed"))
    if seed:
        cmd += ["--seed", seed]
    return cmd, _job_dir("editor", settings), False


def precheck_editor(ctx, settings):
    paths = project_paths(ctx)
    errors = _missing_files([("beat.json", paths["beat_json"])])
    warnings = []

    image_dir = paths["image_source"]
    if image_dir is None:
        errors.append("No image folder set (Video & Animation tab).")
        return errors, warnings
    if not image_dir.is_dir():
        errors.append(f"Image folder does not exist: {image_dir}")
        return errors, warnings

    image_ids = image_ids_in(image_dir)
    if not image_ids:
        errors.append(f"No numbered images (01.png, 02.png, ...) found in {image_dir}. Have the downloads finished?")
        return errors, warnings

    beat_ids = beat_ids_in(paths["beat_json"]) if not errors else None
    if beat_ids is not None:
        missing = beat_ids - image_ids
        extra = image_ids - beat_ids
        if missing and len(missing) == len(beat_ids):
            errors.append(
                f"None of the {len(beat_ids)} beats has a matching image in {image_dir}. "
                "Check that the downloads finished and that the folder is right."
            )
        elif missing:
            warnings.append(
                f"{len(missing)} of {len(beat_ids)} beats have no image in {image_dir} "
                f"(ids: {_summarise(missing)}). The previous image will be held for those beats."
            )
        if extra:
            warnings.append(
                f"{len(extra)} image(s) in {image_dir} have no beat and will be ignored "
                f"(ids: {_summarise(extra)}). Is this folder shared with another project?"
            )
    return errors, warnings


# --------------------------------------------------------------------------- #
# 7. audio adder
# --------------------------------------------------------------------------- #


def build_audio_add_cmd(ctx, settings):
    paths = project_paths(ctx)
    cmd = [
        _python(settings), "add_audio.py",
        "--video", str(paths["video"]),
        "--audio", str(paths["audio"]),
        "--out", str(paths["final"]),
        "--extend-video",
    ]
    return cmd, _job_dir("audio_add", settings), False


def precheck_audio_add(ctx, settings):
    paths = project_paths(ctx)
    return _missing_files([("rendered video", paths["video"]), ("audio.mp3", paths["audio"])]), []


# --------------------------------------------------------------------------- #
# Ordered list of pipeline steps. This order is exactly the order the
# pipeline runs in, and the order shown in the GUI.
# --------------------------------------------------------------------------- #

JOBS = [
    {
        "id": "script",
        "label": "1. Script Generator",
        "builder": build_script_cmd,
        "precheck": precheck_script,
        "auto_pause_after": False,
    },
    {
        "id": "audio",
        "label": "2. Audio Generator",
        "builder": build_audio_cmd,
        "precheck": precheck_audio,
        "auto_pause_after": False,
    },
    {
        "id": "image",
        "label": "3. Image Generator",
        "builder": build_image_cmd,
        "precheck": precheck_image,
        "auto_pause_after": True,
        "pause_message": (
            "This worker opens Brave and triggers the download extension, "
            "then exits immediately - it does NOT wait for images to finish "
            "downloading. Verify the images have finished downloading, then "
            "click Resume."
        ),
    },
    {
        "id": "subtitle",
        "label": "4. Subtitle Generator",
        "builder": build_subtitle_cmd,
        "precheck": precheck_subtitle,
        "auto_pause_after": False,
    },
    {
        "id": "beat",
        "label": "5. Beat Aligner",
        "builder": build_beat_cmd,
        "precheck": precheck_beat,
        "auto_pause_after": False,
    },
    {
        "id": "editor",
        "label": "6. Video Editor",
        "builder": build_editor_cmd,
        "precheck": precheck_editor,
        "auto_pause_after": False,
    },
    {
        "id": "audio_add",
        "label": "7. Audio Adder",
        "builder": build_audio_add_cmd,
        "precheck": precheck_audio_add,
        "auto_pause_after": False,
    },
]

JOB_IDS = [j["id"] for j in JOBS]
JOBS_BY_ID = {j["id"]: j for j in JOBS}
