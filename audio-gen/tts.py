#!/usr/bin/env python3
"""
tts.py - Command-line text-to-speech generator using the Fish Audio API.

Examples:
    python3 tts.py --path notes.md --reference-id b347db033a6549378b48d00acb0d06cd --out audio.mp3
    python3 tts.py --text "Hello there" --reference-id <id> --out hello.mp3 --speed 0.95
    cat script.txt | python3 tts.py --reference-id <id> --out hello.wav --format wav

Auth:
    Put FISH_API_KEY=... in a .env file (auto-loaded from the current directory),
    export it in your shell, or pass --api-key explicitly.

Reliability:
    Network errors, 429 and 5xx responses are retried with exponential backoff
    (--retries, default 3). The audio is written to a temp file and renamed
    only when the download completed, so a half-written file never looks done.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests

API_URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"
VALID_FORMATS = {"mp3", "wav", "pcm", "opus"}
DEFAULT_ENV_FILE = ".env"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def load_env_file(path: Path, verbose: bool = False) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Real environment variables always win - a value already set in the
    shell is never overwritten by the file. No external dependency
    (python-dotenv) required.
    """
    if not path.is_file():
        return

    loaded = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    if verbose:
        print(f"loaded {loaded} var(s) from {path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate speech audio from text using the Fish Audio TTS API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    text_group = parser.add_mutually_exclusive_group(required=False)
    text_group.add_argument("--path", "-p", type=str,
                            help="Path to a text/markdown file whose contents will be converted to speech.")
    text_group.add_argument("--text", "-t", type=str,
                            help="Literal text to convert to speech (alternative to --path).")

    parser.add_argument("--reference-id", "-r", required=True, help="Fish Audio voice reference ID to use.")
    parser.add_argument("--out", "-o", required=True, help="Output audio file path, e.g. audio.mp3.")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="TTS model to use.")
    parser.add_argument("--format", "-f", default=None, choices=sorted(VALID_FORMATS),
                        help="Output audio format. Defaults to the --out file extension (falls back to mp3).")
    parser.add_argument("--speed", type=float, default=None,
                        help="Speech speed multiplier, e.g. 0.9 (slower) or 1.1 (faster).")
    parser.add_argument("--api-key", default=None,
                        help="Fish Audio API key. Defaults to the FISH_API_KEY environment variable.")
    parser.add_argument("--env-file", default=None,
                        help=f"Path to a .env file to load. Defaults to '{DEFAULT_ENV_FILE}' in the current "
                             "directory if it exists; pass an explicit path to require it.")
    parser.add_argument("--mp3-bitrate", type=int, default=None, choices=[64, 128, 192], help="Bitrate for mp3 output.")
    parser.add_argument("--chunk-length", type=int, default=None,
                        help="Optional chunk length (characters) passed through to the API for long-form text.")
    parser.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3,
                        help="Retries on network errors, 429 and 5xx responses.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve inputs and print the request that would be sent, without calling the API.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print request details before sending.")

    return parser.parse_args()


def resolve_text(args) -> str:
    if args.path:
        file_path = Path(args.path).expanduser().resolve()
        if not file_path.is_file():
            sys.exit(f"error: input file not found: {file_path}")
        text = file_path.read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text.strip()
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        sys.exit("error: provide --path, --text, or pipe text via stdin.")

    if not text:
        sys.exit("error: no text content to synthesize.")
    return text


def resolve_api_key(args) -> str:
    api_key = args.api_key or os.environ.get("FISH_API_KEY")
    if not api_key:
        sys.exit("error: no API key found. Set the FISH_API_KEY environment variable or pass --api-key.")
    return api_key


def resolve_format(args) -> str:
    if args.format:
        return args.format
    ext = Path(args.out).suffix.lstrip(".").lower()
    return ext if ext in VALID_FORMATS else "mp3"


def retry_delay(response, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(120.0, float(retry_after) + 1.0)
            except ValueError:
                pass
    return min(60.0, float(2 ** attempt))


def request_with_retries(headers, payload, timeout: int, retries: int):
    """POST until a 200 arrives; retry transient failures with backoff."""
    attempt = 0
    while True:
        attempt += 1
        response = None
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=timeout, stream=True)
        except requests.exceptions.RequestException as exc:
            error = f"request failed: {exc}"
        else:
            if response.status_code == 200:
                return response
            error = f"API returned {response.status_code}: {response.text[:500]}"
            if response.status_code not in RETRYABLE_STATUS:
                sys.exit(f"error: {error}")

        if attempt > retries:
            sys.exit(f"error: {error} (gave up after {attempt} attempt(s))")

        delay = retry_delay(response, attempt)
        print(f"warning: {error}; retrying in {delay:.0f}s ({attempt}/{retries})", file=sys.stderr)
        time.sleep(delay)


def main():
    args = parse_args()

    if args.env_file:
        env_path = Path(args.env_file).expanduser()
        if not env_path.is_file():
            sys.exit(f"error: env file not found: {env_path}")
    else:
        env_path = Path(DEFAULT_ENV_FILE)
    load_env_file(env_path, verbose=args.verbose)

    text = resolve_text(args)
    audio_format = resolve_format(args)

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "text": text,
        "reference_id": args.reference_id,
        "format": audio_format,
    }
    if args.mp3_bitrate:
        payload["mp3_bitrate"] = args.mp3_bitrate
    if args.chunk_length:
        payload["chunk_length"] = args.chunk_length
    if args.speed is not None:
        if not 0.5 <= args.speed <= 2.0:
            sys.exit("error: --speed must be between 0.5 and 2.0")
        payload["prosody"] = {"speed": args.speed}

    if args.verbose or args.dry_run:
        print(f"POST {API_URL}")
        print(f"  model:        {args.model}")
        print(f"  reference_id: {args.reference_id}")
        print(f"  format:       {audio_format}")
        print(f"  speed:        {args.speed if args.speed is not None else 'default'}")
        print(f"  text length:  {len(text)} chars")
        print(f"  out:          {out_path}")

    if args.dry_run:
        print("(dry run - no request sent)")
        return

    api_key = resolve_api_key(args)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": args.model,
    }

    response = request_with_retries(headers, payload, args.timeout, max(0, args.retries))

    content_type = response.headers.get("content-type", "")
    if "audio" not in content_type and "octet-stream" not in content_type:
        sys.exit(
            f"error: unexpected response content-type '{content_type}'. "
            f"Body preview: {response.text[:300]}"
        )

    tmp_path = out_path.with_name(out_path.name + ".part")
    written = 0
    try:
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    except requests.exceptions.RequestException as exc:
        tmp_path.unlink(missing_ok=True)
        sys.exit(f"error: download interrupted: {exc}")

    if written == 0:
        tmp_path.unlink(missing_ok=True)
        sys.exit("error: the API returned an empty audio stream.")

    tmp_path.replace(out_path)
    print(f"\u2713 wrote {out_path} ({written / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
