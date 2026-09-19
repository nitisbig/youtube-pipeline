"""
Loads and saves settings.json.

settings.json lives at the pipeline root (next to main.py) and holds
everything that's environment-specific: where the pipeline root is,
which python/uv binary to call, and the default parameters that get
snapshotted into every new project (tone, palette, reference-id, etc).

Editing settings.json is the normal way to reconfigure the tool -
nothing here is hardcoded except the *shape* of the config and a
first-run default that matches the example commands.

Keys added to DEFAULT_SETTINGS in a later version are filled in
automatically (at any nesting depth) the next time the app starts,
without touching values the user already customised.
"""

import copy
import json
import time
from pathlib import Path

SETTINGS_FILENAME = "settings.json"
PIPELINE_ROOT = Path(__file__).resolve().parent.parent

# First-run defaults. Edit settings.json after first run instead of
# editing this dict - this is only used to create the file if it doesn't
# exist yet, and to fill in keys that are missing from an older file.
DEFAULT_SETTINGS = {
    "pipeline_root": str(PIPELINE_ROOT),
    "python_bin": "python3",
    "uv_bin": "uv",

    # Snapshotted into every new project when it is created. Most of these
    # can be changed per project in the GUI before you click
    # "Create / Load Project" (and again later - the GUI values are applied
    # to the loaded project before every run).
    "defaults": {
        "duration": 3,
        "beats": "",
        "tone": "story, sad",
        "depth": "deep",
        "art_style": "2.5D parallax illustration",
        "palette": "ochre and slate",
        "keywords": ["aqueduct", "legion"],
        "avoid": "clickbait",
        "custom_instructions": "",
        "reference_id": "bf322df2096a46f18c579d0baa36f41d",
        "audio_preset": "youtube",
        "aspect_ratio": "16:9",
        "animation": "random",
        "transition": "fade",
        "smoothness": "ease_in_out",
        "fit": "cover",
        "fps": 30,
        "zoom": 0.18,
        "image_source": str(Path.home() / "Downloads" / "bulk"),
        "image_subfolder": False,
        "subtitle_style": "hormozi",
        "subtitle_animation": "pop",
        "subtitle_position": "bottom",
        "subtitle_max_words": 3,
        "subtitle_uppercase": False,
        "subtitle_font": "",
        "subtitle_font_size": 0,
        "use_gpu": False,
    },

    # Sub-folder (relative to pipeline_root) each worker is run from.
    "job_dirs": {
        "script": "script-gen",
        "audio": "audio-gen",
        "enhance": "audio-gen",
        "image": "image-gen",
        "subtitle": "subtitle-gen/whisper.cpp",
        "beat": "beat-gen",
        "editor": "editor",
        "audio_add": "editor",
        "subtitle_burn": "subtitle-gen",
    },

    # whisper.cpp specifics used by the subtitle job.
    "whisper": {
        "model": "models/ggml-tiny.en.bin",
        "cli_path": "./build/bin/whisper-cli",
    },

    # Where the browser saves downloads. Used when a project enables
    # "per-project image subfolder": images then land in
    # <downloads_dir>/<slug>/ instead of one shared folder.
    "image_gen": {
        "downloads_dir": str(Path.home() / "Downloads"),
    },

    # How many times a failed step is re-run automatically before the
    # pipeline halts. 0 = never retry. The image step is never retried by
    # default because it hands off to the browser extension.
    "retries": {
        "script": 1,
        "audio": 2,
        "enhance": 1,
        "image": 0,
        "subtitle": 0,
        "beat": 2,
        "editor": 0,
        "audio_add": 0,
        "subtitle_burn": 0,
    },
    "retry_delay_seconds": 5,
}


def deep_fill(target, defaults):
    """Recursively add keys from `defaults` that are missing in `target`.

    Existing values are never overwritten. Returns True if anything changed.
    """
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            changed = True
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            if deep_fill(target[key], value):
                changed = True
    return changed


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else PIPELINE_ROOT / SETTINGS_FILENAME
        self.load_error = None  # human readable message if the file had to be replaced
        self.data = self._load()

    # ------------------------------------------------------------------
    def _load(self):
        if not self.path.exists():
            data = copy.deepcopy(DEFAULT_SETTINGS)
            self._write(data)
            return data

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("settings.json must contain a JSON object")
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
            try:
                self.path.replace(backup)
                moved = f"The broken file was moved to {backup.name}. "
            except OSError:
                moved = ""
            self.load_error = f"Could not read {self.path}: {exc}. {moved}Defaults were restored."
            data = copy.deepcopy(DEFAULT_SETTINGS)
            self._write(data)
            return data

        if deep_fill(data, DEFAULT_SETTINGS):
            self._write(data)
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    def save(self, data=None):
        if data is not None:
            if not isinstance(data, dict):
                raise ValueError("settings must be a JSON object")
            deep_fill(data, DEFAULT_SETTINGS)
            self.data = data
        self._write(self.data)

    def reload(self):
        self.load_error = None
        self.data = self._load()
        return self.data

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def defaults(self):
        return self.data.get("defaults", {})

    @property
    def pipeline_root(self):
        return Path(self.data.get("pipeline_root") or PIPELINE_ROOT)
