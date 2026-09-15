# Extending ytscript

Every extension point is a registry plus a one-method class. You never edit core
files to add capability.

```sh
python3 -m ytscript providers   # see what is currently registered
```

## 1. A new LLM / agent backend

```python
# ytscript/providers/my_llm.py
from .base import PROVIDERS, CompletionRequest, LLMProvider

@PROVIDERS.register("my_llm")
class MyProvider(LLMProvider):
    name = "my_llm"
    supports_json_mode = False   # True if the API accepts a JSON response format
    requires_api_key = False     # False for local models

    def complete(self, request: CompletionRequest) -> str:
        return my_client.generate(
            request.prompt,
            system=request.system,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
```

Import it in `ytscript/providers/__init__.py` (importing = registering), then:

```sh
python3 -m ytscript -t "Topic" -g -p my_llm
```

Most vendors need no code at all — if they speak `/chat/completions`, just set
`base_url` and `model`.

## 2. Web research (the planned feature)

The pipeline already runs a research stage and injects its notes into the outline
prompt. Implement `gather` and register it:

```python
# ytscript/research/web.py
from .base import RESEARCH, ResearchProvider, ResearchResult

@RESEARCH.register("web")
class WebResearch(ResearchProvider):
    name = "web"

    def __init__(self, max_results: int = 8, **_):
        self.max_results = max_results

    def gather(self, request):
        hits = search_api(request.topic, limit=self.max_results)
        notes = "\n\n".join(f"[{h['title']}] {h['snippet']}" for h in hits)
        return ResearchResult(notes=notes, sources=[h["url"] for h in hits])
```

```sh
python3 -m ytscript -t "GLP-1 drugs" -g --research web
```

Sources are recorded in `script.metadata["research"]`, so `--save-json` gives you
a citation trail. A dependency-free `file` backend already ships:
`--research-file notes.md`.

Want research to also influence individual beats? Add a stage (see §4) that
enriches `context.beats` before `image_prompts`.

## 3. A new output file

```python
# ytscript/renderers/srt.py
from .markdown import RENDERERS

@RENDERERS.register("captions")
def render_captions(script, options):
    lines = []
    for beat in script.beats:
        lines.append(f"{beat.index}\n{beat.narration}\n")
    return "\n".join(lines)
```

`render_all` writes every registered renderer, using
`RenderOptions.filenames["captions"]` or `captions.md` as a fallback. Set a
nicer name:

```python
RenderOptions(filenames={..., "captions": "captions.srt"})
```

## 4. A new pipeline stage

```python
from ytscript import Pipeline

class FactCheckStage:
    name = "fact_check"

    def run(self, context):
        for beat in context.beats:
            if "scientists agree" in beat.narration.lower():
                beat.narration = beat.narration.replace(
                    "scientists agree", "most researchers currently think"
                )
        context.metadata["fact_checked"] = True

pipeline = Pipeline.default().insert_after("beats", FactCheckStage())
```

A stage may read and mutate `context.request`, `context.outline`,
`context.beats`, `context.research_result` and `context.metadata`.

## 5. Calling ytscript from another agent or tool

### In-process (richest)

```python
from ytscript import (
    Pipeline, RenderOptions, ScriptRequest, Settings, build_context, render_all,
)

settings = Settings.load(config_path="settings.json")
request = ScriptRequest(
    topic="Dog Psychology",
    beats=90,
    tone="dry, precise",
    extra={"mood": "quietly unsettling"},      # freeform narration tags
    image_extra={"film_stock": "Portra 400"},  # freeform visual tags
)

context = build_context(request, settings)
script = Pipeline.default().run(context)

for beat in script.beats:
    print(beat.index, beat.narration, beat.image_prompt)

files = render_all(script, RenderOptions())   # {"voiceover.md": "...", ...}
```

### As a subprocess (language agnostic)

```python
import json, subprocess

out = subprocess.run(
    ["python3", "-m", "ytscript", "-t", "Dog Psychology", "-g",
     "--json", "--save-json", "-o", "out/dog"],
    capture_output=True, text=True, check=True,
)
result = json.loads(out.stdout)   # title, counts, outdir, files[]
```

Stdout stays clean JSON because all logging goes to stderr. Exit codes are
stable, so a wrapper can distinguish "bad config" (78) from "provider down" (69)
and retry accordingly.

### Use the model as your own agent step

Skip the pipeline and reuse just the plumbing:

```python
from ytscript import Settings
from ytscript.providers import get_provider

provider = get_provider(Settings.load().provider)
print(provider.ask("Summarise this topic in one line: dog psychology"))
```

## 6. Changing voice and quality without touching code paths

All model-facing text lives in `ytscript/prompts/templates.py`. Edit the rules
there (beat length, forbidden phrasing, prompt word count) and every generator
picks it up. If you add a new tag to `ScriptRequest`, surface it by adding one
line to `narration_tags()` or `visual_tags()` — prompts build their tag blocks
from those dicts automatically.

## 7. Testing your extension

Use the `mock` provider so tests stay offline and deterministic:

```python
from ytscript import ScriptRequest, Settings, generate_script

settings = Settings()
settings.provider.name = "mock"
script = generate_script(ScriptRequest(topic="Test", beats=6), settings)
assert len(script.beats) == 6
```

```sh
python3 -m unittest discover -s tests -v
```
