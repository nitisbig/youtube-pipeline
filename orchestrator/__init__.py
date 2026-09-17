"""
Orchestrator package for the YouTube video-gen pipeline.

Modules:
    config    - load/save settings.json
    jobs      - definitions + command builders for the 7 worker steps
    state     - per-project resumable state (.pipeline_state.json)
    pipeline  - the engine that actually runs the workers in a background thread
    gui       - the Tkinter front-end
"""
