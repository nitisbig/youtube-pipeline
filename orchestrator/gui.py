"""
Tkinter GUI for the pipeline orchestrator.

Layout:
    - Project panel with three tabs:
        Project            title, output folder, duration, beats, aspect ratio, art style
        Script & Voice     free-form instructions for the script writer, tone, depth, voice
        Video & Animation  animation preset, transition, smoothness, fit, fps, zoom, image folder
    - Steps panel: one row per worker with live status, elapsed time and
      Run / Reset buttons for manual/one-off triggering
    - Controls: START / STOP / RESUME, Save params, Open folder, Open final video, QUIT
    - Progress bar + log console (also written to <project>/pipeline.log)

The GUI is the source of truth for a project's parameters: whatever the
fields show is written into the loaded project right before every run,
so changing e.g. the animation and clicking "Run" on the editor row just
works.

Threading note: PipelineEngine calls its callbacks from a background
thread. Tkinter widgets may only be touched from the main thread, so
those callbacks just push onto a queue.Queue, and App._poll_queue()
(scheduled via root.after) drains it on the main thread.
"""

import json
import os
import queue
import re
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .config import Config
from .pipeline import PipelineEngine
from . import jobs as job_defs
from .state import STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED

STATUS_COLORS = {
    STATUS_PENDING: "#888888",
    STATUS_RUNNING: "#1a73e8",
    STATUS_SUCCESS: "#188038",
    STATUS_FAILED: "#d93025",
    STATUS_STOPPED: "#e8710a",
}

STATUS_LABELS = {
    STATUS_PENDING: "pending",
    STATUS_RUNNING: "running...",
    STATUS_SUCCESS: "done",
    STATUS_FAILED: "failed",
    STATUS_STOPPED: "stopped",
}

POLL_MS = 100
MAX_LOG_LINES = 5000


def slugify(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "untitled"


def open_path(path):
    """Open a file or folder with the desktop's default handler."""
    path = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - desktop integration
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def format_elapsed(seconds):
    seconds = int(max(0, seconds or 0))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Video-Gen Pipeline Orchestrator")
        self.geometry("1060x880")
        self.minsize(920, 720)
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        self.config_mgr = Config()
        self.settings = self.config_mgr.data

        self.event_queue = queue.Queue()
        self.engine = PipelineEngine(
            self.settings,
            log_callback=self._on_engine_log,
            status_callback=self._on_engine_status,
            finished_callback=self._on_engine_finished,
        )

        self.status_labels = {}
        self.elapsed_labels = {}
        self.run_buttons = {}
        self.reset_buttons = {}
        self._slug_manually_edited = False
        self._tick = 0

        self._build_widgets()
        self._apply_params(self.settings.get("defaults", {}))
        self._refresh_project_list()
        self._update_controls()
        self.after(POLL_MS, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_quit)

        if self.config_mgr.load_error:
            self.after(200, lambda: messagebox.showwarning("settings.json", self.config_mgr.load_error))

    # ------------------------------------------------------------------
    # widget construction
    # ------------------------------------------------------------------
    def _build_widgets(self):
        root_pad = {"padx": 10, "pady": 6}

        # ---- Project panel (tabbed) ----
        proj_frame = ttk.LabelFrame(self, text="Project")
        proj_frame.pack(fill="x", **root_pad)

        self.notebook = ttk.Notebook(proj_frame)
        self.notebook.pack(fill="x", padx=8, pady=(8, 4))
        self._build_tab_project()
        self._build_tab_script()
        self._build_tab_video()

        self.current_project_var = tk.StringVar(value="No project loaded.")
        ttk.Label(proj_frame, textvariable=self.current_project_var, foreground="#555555").pack(
            anchor="w", padx=8, pady=(0, 6)
        )

        # ---- Steps panel ----
        steps_frame = ttk.LabelFrame(self, text="Pipeline Steps")
        steps_frame.pack(fill="x", **root_pad)

        for job in job_defs.JOBS:
            row = ttk.Frame(steps_frame)
            row.pack(fill="x", padx=8, pady=3)

            ttk.Label(row, text=job["label"], width=26).pack(side="left")

            status_lbl = ttk.Label(
                row, text=STATUS_LABELS[STATUS_PENDING], foreground=STATUS_COLORS[STATUS_PENDING], width=30
            )
            status_lbl.pack(side="left")
            self.status_labels[job["id"]] = status_lbl

            elapsed_lbl = ttk.Label(row, text="", width=9, foreground="#777777")
            elapsed_lbl.pack(side="left")
            self.elapsed_labels[job["id"]] = elapsed_lbl

            reset_btn = ttk.Button(row, text="Reset", width=8, command=lambda jid=job["id"]: self.on_reset_step(jid))
            reset_btn.pack(side="right", padx=(4, 0))
            self.reset_buttons[job["id"]] = reset_btn

            run_btn = ttk.Button(row, text="Run", width=8, command=lambda jid=job["id"]: self.on_run_single(jid))
            run_btn.pack(side="right")
            self.run_buttons[job["id"]] = run_btn

        # ---- Controls ----
        controls = ttk.Frame(self)
        controls.pack(fill="x", **root_pad)

        self.start_btn = ttk.Button(controls, text="START", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(controls, text="STOP", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=4)
        self.resume_btn = ttk.Button(controls, text="RESUME", command=self.on_resume)
        self.resume_btn.pack(side="left", padx=4)
        ttk.Separator(controls, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)
        self.save_btn = ttk.Button(controls, text="Save params to project", command=self.on_save_params)
        self.save_btn.pack(side="left", padx=4)
        ttk.Button(controls, text="Edit settings.json", command=self.on_edit_settings).pack(side="left", padx=4)
        self.open_folder_btn = ttk.Button(controls, text="Open project folder", command=self.on_open_folder)
        self.open_folder_btn.pack(side="left", padx=4)
        self.open_final_btn = ttk.Button(controls, text="Open final video", command=self.on_open_final)
        self.open_final_btn.pack(side="left", padx=4)
        ttk.Button(controls, text="QUIT", command=self.on_quit).pack(side="right", padx=4)

        progress_row = ttk.Frame(self)
        progress_row.pack(fill="x", padx=10)
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=len(job_defs.JOBS))
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_var = tk.StringVar(value=f"0/{len(job_defs.JOBS)} steps done")
        ttk.Label(progress_row, textvariable=self.progress_var, width=18, anchor="e").pack(side="left", padx=(8, 0))

        # ---- Log console ----
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **root_pad)

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill="x", padx=4, pady=(2, 0))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_toolbar, text="Auto-scroll", variable=self.autoscroll_var).pack(side="left")
        ttk.Button(log_toolbar, text="Clear", command=self.on_clear_log).pack(side="right")

        self.log_text = tk.Text(
            log_frame, wrap="word", state="disabled", bg="#111111", fg="#d8d8d8", insertbackground="#ffffff"
        )
        self.log_text.pack(fill="both", expand=True, side="left")
        self.log_text.tag_configure("orchestrator", foreground="#8ab4f8")
        self.log_text.tag_configure("error", foreground="#f28b82")
        self.log_text.tag_configure("warning", foreground="#fdd663")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_tab_project(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Project")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=8, pady=8)

        ttk.Label(grid, text="Title").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(grid, textvariable=self.title_var, width=48)
        title_entry.grid(row=0, column=1, sticky="we", padx=(4, 16))
        title_entry.bind("<KeyRelease>", self._on_title_typed)

        ttk.Label(grid, text="Duration (min)").grid(row=0, column=2, sticky="w")
        self.duration_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.duration_var, width=8).grid(row=0, column=3, sticky="w", padx=(4, 16))

        ttk.Label(grid, text="Beats (optional)").grid(row=0, column=4, sticky="w")
        self.beats_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.beats_var, width=8).grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(grid, text="Output folder name").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.slug_var = tk.StringVar()
        slug_entry = ttk.Entry(grid, textvariable=self.slug_var, width=48)
        slug_entry.grid(row=1, column=1, sticky="we", padx=(4, 16), pady=(6, 0))
        slug_entry.bind("<KeyRelease>", self._on_slug_typed)

        ttk.Label(grid, text="Aspect ratio").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.aspect_var = tk.StringVar()
        ttk.Combobox(
            grid, textvariable=self.aspect_var, values=job_defs.ASPECT_RATIOS, state="readonly", width=7
        ).grid(row=1, column=3, sticky="w", padx=(4, 16), pady=(6, 0))

        ttk.Label(grid, text="Art style").grid(row=1, column=4, sticky="w", pady=(6, 0))
        self.art_style_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.art_style_var, width=30).grid(row=1, column=5, sticky="we", padx=(4, 0), pady=(6, 0))

        grid.columnconfigure(1, weight=3)
        grid.columnconfigure(5, weight=2)

        actions = ttk.Frame(tab)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        self.create_btn = ttk.Button(actions, text="Create / Load Project", command=self.on_create_project)
        self.create_btn.pack(side="left")
        ttk.Label(actions, text="Existing projects:").pack(side="left", padx=(18, 0))
        self.existing_var = tk.StringVar()
        self.existing_combo = ttk.Combobox(actions, textvariable=self.existing_var, state="readonly", width=36)
        self.existing_combo.pack(side="left", padx=6)
        ttk.Button(actions, text="Load Selected", command=self.on_load_existing).pack(side="left", padx=4)
        ttk.Button(actions, text="Refresh", command=self._refresh_project_list).pack(side="left", padx=4)

    def _build_tab_script(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Script & Voice")

        ttk.Label(
            tab,
            text=("Custom instructions for the script writer - anything the tags can't express: "
                  "exact beat count, structure, narrator voice and persona, facts to include, things to avoid..."),
            foreground="#555555", wraplength=960, justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 2))
        self.instructions_text = ScrolledText(tab, height=6, wrap="word", undo=True)
        self.instructions_text.pack(fill="x", padx=8)

        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=8, pady=8)
        ttk.Label(grid, text="Tone").grid(row=0, column=0, sticky="w")
        self.tone_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.tone_var, width=30).grid(row=0, column=1, sticky="we", padx=(4, 16))

        ttk.Label(grid, text="Depth").grid(row=0, column=2, sticky="w")
        self.depth_var = tk.StringVar()
        ttk.Combobox(grid, textvariable=self.depth_var, values=job_defs.DEPTHS, state="readonly", width=10).grid(
            row=0, column=3, sticky="w", padx=(4, 16)
        )

        ttk.Label(grid, text="Voice (Fish reference ID)").grid(row=0, column=4, sticky="w")
        self.reference_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.reference_var, width=36).grid(row=0, column=5, sticky="we", padx=(4, 0))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(5, weight=1)

    def _build_tab_video(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Video & Animation")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=8, pady=8)

        def combo(row, col, label, var, values, width=13):
            ttk.Label(grid, text=label).grid(row=row, column=col, sticky="w", pady=(0, 6))
            ttk.Combobox(grid, textvariable=var, values=values, state="readonly", width=width).grid(
                row=row, column=col + 1, sticky="w", padx=(4, 16), pady=(0, 6)
            )

        self.animation_var = tk.StringVar()
        self.transition_var = tk.StringVar()
        self.smoothness_var = tk.StringVar()
        self.fit_var = tk.StringVar()
        combo(0, 0, "Animation", self.animation_var, job_defs.ANIMATIONS)
        combo(0, 2, "Transition", self.transition_var, job_defs.TRANSITIONS, width=9)
        combo(0, 4, "Smoothness", self.smoothness_var, job_defs.SMOOTHNESS)
        combo(1, 0, "Fit", self.fit_var, job_defs.FIT_MODES, width=9)

        ttk.Label(grid, text="FPS").grid(row=1, column=2, sticky="w", pady=(0, 6))
        self.fps_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.fps_var, width=6).grid(row=1, column=3, sticky="w", padx=(4, 16), pady=(0, 6))

        ttk.Label(grid, text="Zoom strength (0-1)").grid(row=1, column=4, sticky="w", pady=(0, 6))
        self.zoom_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.zoom_var, width=6).grid(row=1, column=5, sticky="w", padx=(4, 0), pady=(0, 6))

        ttk.Label(grid, text="Image folder").grid(row=2, column=0, sticky="w")
        self.image_source_var = tk.StringVar()
        self.image_source_entry = ttk.Entry(grid, textvariable=self.image_source_var, width=52)
        self.image_source_entry.grid(row=2, column=1, columnspan=3, sticky="we", padx=(4, 6))
        self.browse_btn = ttk.Button(grid, text="Browse...", command=self.on_browse_images)
        self.browse_btn.grid(row=2, column=4, sticky="w")
        self.image_subfolder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            grid, text="Per-project subfolder (<Downloads>/<folder name>)",
            variable=self.image_subfolder_var, command=self._on_subfolder_toggled,
        ).grid(row=2, column=5, sticky="w", padx=(4, 0))

        ttk.Label(
            grid,
            text=("'random' picks a different preset for every scene. Animation, transition and smoothness "
                  "can be changed on a loaded project - just click Run on step 6 again."),
            foreground="#555555", wraplength=960, justify="left",
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))
        grid.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # field helpers
    # ------------------------------------------------------------------
    def _on_title_typed(self, _event):
        # Auto-suggest a slug from the title, but only while the user
        # hasn't started customizing the slug field themselves.
        if not self._slug_manually_edited:
            self.slug_var.set(slugify(self.title_var.get()))
            self._on_subfolder_toggled()

    def _on_slug_typed(self, _event):
        self._slug_manually_edited = bool(self.slug_var.get().strip())
        if not self._slug_manually_edited:
            self.slug_var.set(slugify(self.title_var.get()))
        self._on_subfolder_toggled()

    def _downloads_dir(self):
        return Path(self.settings.get("image_gen", {}).get("downloads_dir") or (Path.home() / "Downloads")).expanduser()

    def _on_subfolder_toggled(self):
        if self.image_subfolder_var.get():
            slug = slugify(self.slug_var.get() or self.title_var.get())
            self.image_source_var.set(str(self._downloads_dir() / slug))
            self.image_source_entry.configure(state="disabled")
            self.browse_btn.configure(state="disabled")
        else:
            self.image_source_entry.configure(state="normal")
            self.browse_btn.configure(state="normal")

    def on_browse_images(self):
        initial = self.image_source_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(title="Select the folder with the downloaded images", initialdir=initial)
        if chosen:
            self.image_source_var.set(chosen)

    def _collect_params(self, require_title=False):
        """Read every GUI field into a params dict. Returns None (after a dialog) on invalid input."""
        def fail(message):
            messagebox.showwarning("Check your input", message)
            return None

        title = self.title_var.get().strip()
        if require_title and not title:
            return fail("Please enter a title for the video.")

        try:
            duration = float(self.duration_var.get().strip() or 0)
        except ValueError:
            return fail("Duration must be a number of minutes, e.g. 3 or 2.5.")
        if duration <= 0:
            return fail("Duration must be greater than 0.")

        beats_raw = self.beats_var.get().strip()
        beats = ""
        if beats_raw:
            try:
                beats = int(beats_raw)
            except ValueError:
                return fail("Beats must be a whole number, or empty to derive it from the duration.")
            if beats <= 0:
                return fail("Beats must be greater than 0.")

        try:
            fps = int(self.fps_var.get().strip() or 30)
        except ValueError:
            return fail("FPS must be a whole number, e.g. 30.")
        if not 1 <= fps <= 120:
            return fail("FPS must be between 1 and 120.")

        try:
            zoom = float(self.zoom_var.get().strip() or 0.18)
        except ValueError:
            return fail("Zoom strength must be a number between 0 and 1, e.g. 0.18.")
        if not 0 <= zoom <= 1:
            return fail("Zoom strength must be between 0 and 1.")

        duration_value = int(duration) if duration == int(duration) else duration
        return {
            "title": title,
            "duration": duration_value,
            "beats": beats,
            "aspect_ratio": self.aspect_var.get() or "16:9",
            "art_style": self.art_style_var.get().strip(),
            "custom_instructions": self.instructions_text.get("1.0", "end-1c").strip(),
            "tone": self.tone_var.get().strip(),
            "depth": self.depth_var.get(),
            "reference_id": self.reference_var.get().strip(),
            "animation": self.animation_var.get() or "none",
            "transition": self.transition_var.get() or "none",
            "smoothness": self.smoothness_var.get() or "ease_in_out",
            "fit": self.fit_var.get() or "cover",
            "fps": fps,
            "zoom": zoom,
            "image_source": self.image_source_var.get().strip(),
            "image_subfolder": bool(self.image_subfolder_var.get()),
        }

    def _apply_params(self, params):
        """Push a params dict (project or settings defaults) into the GUI fields."""
        params = params or {}

        def pick(key, choices, fallback):
            value = str(params.get(key, "") or "")
            return value if value in choices else fallback

        self.title_var.set(str(params.get("title", "") or ""))
        self.duration_var.set(str(params.get("duration", 3) or 3))
        self.beats_var.set(str(params.get("beats", "") or ""))
        self.aspect_var.set(pick("aspect_ratio", job_defs.ASPECT_RATIOS, "16:9"))
        self.art_style_var.set(str(params.get("art_style", "") or ""))

        self.instructions_text.delete("1.0", "end")
        self.instructions_text.insert("1.0", str(params.get("custom_instructions", "") or ""))
        self.tone_var.set(str(params.get("tone", "") or ""))
        self.depth_var.set(pick("depth", job_defs.DEPTHS, "balanced"))
        self.reference_var.set(str(params.get("reference_id", "") or ""))

        self.animation_var.set(pick("animation", job_defs.ANIMATIONS, "none"))
        self.transition_var.set(pick("transition", job_defs.TRANSITIONS, "none"))
        self.smoothness_var.set(pick("smoothness", job_defs.SMOOTHNESS, "ease_in_out"))
        self.fit_var.set(pick("fit", job_defs.FIT_MODES, "cover"))
        self.fps_var.set(str(params.get("fps", 30) or 30))
        self.zoom_var.set(str(params.get("zoom", 0.18) if params.get("zoom", None) not in (None, "") else 0.18))
        self.image_subfolder_var.set(bool(params.get("image_subfolder", False)))
        self.image_source_var.set(str(params.get("image_source", "") or ""))
        self._on_subfolder_toggled()

    # ------------------------------------------------------------------
    # project actions
    # ------------------------------------------------------------------
    def _refresh_project_list(self):
        projects = PipelineEngine.list_projects(self.config_mgr.pipeline_root)
        self.existing_combo["values"] = [p[0] for p in projects]

    def on_create_project(self):
        if self.engine.running:
            messagebox.showinfo("Busy", "Stop the running pipeline before switching projects.")
            return
        params = self._collect_params(require_title=True)
        if params is None:
            return
        slug = slugify(self.slug_var.get() or params["title"])
        self.slug_var.set(slug)

        project_dir, is_new = self.engine.new_project(slug, params)
        self._after_project_loaded(slug, project_dir)
        if not is_new:
            self._append_log(
                "[orchestrator] The GUI now shows the saved settings of that project. Edit them and run a step "
                "(or click 'Save params to project') to apply changes."
            )

    def on_load_existing(self):
        if self.engine.running:
            messagebox.showinfo("Busy", "Stop the running pipeline before switching projects.")
            return
        slug = self.existing_var.get()
        if not slug:
            messagebox.showinfo("Pick a project", "Select a project from the dropdown first.")
            return
        project_dir = self.config_mgr.pipeline_root / "out" / slug
        self.engine.load_project(project_dir)
        self._after_project_loaded(slug, project_dir)
        self._append_log(f"[orchestrator] Loaded project '{slug}'.")

    def _after_project_loaded(self, slug, project_dir):
        params = self.engine.state.get_params()
        self._apply_params(params)
        self.slug_var.set(slug)
        self._slug_manually_edited = True
        self.current_project_var.set(f"Project: {slug}  ({project_dir})")
        self._sync_all_status_labels()
        self._refresh_project_list()
        self._update_progress()
        self._update_controls()
        if self.engine.state.is_awaiting_manual_resume():
            reason = self.engine.state.data.get("pause_reason") or ""
            self._append_log(f"[orchestrator] This project is paused awaiting manual verification: {reason}")

    # ------------------------------------------------------------------
    # pipeline controls
    # ------------------------------------------------------------------
    def _require_project(self):
        if self.engine.state is None:
            messagebox.showinfo("No project", "Create or load a project first.")
            return False
        return True

    def _sync_params_to_project(self):
        """Write the current GUI values into the loaded project. False if a field is invalid."""
        params = self._collect_params()
        if params is None:
            return False
        self.engine.update_params(params)
        return True

    def on_save_params(self):
        if not self._require_project():
            return
        if self._sync_params_to_project():
            self._append_log("[orchestrator] Parameters saved to the project.")

    def on_start(self):
        if not self._require_project() or self.engine.running:
            return
        if self.engine.state.any_completed():
            if not messagebox.askyesno(
                "Start from the beginning?",
                "Some steps are already done. START re-runs every step from step 1.\n\n"
                "Use RESUME to continue from the first unfinished step instead.\n\nRe-run everything?",
            ):
                return
        if not self._sync_params_to_project():
            return
        self.engine.start_full_pipeline(from_index=0)
        self._update_controls()

    def on_resume(self):
        if not self._require_project() or self.engine.running:
            return
        if not self._sync_params_to_project():
            return
        self.engine.resume()
        self._update_controls()

    def on_stop(self):
        self.engine.stop()

    def on_run_single(self, job_id):
        if not self._require_project() or self.engine.running:
            return
        if not self._sync_params_to_project():
            return
        self.engine.run_single(job_id)
        self._update_controls()

    def on_reset_step(self, job_id):
        if not self._require_project() or self.engine.running:
            return
        self.engine.reset_step(job_id)
        self._update_progress()

    def on_open_folder(self):
        if not self._require_project():
            return
        if not open_path(self.engine.state.project_dir):
            messagebox.showinfo("Project folder", str(self.engine.state.project_dir))

    def on_open_final(self):
        if not self._require_project():
            return
        final = self.engine.final_video_path()
        if final is None or not final.exists():
            messagebox.showinfo("Not yet", "final.mp4 doesn't exist yet - run the pipeline through step 7 first.")
            return
        if not open_path(final):
            messagebox.showinfo("Final video", str(final))

    def on_quit(self):
        if self.engine.running:
            if not messagebox.askyesno("Quit", "A worker is currently running. Stop it and quit?"):
                return
            self.engine.stop()
        self.destroy()

    def on_edit_settings(self):
        SettingsEditor(self, self.config_mgr, on_saved=self._on_settings_saved)

    def _on_settings_saved(self, new_data):
        self.settings = new_data
        self.engine.settings = new_data
        self._append_log("[orchestrator] settings.json saved and reloaded.")

    def on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # engine callbacks -> thread-safe queue -> UI
    # ------------------------------------------------------------------
    def _on_engine_log(self, line):
        self.event_queue.put(("log", line))

    def _on_engine_status(self, job_id, status, detail=""):
        self.event_queue.put(("status", job_id, status, detail))

    def _on_engine_finished(self):
        self.event_queue.put(("finished",))

    def _poll_queue(self):
        try:
            while True:
                item = self.event_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "status":
                    self._set_status_label(item[1], item[2], item[3])
                    self._update_progress()
                elif kind == "finished":
                    self._update_controls()
                    self._update_progress()
        except queue.Empty:
            pass

        self._tick += 1
        if self._tick % 5 == 0:  # every ~500 ms
            self._update_elapsed()
            self._update_controls()
        self.after(POLL_MS, self._poll_queue)

    def _append_log(self, line):
        tag = None
        if line.startswith("[orchestrator]"):
            tag = "orchestrator"
            if "ERROR" in line or "FAILED" in line:
                tag = "error"
            elif "WARNING" in line or "PAUSED" in line:
                tag = "warning"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", tag)
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        except (ValueError, tk.TclError):
            pass
        if self.autoscroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status_label(self, job_id, status, detail=""):
        lbl = self.status_labels.get(job_id)
        if lbl:
            text = STATUS_LABELS.get(status, status)
            if detail:
                text = f"{text}  ({detail})"
            lbl.configure(text=text, foreground=STATUS_COLORS.get(status, "#000000"))

    def _sync_all_status_labels(self):
        if self.engine.state is None:
            return
        for jid in job_defs.JOB_IDS:
            step = self.engine.state.get_step(jid)
            status = step.get("status", STATUS_PENDING)
            detail = ""
            if status == STATUS_FAILED and step.get("returncode") not in (None, 0):
                detail = f"exit {step['returncode']}"
            self._set_status_label(jid, status, detail)
        self._update_elapsed()

    def _update_elapsed(self):
        state = self.engine.state
        now = time.time()
        for jid, lbl in self.elapsed_labels.items():
            elapsed = state.step_elapsed(jid, now) if state else None
            lbl.configure(text=format_elapsed(elapsed) if elapsed is not None else "")

    def _update_progress(self):
        total = len(job_defs.JOBS)
        done = self.engine.state.completed_count() if self.engine.state else 0
        self.progress["value"] = done
        self.progress_var.set(f"{done}/{total} steps done")

    def _update_controls(self):
        running = self.engine.running
        has_project = self.engine.state is not None
        idle_state = "normal" if (has_project and not running) else "disabled"
        self.start_btn.configure(state=idle_state)
        self.resume_btn.configure(state=idle_state)
        self.save_btn.configure(state=idle_state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.create_btn.configure(state="disabled" if running else "normal")
        self.open_folder_btn.configure(state="normal" if has_project else "disabled")
        final = self.engine.final_video_path() if has_project else None
        self.open_final_btn.configure(state="normal" if (final and final.exists()) else "disabled")
        for btn in list(self.run_buttons.values()) + list(self.reset_buttons.values()):
            btn.configure(state=idle_state)


class SettingsEditor(tk.Toplevel):
    """Raw-JSON editor for settings.json with validation before save."""

    def __init__(self, parent, config_mgr, on_saved=None):
        super().__init__(parent)
        self.title("Edit settings.json")
        self.geometry("720x640")
        self.config_mgr = config_mgr
        self.on_saved = on_saved

        ttk.Label(
            self, text=str(config_mgr.path), foreground="#555555"
        ).pack(anchor="w", padx=8, pady=(8, 0))
        self.text = ScrolledText(self, wrap="none", undo=True)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        self.text.insert("1.0", json.dumps(config_mgr.data, indent=2))

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="Save", command=self.on_save).pack(side="right")
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def on_save(self):
        raw = self.text.get("1.0", "end")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            messagebox.showerror("Invalid JSON", f"Couldn't parse settings.json:\n{e}")
            return
        if not isinstance(data, dict):
            messagebox.showerror("Invalid settings", "settings.json must contain a JSON object.")
            return
        try:
            self.config_mgr.save(data)
        except OSError as e:
            messagebox.showerror("Could not save", str(e))
            return
        if self.on_saved:
            self.on_saved(self.config_mgr.data)
        self.destroy()


def main():
    app = App()
    app.mainloop()
