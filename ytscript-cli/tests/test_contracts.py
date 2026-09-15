"""Contract tests. Fully offline: everything runs on the `mock` provider.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytscript import (  # noqa: E402
    Pipeline,
    RenderOptions,
    ScriptRequest,
    Settings,
    ValidationError,
    build_context,
    generate_script,
    render_all,
)
from ytscript.cli import main  # noqa: E402
from ytscript.config import parse_dotenv  # noqa: E402
from ytscript.utils import ensure_prefix  # noqa: E402


def mock_settings() -> Settings:
    settings = Settings()
    settings.provider.name = "mock"
    return settings


class BeatCountTests(unittest.TestCase):
    def test_explicit_beats_wins(self):
        self.assertEqual(ScriptRequest(topic="t", beats=90).beat_count, 90)

    def test_beats_derived_from_duration(self):
        request = ScriptRequest(topic="t", duration_minutes=7, words_per_minute=150, words_per_beat=12)
        self.assertEqual(request.beat_count, 88)

    def test_image_prefix_uses_aspect_ratio(self):
        self.assertEqual(
            ScriptRequest(topic="t").image_prefix, "create image with 16:9 ratio."
        )
        self.assertEqual(
            ScriptRequest(topic="t", aspect_ratio="9:16").image_prefix,
            "create image with 9:16 ratio.",
        )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.request = ScriptRequest(topic="Dog Psychology", beats=17, chunk_size=5)
        self.script = generate_script(self.request, mock_settings())

    def test_exact_beat_count(self):
        self.assertEqual(self.script.beat_count, 17)

    def test_beats_equal_image_prompts(self):
        prompts = [b.image_prompt for b in self.script.beats if b.image_prompt.strip()]
        self.assertEqual(len(prompts), self.script.beat_count)

    def test_beats_are_numbered_from_one(self):
        self.assertEqual([b.index for b in self.script.beats], list(range(1, 18)))

    def test_every_prompt_is_prefixed_once(self):
        for beat in self.script.beats:
            self.assertTrue(beat.image_prompt.startswith("create image with 16:9 ratio."))
            self.assertEqual(beat.image_prompt.lower().count("create image with"), 1)

    def test_metadata_is_populated(self):
        self.assertEqual(self.script.metadata["beat_count"], 17)
        self.assertIn("estimated_minutes", self.script.metadata)

    def test_validation_rejects_missing_prompts(self):
        context = build_context(ScriptRequest(topic="t", beats=4), mock_settings())
        pipeline = Pipeline.default().remove("image_prompts")
        with self.assertRaises(ValidationError):
            pipeline.run(context)

    def test_custom_stage_can_be_inserted(self):
        class TagStage:
            name = "tag"

            def run(self, context):
                context.metadata["tagged"] = True

        context = build_context(ScriptRequest(topic="t", beats=3), mock_settings())
        script = Pipeline.default().insert_after("beats", TagStage()).run(context)
        self.assertTrue(script.metadata["tagged"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.script = generate_script(
            ScriptRequest(topic="Dog Psychology", beats=6, chunk_size=3), mock_settings()
        )
        self.files = render_all(self.script, RenderOptions())

    def test_expected_filenames(self):
        self.assertEqual(
            sorted(self.files), ["beat.md", "image_prompts.md", "voiceover.md"]
        )

    def test_voiceover_is_plain_text(self):
        text = self.files["voiceover.md"]
        for marker in ("#", "beat1", "\u2192", "create image"):
            self.assertNotIn(marker, text)

    def test_beat_file_format(self):
        lines = [l for l in self.files["beat.md"].splitlines() if l.strip()]
        self.assertEqual(len(lines), 6)
        for number, line in enumerate(lines, start=1):
            self.assertTrue(line.startswith(f"beat{number} \u2192 "), line)

    def test_image_prompts_blank_line_separated_and_counted(self):
        blocks = [b for b in self.files["image_prompts.md"].split("\n\n") if b.strip()]
        self.assertEqual(len(blocks), 6)
        for block in blocks:
            self.assertTrue(block.startswith("create image with 16:9 ratio."))
            self.assertNotIn("\n", block.strip())

    def test_image_prompts_are_self_sufficient(self):
        banned = ("same as before", "previous scene", "as above", "see beat")
        for block in self.files["image_prompts.md"].split("\n\n"):
            for phrase in banned:
                self.assertNotIn(phrase, block.lower())

    def test_render_options_are_customisable(self):
        files = render_all(
            self.script,
            RenderOptions(
                arrow="->",
                beat_label="shot",
                beat_blank_lines=False,
                voiceover_layout="lines",
                image_prefix="/imagine prompt:",
                filenames={"voiceover": "vo.md", "beats": "b.md", "image_prompts": "img.md"},
            ),
        )
        self.assertEqual(sorted(files), ["b.md", "img.md", "vo.md"])
        self.assertTrue(files["b.md"].startswith("shot1 -> "))
        self.assertEqual(len(files["vo.md"].strip().splitlines()), 6)
        self.assertTrue(files["img.md"].startswith("/imagine prompt:"))

    def test_ensure_prefix_is_idempotent(self):
        prefix = "create image with 16:9 ratio."
        once = ensure_prefix("a dog in a field", prefix)
        twice = ensure_prefix(once, prefix)
        self.assertEqual(once, twice)
        self.assertEqual(twice.lower().count("create image"), 1)


class ConfigTests(unittest.TestCase):
    def test_dotenv_parsing(self):
        values = parse_dotenv('# c\nexport A=1\nB="two"\n\nBAD\n')
        self.assertEqual(values, {"A": "1", "B": "two"})

    def test_settings_file_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "name": "mock",
                            "model": "m1",
                            "api_key": "sk-supersecret-value-123456",
                        },
                        "defaults": {"tone": "dry"},
                        "profiles": {"shorts": {"beats": 12}},
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings.load(config_path=str(path), env={})
            self.assertEqual(settings.provider.name, "mock")
            self.assertEqual(settings.tag_defaults("shorts"), {"tone": "dry", "beats": 12})
            # the key is never echoed back in full
            self.assertNotIn("sk-supersecret-value-123456", json.dumps(settings.to_dict()))


class CliTests(unittest.TestCase):
    def test_generate_writes_three_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(
                ["--topic", "Dog Psychology", "--generate", "--provider", "mock",
                 "--beats", "8", "--chunk-size", "4", "--outdir", tmp, "--quiet"]
            )
            self.assertEqual(code, 0)
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ["beat.md", "image_prompts.md", "voiceover.md"])

            beats = [l for l in (Path(tmp) / "beat.md").read_text().splitlines() if l.strip()]
            prompts = [b for b in (Path(tmp) / "image_prompts.md").read_text().split("\n\n") if b.strip()]
            self.assertEqual(len(beats), 8)
            self.assertEqual(len(beats), len(prompts))

    def test_missing_topic_is_usage_error(self):
        self.assertEqual(main(["--generate", "--provider", "mock"]), 64)

    def test_no_clobber_returns_73(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "voiceover.md").write_text("existing", encoding="utf-8")
            code = main(
                ["-t", "T", "-g", "-p", "mock", "-b", "2", "--chunk-size", "2",
                 "-o", tmp, "-n", "-q"]
            )
            self.assertEqual(code, 73)

    def test_unknown_provider_is_config_error(self):
        settings = mock_settings()
        settings.provider.name = "does_not_exist"
        with self.assertRaises(Exception):
            build_context(ScriptRequest(topic="t"), settings)

    def test_default_command_is_generate(self):
        from ytscript.cli import normalize_argv

        self.assertEqual(normalize_argv(["-t", "x", "-g"])[0], "generate")
        self.assertEqual(normalize_argv(["doctor"]), ["doctor"])
        self.assertEqual(normalize_argv(["--help"]), ["--help"])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["-t", "T", "--dry-run", "-p", "mock", "-o", tmp, "-b", "5"])
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
