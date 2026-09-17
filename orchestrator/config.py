"""
Loads and saves settings.json.

settings.json lives at the pipeline root (next to main.py) and holds
everything that's environment-specific: where the pipeline root is,
which python/uv binary to call, and the default parameters that get
snapshotted into every new project (tone, palette, reference-id, etc).

Editing settings.json is the normal way to reconfigure the tool -
nothing here is hardcoded except the *shape* of the config and a
first-run default that matches the example commands you gave.
"""

import json
from pathlib import Path

SETTINGS_FILENAME = "settings.json"

# First-run defaults. These match the example commands from the brief.
# Edit settings.json after first run instead of editing this dict -
# this is only used to create the file if it doesn't exist yet.
DEFAULT_SETTINGS = {
    "pipeline_root": str(Path(__file__).resolve().parent.parent),
    "python_bin": "python3",
    "uv_bin": "uv",

    "defaults": {
        "duration": 3,
        "tone": "story, sad",
        "depth": "deep",
        "art_style": "2.5D parallax illustration",
        "palette": "ochre and slate",
        "keywords": ["aqueduct", "legion"],
        "avoid": "clickbait",
        "reference_id": "bf322df2096a46f18c579d0baa36f41d",
        "animation": "fadein",
        "image_source": str(Path.home() / "Downloads" / "bulk"),
    },

    # Sub-folder (relative to pipeline_root) each worker is run from.
    "job_dirs": {
        "script": "script-gen",
        "audio": "audio-gen",
        "image": "image-gen",
        "subtitle": "subtitle-gen/whisper.cpp",
        "beat": "beat-gen",
        "editor": "editor",
        "audio_add": "editor",
    },

    # whisper.cpp specifics used by the subtitle job.
    "whisper": {
        "model": "models/ggml-tiny.en.bin",
        "cli_path": "./build/bin/whisper-cli",
    },
}


class Config:
    def __init__(self, path=None):
        pipeline_root = Path(__file__).resolve().parent.parent
        self.path = Path(path) if path else pipeline_root / SETTINGS_FILENAME
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Fill in any keys that were added to DEFAULT_SETTINGS since
            # this settings.json was created, without clobbering the
            # user's existing values.
            changed = False
            for key, value in DEFAULT_SETTINGS.items():
                if key not in data:
                    data[key] = value
                    changed = True
            if changed:
                self._write(data)
            return data
        else:
            self._write(DEFAULT_SETTINGS)
            return json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def save(self, data=None):
        if data is not None:
            self.data = data
        self._write(self.data)

    def reload(self):
        self.data = self._load()
        return self.data

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def pipeline_root(self):
        return Path(self.data.get("pipeline_root", Path(__file__).resolve().parent.parent))
