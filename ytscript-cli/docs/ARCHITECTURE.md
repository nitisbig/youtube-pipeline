# Architecture

## Layout

```
ytscript/
  cli.py            argparse surface, flag → request mapping, exit codes
  config.py         layered settings (CLI > env > .env > settings.json)
  models.py         ScriptRequest / Beat / Outline / Script — the contracts
  pipeline.py       Context, Stage protocol, Pipeline, generate_script()
  registry.py       tiny name → class registry behind every plug point
  errors.py         typed errors carrying POSIX exit codes
  prompts/          all model-facing text
  providers/        LLM plugins (openai_compatible, anthropic, mock) + http
  generators/       outline → beats → image prompts
  renderers/        Script → markdown files (pure functions)
  research/         grounding plugins (none, file, …)
  utils/            io, logging, text/JSON helpers
bin/ytscript        run from a checkout without installing
tests/              contract tests on the offline mock provider
```

## Data flow

```
CLI flags ─┐
.env       ├─► Settings ─┐
settings   ┘             │
                         ├─► Context ─► Pipeline ─► Script ─► renderers ─► 3 files
ScriptRequest ───────────┘
                          research → outline → beats → image_prompts → validate
```

One beat is the atomic unit: **1 beat = 1 narration line = 1 image prompt**.
Because prompts are attached *to* beats (`Beat.image_prompt`) rather than kept in
a parallel list, the two counts are structurally identical — a mismatch is not
representable in the data model.

## Layers and their single responsibility

| Layer | Responsibility | Knows about |
| --- | --- | --- |
| `cli` | parse argv, resolve config, print/write, map errors → exit codes | everything |
| `pipeline` | order the work, hold shared state | models, generators, providers |
| `generators` | one model-facing job each, guarantee shape | prompts, provider, models |
| `providers` | text in → text out | config only |
| `renderers` | formatting | models only |
| `models` | data contracts | nothing |

Dependencies point inward. `models.py` imports nothing from the package, so any
external tool can build a `ScriptRequest` without pulling in HTTP or CLI code.

## Stages

| Stage | Does |
| --- | --- |
| `research` | Optional grounding notes (default `none`, no-op) |
| `outline` | Title, logline, 4–7 sections summing to the beat target |
| `beats` | Narration in chunks, each chunk seeded with the previous beats |
| `image_prompts` | One self-sufficient prompt per beat, chunked |
| `validate` | Enforces the invariants, populates run metadata |

Stages are plain objects with `name` and `run(context)`. Add, remove or reorder:

```python
Pipeline.default().insert_after("beats", FactCheckStage()).remove("research")
```

## Why chunking

A 90-beat script in one response is fragile: truncation, drifting formats,
miscounts. So beats are requested `--chunk-size` at a time (default 12) with the
last four beats replayed as continuity context. Costs scale linearly and stay
well inside any context window, and quality holds because each call has a narrow
job.

## Reliability ladder

Models miscount. The generators assume it:

1. Prompts state the exact index range and count, plus a machine-readable
   `SPEC:` line.
2. JSON is salvaged from fences/prose (`utils.text.extract_json`).
3. Over-long chunks are truncated; beats are renumbered `1..N`.
4. A missing beat or prompt triggers a **single-item repair call**.
5. If that fails, an image prompt is synthesised locally from the beat's visual
   hint plus the art-direction tags.
6. `ValidateStage` still checks everything and raises `ValidationError` (exit 65)
   rather than writing bad output.
7. HTTP failures retry with exponential backoff + jitter on retryable statuses
   only (`429`, `5xx`, …).

Every prompt is also forced through `ensure_prefix`, which strips a prefix the
model echoed and re-applies exactly one — so `image_prompts.md` cannot end up
with `create image with 16:9 ratio. create image with 16:9 ratio. …`.

## Conventions

* **Zero dependencies.** stdlib only (`urllib`, `json`, `argparse`), including
  the `.env` parser. Nothing to install, nothing to break.
* **stdout is data, stderr is chatter.** Logging always goes to stderr.
* **Typed errors.** Every failure is a `YtScriptError` subclass with an
  `exit_code`; the CLI never leaks a traceback.
* **Atomic writes.** Files are written to a temp file then `os.replace`d, so an
  interrupted run cannot leave a half-written `voiceover.md`.
* **Registries over conditionals.** No `if provider == "openai"` anywhere.
