# Usage

## Invocation

Three equivalent ways to run it:

```sh
python3 -m ytscript ...      # from a checkout, no install
./bin/ytscript ...           # executable shim
ytscript ...                 # after: pip install -e .
```

`generate` is the implicit default command:

```sh
python3 -m ytscript --topic "Dog Psychology" --generate
python3 -m ytscript generate --topic "Dog Psychology"   # identical
```

## Commands

| Command | Purpose |
| --- | --- |
| `generate` | Create `voiceover.md`, `beat.md`, `image_prompts.md` (default) |
| `init` | Write `.env`, `.env.example`, `settings.json` templates |
| `tags` | Print every tag flag, grouped, with defaults |
| `providers` | List providers, renderers, research backends, pipeline stages |
| `doctor` | Show resolved config + credential checks (exit 78 if incomplete) |
| `version` | Print version |

## Output files

`voiceover.md` — plain narration, nothing else:

```
Most people think they know what a dog is feeling. ...

But the science tells a stranger story. ...
```

`beat.md` — one entry per beat:

```
beat1 → Most people think they know what a dog is feeling.

beat2 → But the science tells a stranger story.
```

`image_prompts.md` — self-sufficient prompts, blank line between each:

```
create image with 16:9 ratio. Wide establishing shot of a golden retriever ...

create image with 16:9 ratio. Slow push-in close-up of the same dog's eyes ...
```

The number of prompts always equals the number of beats. If the model drops one,
the generator repairs it; if validation still fails, the run exits `65` rather
than writing a mismatched set.

## Beat count

* `--beats/-b N` sets it exactly.
* Otherwise: `ceil(duration × wpm ÷ words_per_beat)`.
* Defaults: `7 min × 150 wpm ÷ 12 words = 88 beats`.

```sh
-b 90                                  # 90 beats, 90 prompts
-D 12                                  # 12 minutes → 150 beats
-D 10 --wpm 165 --words-per-beat 15     # 110 beats
```

## Flag reference

### Input
| Flag | Notes |
| --- | --- |
| `-t, --topic TEXT` | Required. `-` reads the topic from stdin. |
| `-g, --generate` | Explicit "go now" flag (generation is the default action). |
| `--title TEXT` | Force the title instead of letting the model write one. |

### Structure
| Flag | Default |
| --- | --- |
| `-b, --beats N` | derived |
| `-D, --duration MIN` | `7` |
| `--wpm N` | `150` |
| `--words-per-beat N` | `12` |
| `--chunk-size N` | `12` beats per model call |

### Narration tags
`--language` `--tone` `--style` `--audience` `--narrator` `--pov` `--hook`
`--cta` `--depth` `--pacing` `--humor` `--emotion` `--genre` `--series`
`--reading-level` `-k/--keyword` (repeatable) `-x/--avoid` (repeatable)
`--tag KEY=VALUE` (repeatable, freeform)

### Image / art direction tags
`-r/--aspect-ratio` `--image-style` `--art-style` `--palette` `--lighting`
`--camera` `--composition` `--subject-lock` `--negative-prompt`
`--image-prefix` `--image-tag KEY=VALUE` (repeatable, freeform)

`--subject-lock` is the consistency trick for faceless video: the description you
pass is repeated inside every prompt so the recurring character or object does
not drift between images.

`--image-prefix` is a template with `{aspect_ratio}` and `{topic}` available:

```sh
--image-prefix "create image with {aspect_ratio} ratio."      # default
--image-prefix "/imagine prompt:"                              # Midjourney style
```

### Output
| Flag | Notes |
| --- | --- |
| `-o, --outdir DIR` | Default `out/<topic-slug>` |
| `--voiceover-file` `--beat-file` `--image-file` | Rename outputs |
| `--voiceover-layout {paragraphs,lines,single}` | Default `paragraphs` |
| `--paragraph-beats N` | Beats per paragraph (default `4`) |
| `--arrow STR` | Separator in `beat.md` (default `→`) |
| `--beat-label STR` | Prefix in `beat.md` (default `beat`) |
| `--compact-beats` | No blank line between beats |
| `--save-json [PATH]` | Also dump the full script as JSON |
| `-f, --force` / `-n, --no-clobber` | Overwrite policy (force is default) |
| `--stdout` | Print files instead of writing them |
| `--json` | Machine-readable summary on stdout |

### Model / provider
`-p/--provider` `-m/--model` `-u/--base-url` `--api-key` `--api-key-env`
`--temperature` `--max-tokens` `--timeout` `--retries` `--seed`

Prefer `--api-key-env MY_VAR` over `--api-key` so secrets never land in shell
history.

### Research
`--research {none,file,...}` `--research-file PATH`

### Meta
`-c/--config` `--env-file` `-P/--profile` `--dry-run` `-v/-vv` `-q`

## Recipes

```sh
# offline smoke test, no API key
python3 -m ytscript -t "Dog Psychology" -g -p mock -b 12 -o /tmp/demo

# 90-beat mythology episode with locked art direction
python3 -m ytscript -t "Swayamprabha" -g -b 90 \
  --genre mythology --tone "reverent, cinematic" \
  --art-style "2.5D parallax illustration" \
  --palette "gold, deep indigo, ember orange" \
  --subject-lock "a calm ascetic woman in saffron robes, silver bangles" \
  --negative-prompt "modern clothing, text, logos, watermarks"

# vertical shorts via a saved profile
python3 -m ytscript -t "Why cats knead" -g -P shorts

# grounded in your own notes
python3 -m ytscript -t "GLP-1 drugs" -g --research-file notes.md

# pipe-friendly automation
python3 -m ytscript -t "Dog Psychology" -g --json | jq -r '.files[]'

# inspect resolved config, then the plan, then run
python3 -m ytscript doctor
python3 -m ytscript -t "Dog Psychology" --dry-run
python3 -m ytscript -t "Dog Psychology" -g -vv
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 64 | usage error |
| 65 | unusable model output / invariant broken |
| 69 | provider unavailable |
| 73 | cannot write output |
| 78 | configuration error |
| 130 | interrupted |

```sh
python3 -m ytscript -t "X" -g || echo "failed with $?"
```
