"""Unix-style command line interface.

Design rules:

* ``generate`` is the implicit default command, so the documented workflow
  ``python3 -m ytscript --topic "Dog Psychology" --generate`` just works.
* stdout is data, stderr is chatter (so ``--json | jq`` is safe).
* every failure maps to a stable exit code from ``errors.py``.
* short and long flags for everything you type often.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .config import Settings
from .errors import ConfigError, UsageError, YtScriptError
from .models import ScriptRequest
from .pipeline import Pipeline, build_context
from .providers import PROVIDERS
from .renderers import DEFAULT_FILENAMES, RENDERERS, RenderOptions, render_all
from .research import RESEARCH
from .utils import get_logger, setup_logging, slugify, write_text

PROG = "ytscript"
COMMANDS = ("generate", "init", "tags", "providers", "doctor", "version")

EPILOG = """
examples:
  # the documented workflow
  python3 -m ytscript --topic "Dog Psychology" --generate

  # offline demo, no API key needed
  python3 -m ytscript -t "Dog Psychology" -g -p mock -b 12 -o out/demo

  # 90 beats with locked art direction
  python3 -m ytscript -t "Swayamprabha" -g -b 90 --genre mythology \\
      --art-style "2.5D parallax illustration" \\
      --subject-lock "a calm ascetic woman in saffron robes"

  # machine readable, for another agent or a shell pipeline
  python3 -m ytscript -t "Dog Psychology" -g --json | jq -r '.files[]'

files written:
  voiceover.md      plain voiceover text
  beat.md           beat1 \u2192 voiceover
  image_prompts.md  "create image with 16:9 ratio. {prompt}", blank-line separated
                    (count always equals the number of beats)

exit codes:
  0 ok   64 usage   65 bad model output   69 provider   73 cannot write
  78 config   130 interrupted
"""

# Tag fields that may also be set from settings.json defaults / profiles.
TAG_FIELDS = (
    "beats",
    "duration_minutes",
    "words_per_minute",
    "words_per_beat",
    "chunk_size",
    "title",
    "language",
    "tone",
    "style",
    "audience",
    "narrator",
    "pov",
    "hook",
    "cta",
    "depth",
    "pacing",
    "humor",
    "emotion",
    "genre",
    "series",
    "reading_level",
    "keywords",
    "avoid",
    "instructions",
    "aspect_ratio",
    "image_style",
    "art_style",
    "palette",
    "lighting",
    "camera",
    "composition",
    "subject_lock",
    "negative_prompt",
    "image_prefix_template",
    "seed",
    "research",
)

# CLI flag -> request field, for "was it typed explicitly?" detection.
ALIASES = {
    "--beats": "beats",
    "-b": "beats",
    "--duration": "duration_minutes",
    "-D": "duration_minutes",
    "--wpm": "words_per_minute",
    "--words-per-beat": "words_per_beat",
    "--chunk-size": "chunk_size",
    "--aspect-ratio": "aspect_ratio",
    "-r": "aspect_ratio",
    "--image-prefix": "image_prefix_template",
    "--reading-level": "reading_level",
    "--image-style": "image_style",
    "--art-style": "art_style",
    "--subject-lock": "subject_lock",
    "--negative-prompt": "negative_prompt",
    "-k": "keywords",
    "--keyword": "keywords",
    "-x": "avoid",
    "--avoid": "avoid",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _kv(value: str) -> tuple:
    """``KEY=VALUE`` argparse type for freeform custom tags."""
    key, sep, val = value.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
    return key.strip(), val.strip()


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32, width=100)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Generate voiceover.md, beat.md and image_prompts.md for a faceless "
            "YouTube video. Provider agnostic, plug-and-play, scriptable."
        ),
        epilog=EPILOG,
        formatter_class=_Formatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    _add_generate(subparsers.add_parser(
        "generate",
        help="generate the three files (default command)",
        description="Generate voiceover.md, beat.md and image_prompts.md.",
        epilog=EPILOG,
        formatter_class=_Formatter,
    ))

    init = subparsers.add_parser(
        "init", help="write .env / settings.json templates", formatter_class=_Formatter
    )
    init.add_argument("-d", "--dir", default=".", help="where to write the templates")
    init.add_argument("-f", "--force", action="store_true", help="overwrite existing files")

    subparsers.add_parser("tags", help="list every custom tag option", formatter_class=_Formatter)

    subparsers.add_parser(
        "providers",
        help="list providers, research backends, renderers, stages",
        formatter_class=_Formatter,
    )

    doctor = subparsers.add_parser(
        "doctor", help="check the resolved configuration", formatter_class=_Formatter
    )
    doctor.add_argument("-c", "--config", help="path to settings.json")
    doctor.add_argument("--env-file", help="path to a .env file")
    doctor.add_argument("--json", action="store_true", help="machine readable output")

    subparsers.add_parser("version", help="print the version", formatter_class=_Formatter)
    return parser


def _add_generate(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    src = p.add_argument_group("input")
    src.add_argument("-t", "--topic", help='video topic ("-" reads it from stdin)')
    src.add_argument(
        "-g", "--generate", action="store_true", help="generate now (default action)"
    )
    src.add_argument("--title", help="force the video title instead of generating one")

    st = p.add_argument_group("structure")
    st.add_argument("-b", "--beats", type=int, help="exact number of beats (= image prompts)")
    st.add_argument(
        "-D", "--duration", type=float, dest="duration_minutes",
        help="target runtime in minutes (default 7)",
    )
    st.add_argument("--wpm", type=int, dest="words_per_minute", help="speaking rate (default 150)")
    st.add_argument("--words-per-beat", type=int, help="words per beat (default 12)")
    st.add_argument("--chunk-size", type=int, help="beats per model call (default 12)")

    nar = p.add_argument_group("narration tags")
    nar.add_argument("--language", help="narration language")
    nar.add_argument("--tone", help='e.g. "warm, curious"')
    nar.add_argument("--style", help='e.g. "story-driven explainer"')
    nar.add_argument("--audience", help="who is watching")
    nar.add_argument("--narrator", help="narrator persona")
    nar.add_argument("--pov", help="point of view")
    nar.add_argument("--hook", help="how to open")
    nar.add_argument("--cta", help="call to action for the final beats")
    nar.add_argument("--depth", choices=["light", "balanced", "deep"], help="information density")
    nar.add_argument("--pacing", choices=["slow", "steady", "fast"], help="narrative pacing")
    nar.add_argument("--humor", choices=["none", "dry", "playful", "absurd"], help="humor level")
    nar.add_argument("--emotion", help='dominant feeling, e.g. "wonder"')
    nar.add_argument("--genre", help='e.g. "mythology retelling", "true crime"')
    nar.add_argument("--series", help="series name for recurring framing")
    nar.add_argument("--reading-level", help='e.g. "grade 8"')
    nar.add_argument("-k", "--keyword", action="append", dest="keywords", metavar="WORD",
                     help="keyword to weave in (repeatable)")
    nar.add_argument("-x", "--avoid", action="append", metavar="WORD",
                     help="word or claim to avoid (repeatable)")
    nar.add_argument("--tag", action="append", type=_kv, metavar="KEY=VALUE",
                     help="any custom narration tag (repeatable)")

    img = p.add_argument_group("image / art direction tags")
    img.add_argument("-r", "--aspect-ratio", help="aspect ratio used in the prompt prefix (16:9)")
    img.add_argument("--image-style", help='e.g. "cinematic realism"')
    img.add_argument("--art-style", help='e.g. "2.5D parallax illustration"')
    img.add_argument("--palette", help="color palette")
    img.add_argument("--lighting", help="lighting direction")
    img.add_argument("--camera", help="lens / camera language")
    img.add_argument("--composition", help="framing rules")
    img.add_argument("--subject-lock", help="recurring subject description repeated in every prompt")
    img.add_argument("--negative-prompt", help="what must never appear in images")
    img.add_argument("--image-prefix", dest="image_prefix_template",
                     help='prompt prefix template (default "create image with {aspect_ratio} ratio.")')
    img.add_argument("--image-tag", action="append", type=_kv, metavar="KEY=VALUE",
                     help="any custom visual tag (repeatable)")

    out = p.add_argument_group("output")
    out.add_argument("-o", "--outdir", help="output directory (default out/<topic-slug>)")
    out.add_argument("--voiceover-file", help=f"default {DEFAULT_FILENAMES['voiceover']}")
    out.add_argument("--beat-file", help=f"default {DEFAULT_FILENAMES['beats']}")
    out.add_argument("--image-file", help=f"default {DEFAULT_FILENAMES['image_prompts']}")
    out.add_argument("--voiceover-layout", choices=["paragraphs", "lines", "single"],
                     help="voiceover.md layout (default paragraphs)")
    out.add_argument("--paragraph-beats", type=int, help="beats per paragraph (default 4)")
    out.add_argument("--arrow", help="separator in beat.md (default \u2192)")
    out.add_argument("--beat-label", help='prefix in beat.md (default "beat")')
    out.add_argument("--compact-beats", action="store_true", help="no blank line between beats")
    out.add_argument("--save-json", nargs="?", const="script.json", metavar="PATH",
                     help="also write the full script as JSON")
    out.add_argument("-f", "--force", action="store_true", default=True,
                     help="overwrite existing files (default)")
    out.add_argument("-n", "--no-clobber", dest="force", action="store_false",
                     help="never overwrite existing files")
    out.add_argument("--stdout", action="store_true", help="print files instead of writing them")
    out.add_argument("--json", action="store_true", help="machine readable summary on stdout")

    mod = p.add_argument_group("model / provider")
    mod.add_argument("-p", "--provider", choices=PROVIDERS.names(), help="wire format to use")
    mod.add_argument("-m", "--model", help="model name")
    mod.add_argument("-u", "--base-url", help="API base url")
    mod.add_argument("--api-key", help="API key (prefer --api-key-env)")
    mod.add_argument("--api-key-env", metavar="VAR", help="read the key from this env var")
    mod.add_argument("--temperature", type=float, help="sampling temperature")
    mod.add_argument("--max-tokens", type=int, help="max tokens per call")
    mod.add_argument("--timeout", type=int, help="request timeout in seconds")
    mod.add_argument("--retries", type=int, help="retries on retryable HTTP errors")
    mod.add_argument("--seed", type=int, help="seed for reproducibility, when supported")

    res = p.add_argument_group("research")
    res.add_argument("--research", choices=RESEARCH.names(), help="research backend (default none)")
    res.add_argument("--research-file", metavar="PATH", help="notes file for --research file")

    cfg = p.add_argument_group("configuration & diagnostics")
    cfg.add_argument("-c", "--config", help="path to settings.json")
    cfg.add_argument("--env-file", help="path to a .env file")
    cfg.add_argument("-P", "--profile", help="named tag bundle from settings.json")
    cfg.add_argument("--dry-run", action="store_true", help="show the plan, call nothing, write nothing")
    cfg.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug (stderr)")
    cfg.add_argument("-q", "--quiet", action="store_true", help="errors only")
    return p


# --------------------------------------------------------------------------- #
# request building
# --------------------------------------------------------------------------- #


def _resolve_topic(args: argparse.Namespace) -> str:
    topic = (args.topic or "").strip()
    if topic == "-":
        topic = sys.stdin.read().strip()
    if not topic:
        raise UsageError("a topic is required: --topic \"Dog Psychology\"")
    return topic


def _explicit_flags(argv: Sequence[str]) -> set:
    """Which request fields did the user actually type?"""
    typed = set()
    for token in argv:
        name = token.split("=", 1)[0]
        if name in ALIASES:
            typed.add(ALIASES[name])
        elif name.startswith("--"):
            typed.add(name[2:].replace("-", "_"))
    return typed


def build_request(
    args: argparse.Namespace, settings: Settings, argv: Sequence[str]
) -> ScriptRequest:
    """CLI flags > profile > settings defaults > dataclass defaults."""
    values: Dict[str, Any] = {}

    defaults = settings.tag_defaults(getattr(args, "profile", None))
    for key, value in defaults.items():
        if key in TAG_FIELDS or key in ("extra", "image_extra"):
            values[key] = value

    typed = _explicit_flags(argv)
    for field_name in TAG_FIELDS:
        value = getattr(args, field_name, None)
        if value is None:
            continue
        if field_name in values and field_name not in typed:
            continue  # keep the profile / settings value
        values[field_name] = value

    extra: Dict[str, str] = dict(values.pop("extra", {}) or {})
    extra.update(dict(getattr(args, "tag", None) or []))
    image_extra: Dict[str, str] = dict(values.pop("image_extra", {}) or {})
    image_extra.update(dict(getattr(args, "image_tag", None) or []))

    values.setdefault("research", settings.research)
    return ScriptRequest(
        topic=_resolve_topic(args), extra=extra, image_extra=image_extra, **values
    )


def apply_provider_overrides(settings: Settings, args: argparse.Namespace) -> None:
    provider = settings.provider
    if args.provider:
        provider.name = args.provider
    if args.model:
        provider.model = args.model
    if args.base_url:
        provider.base_url = args.base_url
    if args.api_key_env:
        key = os.environ.get(args.api_key_env)
        if not key:
            raise ConfigError(f"env var {args.api_key_env} is not set")
        provider.api_key = key
    if args.api_key:
        provider.api_key = args.api_key
    for name in ("temperature", "max_tokens", "timeout", "retries"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(provider, name, value)


def build_render_options(args: argparse.Namespace, request: ScriptRequest) -> RenderOptions:
    options = RenderOptions(image_prefix=request.image_prefix)
    if args.arrow:
        options.arrow = args.arrow
    if args.beat_label:
        options.beat_label = args.beat_label
    if args.compact_beats:
        options.beat_blank_lines = False
    if args.voiceover_layout:
        options.voiceover_layout = args.voiceover_layout
    if args.paragraph_beats:
        options.paragraph_beats = args.paragraph_beats
    if args.voiceover_file:
        options.filenames["voiceover"] = args.voiceover_file
    if args.beat_file:
        options.filenames["beats"] = args.beat_file
    if args.image_file:
        options.filenames["image_prompts"] = args.image_file
    return options


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_generate(args: argparse.Namespace, argv: Sequence[str]) -> int:
    log = setup_logging(args.verbose, args.quiet)

    settings = Settings.load(config_path=args.config, env_file=args.env_file)
    apply_provider_overrides(settings, args)
    request = build_request(args, settings, argv)

    outdir = Path(
        args.outdir
        or settings.output.get("outdir_full")
        or os.path.join(settings.output.get("outdir", "out"), slugify(request.topic))
    )
    options = build_render_options(args, request)

    if args.dry_run:
        plan = {
            "topic": request.topic,
            "beats": request.beat_count,
            "image_prompts": request.beat_count,
            "provider": settings.provider.name,
            "model": settings.provider.model,
            "research": request.research,
            "outdir": str(outdir),
            "files": list(options.filenames.values()),
            "image_prefix": request.image_prefix,
            "pipeline": Pipeline.default().names(),
            "narration_tags": request.narration_tags(),
            "visual_tags": request.visual_tags(),
        }
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0

    context = build_context(
        request, settings, research_kwargs={"path": args.research_file or ""}
    )
    log.info("provider: %s", context.provider.describe())
    script = Pipeline.default().run(context)
    files = render_all(script, options)

    if args.stdout:
        for name, content in files.items():
            print(f"==> {name} <==")
            print(content)
        return 0

    written: List[str] = []
    for name, content in files.items():
        written.append(str(write_text(outdir / name, content, force=args.force)))

    if args.save_json:
        path = Path(args.save_json)
        if not path.is_absolute() and path.parent == Path("."):
            path = outdir / path
        written.append(
            str(write_text(
                path,
                json.dumps(script.to_dict(), indent=2, ensure_ascii=False) + "\n",
                force=args.force,
            ))
        )

    if args.json:
        print(json.dumps(
            {
                "topic": script.topic,
                "title": script.title,
                "beat_count": script.beat_count,
                "image_prompt_count": script.beat_count,
                "word_count": script.word_count,
                "outdir": str(outdir),
                "files": written,
                "metadata": script.metadata,
            },
            indent=2,
            ensure_ascii=False,
        ))
    elif not args.quiet:
        print(f"{script.title}")
        print(f"{script.beat_count} beats \u00b7 {script.beat_count} image prompts \u00b7 "
              f"~{script.metadata.get('estimated_minutes', '?')} min \u00b7 {outdir}")
        for path_str in written:
            print(f"  {path_str}")
    return 0


INIT_ENV = None  # filled in lazily from .env.example when present


def cmd_init(args: argparse.Namespace, argv: Sequence[str]) -> int:
    setup_logging(getattr(args, "verbose", 0), getattr(args, "quiet", False))
    target = Path(args.dir)
    root = Path(__file__).resolve().parent.parent

    env_template = (root / ".env.example")
    settings_template = (root / "settings.example.json")

    env_text = env_template.read_text(encoding="utf-8") if env_template.is_file() else (
        "YTSCRIPT_PROVIDER=openai_compatible\n"
        "YTSCRIPT_API_KEY=REPLACE_WITH_API_KEY\n"
        "YTSCRIPT_MODEL=REPLACE_WITH_MODEL_NAME\n"
        "YTSCRIPT_BASE_URL=https://api.openai.com/v1\n"
    )
    settings_text = settings_template.read_text(encoding="utf-8") if settings_template.is_file() else json.dumps(
        {
            "provider": {
                "name": "openai_compatible",
                "api_key": "REPLACE_WITH_API_KEY",
                "model": "REPLACE_WITH_MODEL_NAME",
                "base_url": "https://api.openai.com/v1",
            }
        },
        indent=2,
    ) + "\n"

    written = [
        str(write_text(target / ".env.example", env_text, force=True)),
        str(write_text(target / "settings.json", settings_text, force=args.force)),
    ]
    env_path = target / ".env"
    if args.force or not env_path.exists():
        written.append(str(write_text(env_path, env_text, force=True)))

    print("wrote:")
    for path_str in written:
        print(f"  {path_str}")
    print("\nNext: put your api_key, model name and base url in .env (or settings.json),")
    print("then run:  python3 -m ytscript --topic \"Dog Psychology\" --generate")
    return 0


def cmd_tags(args: argparse.Namespace, argv: Sequence[str]) -> int:
    """Print every tag flag, grouped, straight from the parser definition."""
    generate = _add_generate(
        argparse.ArgumentParser(prog=f"{PROG} generate", add_help=False, formatter_class=_Formatter)
    )
    request = ScriptRequest(topic="example")

    for group in generate._action_groups:
        actions = [a for a in group._group_actions if a.option_strings]
        if not actions or group.title in ("positional arguments", "options"):
            continue
        print(f"\n{group.title}:")
        for action in actions:
            flags = ", ".join(action.option_strings)
            default = getattr(request, action.dest, None)
            shown = "" if default in (None, [], {}, False) else f"  [default: {default}]"
            print(f"  {flags:<34} {action.help or ''}{shown}")

    print("\nfreeform tags:")
    print("  --tag KEY=VALUE        any narration tag, passed through to the writer")
    print("  --image-tag KEY=VALUE  any visual tag, passed through to the art director")
    print('\nexample:\n  python3 -m ytscript -t "Dog Psychology" -g \\\n'
          '      --tone "dry, precise" --tag mood="quietly unsettling" \\\n'
          '      --image-tag film_stock="Portra 400"')
    return 0


def cmd_providers(args: argparse.Namespace, argv: Sequence[str]) -> int:
    print("providers (--provider):")
    for name, cls in PROVIDERS.items():
        needs_key = "api key required" if getattr(cls, "requires_api_key", True) else "no api key"
        print(f"  {name:<20} {needs_key}")
    print("\nresearch backends (--research):")
    for name, _ in RESEARCH.items():
        print(f"  {name}")
    print("\nrenderers (one output file each):")
    for name, _ in RENDERERS.items():
        print(f"  {name:<20} -> {DEFAULT_FILENAMES.get(name, name + '.md')}")
    print("\npipeline stages:")
    print("  " + " -> ".join(Pipeline.default().names()))
    print("\nAdd your own: see docs/EXTENDING.md")
    return 0


def cmd_doctor(args: argparse.Namespace, argv: Sequence[str]) -> int:
    setup_logging(getattr(args, "verbose", 0), getattr(args, "quiet", False))
    settings = Settings.load(config_path=args.config, env_file=args.env_file)
    provider = settings.provider

    key = (provider.api_key or "").strip()
    model = (provider.model or "").strip()
    needs_key = getattr(PROVIDERS.get(provider.name) if provider.name in PROVIDERS else None,
                        "requires_api_key", True)

    checks = {
        "provider_registered": provider.name in PROVIDERS,
        "api_key_set": bool(key) and not key.startswith("REPLACE_") if needs_key else True,
        "model_set": bool(model) and not model.startswith("REPLACE_"),
        "base_url_set": bool((provider.base_url or "").strip()) or not needs_key,
    }
    payload = {
        "version": __version__,
        "python": sys.version.split()[0],
        "settings": settings.to_dict(),
        "checks": checks,
        "ok": all(checks.values()),
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"{PROG} {__version__} (python {payload['python']})")
        print(f"config sources: {', '.join(settings.sources) or 'defaults + environment'}")
        print(f"provider:  {provider.name}")
        print(f"model:     {model or '(unset)'}")
        print(f"base_url:  {provider.base_url or '(unset)'}")
        print(f"api_key:   {provider.redacted()['api_key'] or '(unset)'}")
        print(f"research:  {settings.research}")
        print("")
        for name, ok in checks.items():
            print(f"  [{'ok' if ok else 'FAIL'}] {name}")
        if not payload["ok"]:
            print("\nFix: run `python3 -m ytscript init`, then edit .env")
            print("Or try it offline first: --provider mock")

    return 0 if payload["ok"] else 78


def cmd_version(args: argparse.Namespace, argv: Sequence[str]) -> int:
    print(f"{PROG} {__version__}")
    return 0


DISPATCH = {
    "generate": cmd_generate,
    "init": cmd_init,
    "tags": cmd_tags,
    "providers": cmd_providers,
    "doctor": cmd_doctor,
    "version": cmd_version,
}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def normalize_argv(argv: Sequence[str]) -> List[str]:
    """Make ``generate`` implicit so ``--topic X --generate`` works bare."""
    args = list(argv)
    if not args:
        return args
    first = args[0]
    if first in COMMANDS or first in ("-h", "--help", "-V", "--version"):
        return args
    return ["generate"] + args


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(normalize_argv(raw))

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return 0

    try:
        return DISPATCH[command](args, raw)
    except YtScriptError as exc:
        get_logger().error("%s", exc)
        if not get_logger().handlers:
            print(f"{PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
