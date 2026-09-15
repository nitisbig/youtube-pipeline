# Configuration

The tool is vendor-neutral: a provider is just a **name**, an **api_key**, a
**model name** and a **base url**.

## Precedence

```
CLI flags  >  process env  >  .env  >  settings.json  >  built-in defaults
```

For creative tags the rule is slightly friendlier: a value in
`settings.json` → `defaults` or in a `--profile` beats the *untouched* CLI
default, but any flag you actually type always wins.

See exactly what got resolved (the key is redacted):

```sh
python3 -m ytscript doctor
python3 -m ytscript doctor --json | jq .checks
```

`doctor` exits `78` when the key, model or base url is missing or still a
`REPLACE_…` placeholder.

## Files

Generate templates:

```sh
python3 -m ytscript init            # .env, .env.example, settings.json
python3 -m ytscript init -d . -f    # overwrite existing
```

Discovery order for `settings.json`: `--config PATH`, then `./settings.json`,
then `./.ytscript.json`, then `~/.config/ytscript/settings.json`.
For env: `--env-file PATH`, then `./.env`. Real environment variables are never
overwritten by `.env`.

### .env

```ini
YTSCRIPT_PROVIDER=openai_compatible
YTSCRIPT_API_KEY=REPLACE_WITH_API_KEY
YTSCRIPT_MODEL=REPLACE_WITH_MODEL_NAME
YTSCRIPT_BASE_URL=https://api.openai.com/v1

# optional
YTSCRIPT_TEMPERATURE=0.8
YTSCRIPT_MAX_TOKENS=4096
YTSCRIPT_TIMEOUT=120
YTSCRIPT_RETRIES=2
YTSCRIPT_OUTDIR=out
YTSCRIPT_RESEARCH=none
```

`OPENAI_API_KEY`, `LLM_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` (and similar
generic names) are accepted as fallbacks, so an existing shell setup often works
with no `.env` at all.

### settings.json

```json
{
  "provider": {
    "name": "openai_compatible",
    "api_key": "REPLACE_WITH_API_KEY",
    "model": "REPLACE_WITH_MODEL_NAME",
    "base_url": "https://api.openai.com/v1",
    "temperature": 0.8,
    "max_tokens": 4096,
    "timeout": 120,
    "retries": 2,
    "extra_headers": { "HTTP-Referer": "https://example.com" },
    "extra_body": { "top_p": 0.9 }
  },
  "research": "none",
  "output": { "outdir": "out" },
  "defaults": { "tone": "curious, warm, documentary", "aspect_ratio": "16:9" },
  "profiles": {
    "shorts": { "beats": 18, "words_per_beat": 9, "aspect_ratio": "9:16" }
  }
}
```

* `provider.extra_headers` / `extra_body` are merged into every request — use
  them for gateway headers or vendor-specific sampling params.
* `defaults` accepts **any** `ScriptRequest` field, including `extra` and
  `image_extra` maps of freeform tags.
* Passing `"provider": "mock"` as a bare string is also accepted.

## Profiles

A profile is a named bundle of tag defaults:

```sh
python3 -m ytscript -t "Why cats knead" -g --profile shorts
python3 -m ytscript -t "Roman Roads"    -g -P documentary -b 140   # -b still wins
```

Shipped in `settings.example.json`: `shorts`, `documentary`, `mythology`.

## Provider matrix

| Provider | `provider` | `base_url` |
| --- | --- | --- |
| OpenAI | `openai_compatible` | `https://api.openai.com/v1` |
| OpenRouter | `openai_compatible` | `https://openrouter.ai/api/v1` |
| Groq | `openai_compatible` | `https://api.groq.com/openai/v1` |
| DeepSeek | `openai_compatible` | `https://api.deepseek.com/v1` |
| Together | `openai_compatible` | `https://api.together.xyz/v1` |
| Mistral | `openai_compatible` | `https://api.mistral.ai/v1` |
| vLLM / LM Studio | `openai_compatible` | `http://localhost:8000/v1` |
| Ollama | `openai_compatible` | `http://localhost:11434/v1` (any key) |
| Anthropic | `anthropic` | `https://api.anthropic.com` |
| Offline demo | `mock` | — (no key, deterministic) |

Per-run override, no files touched:

```sh
python3 -m ytscript -t "Topic" -g \
  -p openai_compatible -m llama-3.3-70b -u https://api.groq.com/openai/v1 \
  --api-key-env GROQ_API_KEY
```

## Secrets hygiene

* Prefer `--api-key-env VAR` over `--api-key` so keys stay out of shell history
  and process listings.
* `.env` and `settings.json` are git-ignored; `.env.example` and
  `settings.example.json` (placeholders only) are the files you commit.
* `doctor` and `--save-json` never print a full key.

## Tuning notes

| Knob | Effect |
| --- | --- |
| `--chunk-size` | Beats per model call. Lower = more calls, tighter adherence. |
| `--temperature` | Lower (~0.5) for factual topics, higher (~0.9) for narrative. |
| `--max-tokens` | Must comfortably fit one chunk of JSON; raise with chunk size. |
| `--retries` | Retries on `429`/`5xx` with exponential backoff and jitter. |
| `--seed` | Reproducibility, when the provider honours it. |
