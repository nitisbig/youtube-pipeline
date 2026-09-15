# ytscript

A small, modular, **provider-agnostic** CLI that turns a topic into a ready-to-produce
faceless-YouTube script package:

| File | Contents |
| --- | --- |
| `voiceover.md` | plain narration text only — nothing but words to read aloud |
| `beat.md` | `beat1 → voiceover` , one line per beat |
| `image_prompts.md` | one **self-sufficient** prompt per beat, each starting with `create image with 16:9 ratio.`, separated by a blank line |

**Invariant the tool enforces:** `total beats == total image prompts`. Always. A
validation stage fails the run rather than shipping a mismatch.

---

## Quick start

```sh
unzip ytscript-cli.zip && cd ytscript-cli

# 1. try it offline, no API key needed (deterministic mock provider)
python3 -m ytscript --topic "Dog Psychology" --provider mock --generate

# 2. add credentials
cp .env.example .env        # or: python3 -m ytscript init
$EDITOR .env                # api_key, model name, base url

# 3. real run
python3 -m ytscript --topic "Dog Psychology" --generate
```

Output lands in `out/dog-psychology/`:

```
out/dog-psychology/voiceover.md
out/dog-psychology/beat.md
out/dog-psychology/image_prompts.md
```

No third-party dependencies. Python 3.9+. `pip install -e .` is optional and only
adds the `ytscript` command; `python3 -m ytscript` and `./bin/ytscript` work from a
plain checkout.

---

## The workflow you asked for

```sh
python3 -m ytscript --topic "Dog Psychology" --generate
```

`generate` is the implicit default command, so no subcommand is needed. `--generate`
(`-g`) is accepted explicitly for readability and starts generation immediately: no
prompts, no wizard, no confirmation.

More shapes of the same thing:

```sh
# exactly 90 beats -> exactly 90 image prompts
python3 -m ytscript -t "Dog Psychology" -g -b 90

# 12-minute documentary, custom art direction, custom output dir
python3 -m ytscript -t "Roman Roads" -g \
  --duration 12 --tone "epic, measured" --depth deep-dive \
  --art-style "2.5D parallax illustration" --palette "ochre and slate" \
  --keyword aqueduct --keyword legion --avoid "clickbait" \
  -o out/roman-roads

# preset from settings.json + machine-readable summary
python3 -m ytscript -t "Deep Sea" -g --profile shorts --json | jq .files

# topic from stdin, files to stdout (nothing written to disk)
echo "Dog Psychology" | python3 -m ytscript -t - -g --stdout

# see the plan without spending a token
python3 -m ytscript -t "Dog Psychology" --dry-run
```

---

## Unix / POSIX behaviour

* short + long flags (`-t` / `--topic`), `--force` / `--no-clobber` pairs
* **stdout is data, stderr is chatter** — `--json` output pipes cleanly into `jq`
* `-` means read from stdin
* `-v` / `-vv` raise verbosity, `-q` silences everything but errors
* exits with meaningful codes (`sysexits.h`)

| Code | Meaning |
| --- | --- |
| 0 | success |
| 64 | usage error (e.g. missing topic) |
| 65 | model returned unusable data / invariant broken |
| 69 | provider unavailable (HTTP, timeout) |
| 73 | cannot write output (e.g. `--no-clobber` hit) |
| 78 | configuration error (missing key/model/base url) |
| 130 | interrupted (Ctrl-C) |

---

## Commands

```sh
ytscript generate ...   # default; create the three files
ytscript init           # write .env / .env.example / settings.json templates
ytscript tags           # print every custom tag option, grouped
ytscript providers      # list providers, renderers, research backends, stages
ytscript doctor         # show resolved config + credential checks (exit 78 if incomplete)
ytscript version
```

---

## Configuration (provider agnostic)

Precedence, highest first:

```
CLI flags  >  environment  >  .env  >  settings.json  >  built-in defaults
```

`.env` placeholders:

```ini
YTSCRIPT_PROVIDER=openai_compatible
YTSCRIPT_API_KEY=REPLACE_WITH_API_KEY
YTSCRIPT_MODEL=REPLACE_WITH_MODEL_NAME
YTSCRIPT_BASE_URL=https://api.openai.com/v1
```

`settings.json` (same fields, plus reusable tag defaults and profiles):

```json
{
  "provider": {
    "name": "openai_compatible",
    "api_key": "REPLACE_WITH_API_KEY",
    "model": "REPLACE_WITH_MODEL_NAME",
    "base_url": "https://api.openai.com/v1"
  },
  "defaults": { "tone": "curious, warm, documentary" },
  "profiles": { "shorts": { "beats": 18, "aspect_ratio": "9:16" } }
}
```

The tool never hardcodes a vendor. Anything speaking the OpenAI
`/chat/completions` shape works by changing `base_url` + `model`:
OpenAI, OpenRouter, Groq, DeepSeek, Together, Fireworks, Mistral, vLLM,
LM Studio, Ollama (`http://localhost:11434/v1`). Anthropic's `/v1/messages`
format ships as a second provider (`--provider anthropic`) to prove the seam.

Check what the tool actually resolved:

```sh
ytscript doctor          # api_key is redacted in the output
```

---

## Custom tags

Everything below is a flag; `ytscript tags` prints the same list with defaults.

**Structure** — `--beats/-b`, `--duration/-D`, `--wpm`, `--words-per-beat`, `--chunk-size`

**Narration** — `--language`, `--tone`, `--style`, `--audience`, `--narrator`,
`--pov`, `--hook`, `--cta`, `--depth`, `--pacing`, `--humor`, `--emotion`,
`--genre`, `--series`, `--reading-level`, `--keyword/-k` (repeatable),
`--avoid/-x` (repeatable), `--title`

**Images** — `--aspect-ratio/-r`, `--image-style`, `--art-style`, `--palette`,
`--lighting`, `--camera`, `--composition`, `--subject-lock`,
`--negative-prompt`, `--image-prefix`

**Anything else** — freeform tags are passed straight through to the prompts:

```sh
--tag        mood="quietly unsettling"    # narration tag
--image-tag  film_stock="Kodak Portra 400" # art-direction tag
```

**Output** — `--outdir/-o`, `--voiceover-file`, `--beat-file`, `--image-file`,
`--voiceover-layout {paragraphs,lines,single}`, `--paragraph-beats`, `--arrow`,
`--beat-label`, `--compact-beats`, `--save-json`, `--force/-f`,
`--no-clobber/-n`, `--stdout`, `--json`

**Model** — `--provider/-p`, `--model/-m`, `--base-url/-u`, `--api-key`,
`--api-key-env`, `--temperature`, `--max-tokens`, `--timeout`, `--retries`, `--seed`

**Meta** — `--config/-c`, `--env-file`, `--profile/-P`, `--research`,
`--research-file`, `--dry-run`, `-v/-vv`, `-q`

Beat count rule: `--beats` wins; otherwise
`ceil(duration × wpm ÷ words_per_beat)` (default `7 × 150 ÷ 12 = 88`).

---

## Architecture

```
ytscript/
  cli.py            argparse surface, flag -> request mapping, exit codes
  config.py         layered settings: CLI > env > .env > settings.json
  models.py         ScriptRequest / Beat / Outline / Script  (the contracts)
  pipeline.py       Context + Stage protocol + Pipeline + generate_script()
  registry.py       tiny name -> class registry used by every plug point
  prompts/          all model-facing text (tune voice here, no code changes)
  providers/        LLM plugins: openai_compatible, anthropic, mock
  generators/       outline -> beats -> image prompts (chunked, self-repairing)
  renderers/        Script -> the three markdown files (pure functions)
  research/         extension point for web research (default: none)
  utils/            io, logging, text/JSON salvage helpers
```

Pipeline: `research → outline → beats → image_prompts → validate`

Why it scales: beats are generated in **chunks** (`--chunk-size`, default 12) with
the previous beats passed as continuity context, so a 200-beat script never
overflows the context window. Any beat the model skips is repaired with a
single-beat call, then with a locally synthesised prompt — the count invariant
cannot break.

---

## Using it from another agent or tool

The CLI is a thin shell over a stable Python API:

```python
from ytscript import RenderOptions, ScriptRequest, Settings, generate_script, render_all

settings = Settings.load()                       # .env / settings.json / env
request  = ScriptRequest(topic="Dog Psychology", beats=90, tone="dry, precise")
script   = generate_script(request, settings)     # -> Script (beats + prompts)

files = render_all(script, RenderOptions())       # {"voiceover.md": "...", ...}
assert len(script.beats) == 90
```

Or consume the CLI as a subprocess and read JSON off stdout:

```sh
ytscript -t "Dog Psychology" -g --json --save-json
```

---

## Extending it

Every extension point is a registry + a one-method class. No core edits.

**New LLM / agent backend**

```python
from ytscript.providers import PROVIDERS, CompletionRequest, LLMProvider

@PROVIDERS.register("my_llm")
class MyProvider(LLMProvider):
    name = "my_llm"
    def complete(self, request: CompletionRequest) -> str:
        return my_client.generate(request.prompt, system=request.system)
```

`ytscript -p my_llm ...`

**Web research (the feature you plan to add)**

```python
from ytscript.research import RESEARCH, ResearchProvider, ResearchResult

@RESEARCH.register("web")
class WebResearch(ResearchProvider):
    name = "web"
    def gather(self, request):
        hits = my_search_api(request.topic)
        return ResearchResult(notes="\n".join(h["text"] for h in hits),
                              sources=[h["url"] for h in hits])
```

`ytscript -t "..." -g --research web` — notes are injected into the outline prompt
and recorded in the run metadata. A zero-dependency `file` backend already exists:
`--research-file notes.md`.

**New output file**

```python
from ytscript.renderers import RENDERERS

@RENDERERS.register("srt")
def render_srt(script, options):
    return "\n".join(f"{b.index}\n{b.narration}\n" for b in script.beats)
```

**New pipeline stage**

```python
from ytscript import Pipeline

class FactCheckStage:
    name = "fact_check"
    def run(self, context): ...

pipeline = Pipeline.default().insert_after("beats", FactCheckStage())
```

---

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite runs the entire pipeline on the offline `mock` provider and asserts the
contracts: beat count, `beats == image prompts`, the `create image with 16:9
ratio.` prefix on every prompt, blank-line separation, `beatN → text` formatting,
plain-text voiceover, config precedence and CLI exit codes.

---

## Docs

* `docs/USAGE.md` — every flag, with recipes
* `docs/ARCHITECTURE.md` — modules, data flow, invariants, design rationale
* `docs/EXTENDING.md` — providers, research, renderers, stages, agent integration
* `docs/CONFIGURATION.md` — precedence, `.env`, `settings.json`, profiles, providers

## License

MIT
