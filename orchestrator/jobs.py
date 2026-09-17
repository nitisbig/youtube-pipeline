"""
Defines the 7 pipeline steps and how to build the exact CLI command for
each one, given a project context (ctx) and the loaded settings.

Each builder returns a 3-tuple:
    (command, cwd_relative_to_pipeline_root, is_shell)

- command is a list of argv tokens (normal case), or a single shell
  string when is_shell=True (needed for the subtitle step, which chains
  several commands with mktemp / && / ; the way you run it by hand).
- cwd_relative_to_pipeline_root is looked up from settings["job_dirs"].

To change how a step is invoked (new flag, different binary, etc.) edit
the matching build_*_cmd function below - nothing else needs to change.
"""


def _job_dir(job_id, settings):
    return settings.get("job_dirs", {}).get(job_id, ".")


def build_script_cmd(ctx, settings):
    cmd = [
        settings.get("python_bin", "python3"), "-m", "ytscript",
        "-t", str(ctx["title"]), "-g",
        "--duration", str(ctx.get("duration", 3)),
        "--tone", str(ctx.get("tone", "")),
        "--depth", str(ctx.get("depth", "")),
        "--art-style", str(ctx.get("art_style", "")),
        "--palette", str(ctx.get("palette", "")),
    ]
    for kw in ctx.get("keywords", []) or []:
        cmd += ["--keyword", str(kw)]
    if ctx.get("avoid"):
        cmd += ["--avoid", str(ctx["avoid"])]
    cmd += ["-o", ctx["project_dir"]]
    return cmd, _job_dir("script", settings), False


def build_audio_cmd(ctx, settings):
    project_dir = ctx["project_dir"]
    cmd = [
        settings.get("python_bin", "python3"), "tts.py",
        "--path", f"{project_dir}/voiceover.md",
        "--reference-id", str(ctx.get("reference_id", "")),
        "--out", f"{project_dir}/audio.mp3",
    ]
    return cmd, _job_dir("audio", settings), False


def build_image_cmd(ctx, settings):
    project_dir = ctx["project_dir"]
    cmd = [
        settings.get("uv_bin", "uv"), "run", "autoimg.py",
        "--source", f"{project_dir}/image_prompts.md",
    ]
    return cmd, _job_dir("image", settings), False


def build_subtitle_cmd(ctx, settings):
    """
    Mirrors the manual one-liner:
        tmp=$(mktemp --suffix=.wav) && ffmpeg ... -i audio.mp3 ... "$tmp" \
          && ./build/bin/whisper-cli -m <model> -f "$tmp" -osrt -of <prefix> ; rm -f "$tmp"

    -of is given "<project_dir>/<slug>" so whisper.cpp writes
    "<project_dir>/<slug>.srt" (i.e. the .srt ends up *inside* the
    project folder, named after the slug). If your build of whisper.cpp
    names the output differently, adjust this function and the
    --srtsource path in build_beat_cmd to match.
    """
    project_dir = ctx["project_dir"]
    slug = ctx["slug"]
    whisper = settings.get("whisper", {})
    model = whisper.get("model", "models/ggml-tiny.en.bin")
    whisper_cli = whisper.get("cli_path", "./build/bin/whisper-cli")

    audio_path = f"{project_dir}/audio.mp3"
    out_prefix = f"{project_dir}/{slug}"

    shell_cmd = (
        'tmp=$(mktemp --suffix=.wav) && '
        f'ffmpeg -loglevel error -y -i "{audio_path}" -ar 16000 -ac 1 -c:a pcm_s16le "$tmp" && '
        f'{whisper_cli} -m {model} -f "$tmp" -osrt -of "{out_prefix}" ; '
        'rm -f "$tmp"'
    )
    return shell_cmd, _job_dir("subtitle", settings), True


def build_beat_cmd(ctx, settings):
    project_dir = ctx["project_dir"]
    slug = ctx["slug"]
    cmd = [
        settings.get("python_bin", "python3"), "beatalign.py",
        "--beatsource", f"{project_dir}/beat.md",
        "--srtsource", f"{project_dir}/{slug}.srt",
        "--out", f"{project_dir}/beat.json",
    ]
    return cmd, _job_dir("beat", settings), False


def build_editor_cmd(ctx, settings):
    project_dir = ctx["project_dir"]
    slug = ctx["slug"]
    cmd = [
        settings.get("python_bin", "python3"), "editor.py",
        "--imagesource", str(ctx.get("image_source", "")),
        "--beatpath", f"{project_dir}/beat.json",
        "--animation", str(ctx.get("animation", "fadein")),
        "--out", f"{project_dir}/{slug}.mp4",
    ]
    return cmd, _job_dir("editor", settings), False


def build_audio_add_cmd(ctx, settings):
    project_dir = ctx["project_dir"]
    slug = ctx["slug"]
    cmd = [
        settings.get("python_bin", "python3"), "add_audio.py",
        "--video", f"{project_dir}/{slug}.mp4",
        "--audio", f"{project_dir}/audio.mp3",
        "--out", f"{project_dir}/final.mp4",
    ]
    return cmd, _job_dir("audio_add", settings), False


# Ordered list of pipeline steps. This order is exactly the order the
# pipeline runs in, and the order shown in the GUI.
JOBS = [
    {
        "id": "script",
        "label": "1. Script Generator",
        "builder": build_script_cmd,
        "auto_pause_after": False,
    },
    {
        "id": "audio",
        "label": "2. Audio Generator",
        "builder": build_audio_cmd,
        "auto_pause_after": False,
    },
    {
        "id": "image",
        "label": "3. Image Generator",
        "builder": build_image_cmd,
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
        "auto_pause_after": False,
    },
    {
        "id": "beat",
        "label": "5. Beat Aligner",
        "builder": build_beat_cmd,
        "auto_pause_after": False,
    },
    {
        "id": "editor",
        "label": "6. Video Editor",
        "builder": build_editor_cmd,
        "auto_pause_after": False,
    },
    {
        "id": "audio_add",
        "label": "7. Audio Adder",
        "builder": build_audio_add_cmd,
        "auto_pause_after": False,
    },
]

JOB_IDS = [j["id"] for j in JOBS]
JOBS_BY_ID = {j["id"]: j for j in JOBS}
