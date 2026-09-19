"""
Tkinter GUI for the pipeline orchestrator.

Layout:
    - Top Header: Active project indicator, quick project loader/creator, utility shortcuts (settings, folder, video, quit).
    - Responsive Two-Column Split (ttk.PanedWindow):
        - Left Column (Scrollable):
            - Project Configuration Panel (tabbed: Project, Script & Voice, Video & Animation, Subtitles)
            - Master Controls & Progress: START, STOP, RESUME, Save params, live progress bar
            - Pipeline Steps Panel: 9 workers with real-time status badges, elapsed timers, Run/Reset actions, active step highlight
        - Right Column (Full Height):
            - Live Execution Log Console with filtering (All, Orchestrator, Errors, Warnings), in-log search/find, copy to clipboard, and auto-scroll.

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

try:
    from editor.ffmpeg_gpu import get_hardware_status_summary, str2bool
except ImportError:
    try:
        from ffmpeg_gpu import get_hardware_status_summary, str2bool
    except ImportError:
        def get_hardware_status_summary():
            return "CPU (libx264)"
        def str2bool(val):
            return str(val).strip().lower() in ("yes", "true", "t", "y", "1")

STATUS_COLORS = {
    STATUS_PENDING: "#64748b",
    "pending": "#64748b",
    STATUS_RUNNING: "#2563eb",
    "running": "#2563eb",
    "running...": "#2563eb",
    STATUS_SUCCESS: "#16a34a",
    "success": "#16a34a",
    "done": "#16a34a",
    STATUS_FAILED: "#dc2626",
    "failed": "#dc2626",
    STATUS_STOPPED: "#ea580c",
    "stopped": "#ea580c",
}

STATUS_LABELS = {
    STATUS_PENDING: "pending",
    "pending": "pending",
    STATUS_RUNNING: "running...",
    "running": "running...",
    "running...": "running...",
    STATUS_SUCCESS: "done",
    "success": "done",
    "done": "done",
    STATUS_FAILED: "failed",
    "failed": "failed",
    STATUS_STOPPED: "stopped",
    "stopped": "stopped",
}

STATUS_ICONS = {
    STATUS_PENDING: "○",
    "pending": "○",
    STATUS_RUNNING: "●",
    "running": "●",
    "running...": "●",
    STATUS_SUCCESS: "✓",
    "success": "✓",
    "done": "✓",
    STATUS_FAILED: "✗",
    "failed": "✗",
    STATUS_STOPPED: "■",
    "stopped": "■",
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


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable frame that automatically matches parent width."""

    def __init__(self, container, width=550, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, width=width)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_window = ttk.Frame(self.canvas)

        self.scrollable_window.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_window, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Scroll on mousewheel only when mouse hovers over left pane
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.bind_all("<Button-5>", self._on_mousewheel, add="+")

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
            if widget and (widget == self.canvas or str(widget).startswith(str(self.scrollable_window))):
                if event.num == 4:
                    self.canvas.yview_scroll(-2, "units")
                elif event.num == 5:
                    self.canvas.yview_scroll(2, "units")
                elif event.delta:
                    self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except (tk.TclError, ValueError):
            pass


class App(tk.Tk):
    def __init__(self, initial_gpu=None):
        super().__init__()
        self.title("YouTube Video-Gen Pipeline Orchestrator")
        self.geometry("1240x820")
        self.minsize(1020, 660)

        self._setup_styles()

        self.config_mgr = Config()
        self.settings = self.config_mgr.data
        if initial_gpu is not None:
            self.settings.setdefault("defaults", {})["use_gpu"] = bool(initial_gpu)

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
        self.step_row_frames = {}
        self._slug_manually_edited = False
        self._tick = 0

        # Log system state
        self._raw_logs = []  # list of dicts: {"line": str, "tag": str, "kind": str}
        self.active_log_filter = "All"
        self._search_match_indices = []
        self._current_match_idx = -1

        self._build_widgets()
        self._apply_params(self.settings.get("defaults", {}))
        self._refresh_project_list()
        self._update_controls()
        self.after(POLL_MS, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_quit)

        if self.config_mgr.load_error:
            self.after(200, lambda: messagebox.showwarning("settings.json", self.config_mgr.load_error))

    def _setup_styles(self):
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        style = ttk.Style(self)

        # Header Styles
        style.configure("Header.TFrame", background="#1e293b")
        style.configure("HeaderTitle.TLabel", background="#1e293b", foreground="#ffffff", font=("Helvetica", 12, "bold"))
        style.configure("HeaderBadge.TLabel", background="#0f172a", foreground="#38bdf8", font=("Helvetica", 9, "bold"), padding=(8, 4))
        style.configure("HeaderLabel.TLabel", background="#1e293b", foreground="#cbd5e1", font=("Helvetica", 9))

        # Master Controls
        style.configure("Start.TButton", background="#16a34a", foreground="#ffffff", font=("Helvetica", 10, "bold"), padding=(10, 5))
        style.map("Start.TButton", background=[("active", "#15803d"), ("disabled", "#9ca3af")], foreground=[("disabled", "#e5e7eb")])

        style.configure("Resume.TButton", background="#2563eb", foreground="#ffffff", font=("Helvetica", 10, "bold"), padding=(10, 5))
        style.map("Resume.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9ca3af")], foreground=[("disabled", "#e5e7eb")])

        style.configure("Stop.TButton", background="#dc2626", foreground="#ffffff", font=("Helvetica", 10, "bold"), padding=(10, 5))
        style.map("Stop.TButton", background=[("active", "#b91c1c"), ("disabled", "#9ca3af")], foreground=[("disabled", "#e5e7eb")])

        style.configure("Save.TButton", background="#475569", foreground="#ffffff", font=("Helvetica", 9, "bold"), padding=(8, 5))
        style.map("Save.TButton", background=[("active", "#334155"), ("disabled", "#9ca3af")], foreground=[("disabled", "#e5e7eb")])

        # Step Actions
        style.configure("StepRun.TButton", font=("Helvetica", 8, "bold"), padding=(4, 2))
        style.configure("StepReset.TButton", font=("Helvetica", 8), padding=(4, 2))

        # Notebook tabs & cards
        style.configure("TNotebook", tabposition="nw")
        style.configure("TNotebook.Tab", font=("Helvetica", 9, "bold"), padding=(10, 4))
        style.configure("TLabelframe", padding=6)
        style.configure("TLabelframe.Label", font=("Helvetica", 9, "bold"), foreground="#1e293b")

    # ------------------------------------------------------------------
    # widget construction
    # ------------------------------------------------------------------
    def _build_widgets(self):
        # 1. Top Header Bar
        self._build_header_bar()

        # 2. Main Two-Column Split (PanedWindow)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Left Column: Fixed initial width of ~550px, weight=0 so right gets expansion
        left_container = ttk.Frame(paned, width=550)
        paned.add(left_container, weight=0)

        self.scroll_pane = ScrollableFrame(left_container, width=540)
        self.scroll_pane.pack(fill="both", expand=True)
        left_content = self.scroll_pane.scrollable_window

        # A. Project Configuration Panel (tabbed)
        self._build_project_notebook(left_content)

        # B. Controls & Progress Panel
        self._build_master_controls(left_content)

        # C. Steps Panel (9 workers)
        self._build_steps_panel(left_content)

        # Right Column: Execution Log Console (weight=1 takes all remaining width)
        right_container = ttk.Frame(paned)
        paned.add(right_container, weight=1)
        self._build_log_console(right_container)

    def _build_header_bar(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(10, 6))
        header.pack(fill="x")

        # Left side: Branding + Current Project Indicator
        left_box = ttk.Frame(header, style="Header.TFrame")
        left_box.pack(side="left")

        title_lbl = ttk.Label(left_box, text="🎬 YouTube Pipeline", style="HeaderTitle.TLabel")
        title_lbl.pack(side="left", padx=(0, 10))

        self.current_project_var = tk.StringVar(value="No project loaded")
        self.project_badge = ttk.Label(left_box, textvariable=self.current_project_var, style="HeaderBadge.TLabel")
        self.project_badge.pack(side="left")

        # Right side: Project Switcher & Utilities
        right_box = ttk.Frame(header, style="Header.TFrame")
        right_box.pack(side="right")

        ttk.Label(right_box, text="Project:", style="HeaderLabel.TLabel").pack(side="left", padx=(0, 4))
        self.existing_var = tk.StringVar()
        self.existing_combo = ttk.Combobox(right_box, textvariable=self.existing_var, state="readonly", width=14)
        self.existing_combo.pack(side="left", padx=(0, 4))

        self.load_top_btn = ttk.Button(right_box, text="Load", width=5, command=self.on_load_existing)
        self.load_top_btn.pack(side="left", padx=2)

        self.refresh_top_btn = ttk.Button(right_box, text="↻", width=3, command=self._refresh_project_list)
        self.refresh_top_btn.pack(side="left", padx=2)

        ttk.Separator(right_box, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)

        self.open_folder_btn = ttk.Button(right_box, text="📁 Folder", command=self.on_open_folder)
        self.open_folder_btn.pack(side="left", padx=2)

        self.open_final_btn = ttk.Button(right_box, text="🎬 Video", command=self.on_open_final)
        self.open_final_btn.pack(side="left", padx=2)

        ttk.Button(right_box, text="⚙ Settings", command=self.on_edit_settings).pack(side="left", padx=2)
        ttk.Button(right_box, text="✕ Quit", width=6, command=self.on_quit).pack(side="left", padx=(4, 4))

    def _build_project_notebook(self, parent):
        proj_frame = ttk.LabelFrame(parent, text="Project Configuration")
        proj_frame.pack(fill="x", padx=6, pady=(4, 6))

        self.notebook = ttk.Notebook(proj_frame)
        self.notebook.pack(fill="x", padx=4, pady=4)

        self._build_tab_project()
        self._build_tab_script()
        self._build_tab_video()
        self._build_tab_subtitles()

    def _build_tab_project(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Project")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=6, pady=6)

        # Row 0: Title
        ttk.Label(grid, text="Title").grid(row=0, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(grid, textvariable=self.title_var, width=32)
        title_entry.grid(row=0, column=1, columnspan=3, sticky="we", padx=(4, 10), pady=3)
        title_entry.bind("<KeyRelease>", self._on_title_typed)

        # Row 1: Duration & Beats
        ttk.Label(grid, text="Duration (min)").grid(row=1, column=0, sticky="w", pady=3)
        self.duration_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.duration_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 10), pady=3)

        ttk.Label(grid, text="Beats (optional)").grid(row=1, column=2, sticky="w", pady=3)
        self.beats_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.beats_var, width=8).grid(row=1, column=3, sticky="w", padx=(4, 0), pady=3)

        # Row 2: Folder slug
        ttk.Label(grid, text="Folder slug").grid(row=2, column=0, sticky="w", pady=3)
        self.slug_var = tk.StringVar()
        slug_entry = ttk.Entry(grid, textvariable=self.slug_var, width=32)
        slug_entry.grid(row=2, column=1, columnspan=3, sticky="we", padx=(4, 10), pady=3)
        slug_entry.bind("<KeyRelease>", self._on_slug_typed)

        # Row 3: Aspect Ratio & Art Style
        ttk.Label(grid, text="Aspect ratio").grid(row=3, column=0, sticky="w", pady=3)
        self.aspect_var = tk.StringVar()
        ttk.Combobox(
            grid, textvariable=self.aspect_var, values=job_defs.ASPECT_RATIOS, state="readonly", width=8
        ).grid(row=3, column=1, sticky="w", padx=(4, 10), pady=3)

        ttk.Label(grid, text="Art style").grid(row=3, column=2, sticky="w", pady=3)
        self.art_style_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.art_style_var, width=20).grid(row=3, column=3, sticky="we", padx=(4, 0), pady=3)

        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=2)

        # Actions in Tab 1
        actions = ttk.Frame(tab)
        actions.pack(fill="x", padx=6, pady=(4, 6))
        self.create_btn = ttk.Button(actions, text="Create / Load Project", command=self.on_create_project)
        self.create_btn.pack(side="left")

        ttk.Label(actions, text="Save new or load existing project", foreground="#64748b").pack(side="left", padx=(10, 0))

    def _build_tab_script(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Script & Voice")

        ttk.Label(
            tab,
            text="Custom instructions for writer (narrator tone, pacing, facts, style constraints):",
            foreground="#475569",
        ).pack(anchor="w", padx=6, pady=(4, 2))

        self.instructions_text = ScrolledText(tab, height=4, wrap="word", undo=True)
        self.instructions_text.pack(fill="x", padx=6, pady=(0, 4))

        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=6, pady=4)

        ttk.Label(grid, text="Tone").grid(row=0, column=0, sticky="w", pady=2)
        self.tone_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.tone_var, width=18).grid(row=0, column=1, sticky="we", padx=(4, 10), pady=2)

        ttk.Label(grid, text="Depth").grid(row=0, column=2, sticky="w", pady=2)
        self.depth_var = tk.StringVar()
        ttk.Combobox(grid, textvariable=self.depth_var, values=job_defs.DEPTHS, state="readonly", width=10).grid(
            row=0, column=3, sticky="w", padx=(4, 0), pady=2
        )

        ttk.Label(grid, text="Voice ID").grid(row=1, column=0, sticky="w", pady=4)
        self.reference_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.reference_var, width=18).grid(row=1, column=1, sticky="we", padx=(4, 10), pady=4)

        ttk.Label(grid, text="Polish").grid(row=1, column=2, sticky="w", pady=4)
        self.audio_preset_var = tk.StringVar()
        ttk.Combobox(
            grid, textvariable=self.audio_preset_var, values=job_defs.AUDIO_PRESETS, state="readonly", width=10
        ).grid(row=1, column=3, sticky="w", padx=(4, 0), pady=4)

        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

    def _build_tab_video(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Video & Animation")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=6, pady=6)

        def combo(row, col, label, var, values, width=12):
            ttk.Label(grid, text=label).grid(row=row, column=col, sticky="w", pady=2)
            ttk.Combobox(grid, textvariable=var, values=values, state="readonly", width=width).grid(
                row=row, column=col + 1, sticky="w", padx=(4, 10), pady=2
            )

        self.animation_var = tk.StringVar()
        self.transition_var = tk.StringVar()
        self.smoothness_var = tk.StringVar()
        self.fit_var = tk.StringVar()

        combo(0, 0, "Animation", self.animation_var, job_defs.ANIMATIONS)
        combo(0, 2, "Transition", self.transition_var, job_defs.TRANSITIONS, width=10)

        combo(1, 0, "Smoothness", self.smoothness_var, job_defs.SMOOTHNESS)
        combo(1, 2, "Fit mode", self.fit_var, job_defs.FIT_MODES, width=10)

        ttk.Label(grid, text="FPS").grid(row=2, column=0, sticky="w", pady=2)
        self.fps_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.fps_var, width=8).grid(row=2, column=1, sticky="w", padx=(4, 10), pady=2)

        ttk.Label(grid, text="Zoom (0-1)").grid(row=2, column=2, sticky="w", pady=2)
        self.zoom_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.zoom_var, width=8).grid(row=2, column=3, sticky="w", padx=(4, 0), pady=2)

        ttk.Label(grid, text="Images").grid(row=3, column=0, sticky="w", pady=4)
        self.image_source_var = tk.StringVar()
        self.image_source_entry = ttk.Entry(grid, textvariable=self.image_source_var, width=28)
        self.image_source_entry.grid(row=3, column=1, columnspan=2, sticky="we", padx=(4, 6), pady=4)
        self.browse_btn = ttk.Button(grid, text="Browse...", command=self.on_browse_images)
        self.browse_btn.grid(row=3, column=3, sticky="w", pady=4)

        self.image_subfolder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            grid, text="Per-project subfolder (<Downloads>/<slug>)",
            variable=self.image_subfolder_var, command=self._on_subfolder_toggled,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 4))

        # Hardware / GPU Acceleration
        self.use_gpu_var = tk.BooleanVar(value=bool(self.settings.get("defaults", {}).get("use_gpu", False)))
        hw_summary = get_hardware_status_summary()
        gpu_row = ttk.Frame(grid)
        gpu_row.grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 2))
        ttk.Checkbutton(
            gpu_row, text="Use GPU Acceleration (--gpu true)", variable=self.use_gpu_var
        ).pack(side="left")
        ttk.Label(
            gpu_row, text=f"  [{hw_summary}]", foreground="#2563eb", font=("Helvetica", 8)
        ).pack(side="left")

        grid.columnconfigure(1, weight=1)

    def _build_tab_subtitles(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Subtitles")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=6, pady=6)

        def combo(row, col, label, var, values, width=12):
            ttk.Label(grid, text=label).grid(row=row, column=col, sticky="w", pady=2)
            ttk.Combobox(grid, textvariable=var, values=values, state="readonly", width=width).grid(
                row=row, column=col + 1, sticky="w", padx=(4, 10), pady=2
            )

        self.subtitle_style_var = tk.StringVar()
        self.subtitle_animation_var = tk.StringVar()
        self.subtitle_position_var = tk.StringVar()

        combo(0, 0, "Style", self.subtitle_style_var, job_defs.SUBTITLE_STYLES)
        combo(0, 2, "Animation", self.subtitle_animation_var, job_defs.SUBTITLE_ANIMATIONS)

        combo(1, 0, "Position", self.subtitle_position_var, job_defs.SUBTITLE_POSITIONS)

        ttk.Label(grid, text="Words/chunk").grid(row=1, column=2, sticky="w", pady=2)
        self.subtitle_max_words_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.subtitle_max_words_var, width=8).grid(
            row=1, column=3, sticky="w", padx=(4, 0), pady=2
        )

        ttk.Label(grid, text="Font").grid(row=2, column=0, sticky="w", pady=2)
        self.subtitle_font_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.subtitle_font_var, width=14).grid(
            row=2, column=1, sticky="we", padx=(4, 10), pady=2
        )

        ttk.Label(grid, text="Font size").grid(row=2, column=2, sticky="w", pady=2)
        self.subtitle_font_size_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.subtitle_font_size_var, width=8).grid(
            row=2, column=3, sticky="w", padx=(4, 0), pady=2
        )

        self.subtitle_uppercase_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            grid, text="ALL CAPS (uppercase)", variable=self.subtitle_uppercase_var
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 2))

        ttk.Checkbutton(
            grid, text="GPU Acceleration", variable=self.use_gpu_var
        ).grid(row=3, column=2, columnspan=2, sticky="w", pady=(4, 2))

        grid.columnconfigure(1, weight=1)

    def _build_master_controls(self, parent):
        ctrl_frame = ttk.LabelFrame(parent, text="Pipeline Controls")
        ctrl_frame.pack(fill="x", padx=6, pady=(0, 6))

        btn_row = ttk.Frame(ctrl_frame)
        btn_row.pack(fill="x", padx=6, pady=(4, 4))

        self.start_btn = ttk.Button(btn_row, text="▶ START", style="Start.TButton", command=self.on_start)
        self.start_btn.pack(side="left", padx=(0, 4))

        self.resume_btn = ttk.Button(btn_row, text="▶▶ RESUME", style="Resume.TButton", command=self.on_resume)
        self.resume_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(btn_row, text="⏹ STOP", style="Stop.TButton", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=4)

        self.save_btn = ttk.Button(btn_row, text="💾 Save", style="Save.TButton", command=self.on_save_params)
        self.save_btn.pack(side="right")

        # Progress bar with clean status
        progress_row = ttk.Frame(ctrl_frame)
        progress_row.pack(fill="x", padx=6, pady=(2, 4))

        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=len(job_defs.JOBS))
        self.progress.pack(side="left", fill="x", expand=True)

        self.progress_var = tk.StringVar(value=f"0/{len(job_defs.JOBS)} steps done")
        self.progress_lbl = ttk.Label(progress_row, textvariable=self.progress_var, width=22, anchor="e", font=("Helvetica", 9, "bold"))
        self.progress_lbl.pack(side="left", padx=(8, 0))

    def _build_steps_panel(self, parent):
        steps_frame = ttk.LabelFrame(parent, text="Pipeline Workers (Steps 1–9)")
        steps_frame.pack(fill="x", padx=6, pady=(0, 8))

        for job in job_defs.JOBS:
            jid = job["id"]
            row = ttk.Frame(steps_frame)
            row.pack(fill="x", padx=6, pady=2)
            self.step_row_frames[jid] = row

            # Label
            lbl_text = job["label"]
            ttk.Label(row, text=lbl_text, width=20, font=("Helvetica", 9, "bold")).pack(side="left")

            # Status with icon
            status_text = f"{STATUS_ICONS[STATUS_PENDING]} {STATUS_LABELS[STATUS_PENDING]}"
            status_lbl = ttk.Label(
                row, text=status_text, foreground=STATUS_COLORS[STATUS_PENDING], width=14, font=("Helvetica", 9)
            )
            status_lbl.pack(side="left", padx=(2, 4))
            self.status_labels[jid] = status_lbl

            # Elapsed time
            elapsed_lbl = ttk.Label(row, text="", width=7, foreground="#64748b", font=("Monospace", 9))
            elapsed_lbl.pack(side="left", padx=(2, 4))
            self.elapsed_labels[jid] = elapsed_lbl

            # Action Buttons
            reset_btn = ttk.Button(
                row, text="↺ Reset", width=7, style="StepReset.TButton", command=lambda j=jid: self.on_reset_step(j)
            )
            reset_btn.pack(side="right", padx=(2, 0))
            self.reset_buttons[jid] = reset_btn

            run_btn = ttk.Button(
                row, text="▶ Run", width=6, style="StepRun.TButton", command=lambda j=jid: self.on_run_single(j)
            )
            run_btn.pack(side="right", padx=2)
            self.run_buttons[jid] = run_btn

            # Subtle hint for step 4 (Image Generator)
            if jid == "image":
                ttk.Label(row, text="[auto-pause]", foreground="#94a3b8", font=("Helvetica", 8)).pack(side="right", padx=4)

    def _build_log_console(self, parent):
        log_frame = ttk.LabelFrame(parent, text="Live Execution Log")
        log_frame.pack(fill="both", expand=True, padx=4, pady=(4, 6))

        # Log Top Toolbar (2 clean rows)
        toolbar = ttk.Frame(log_frame)
        toolbar.pack(fill="x", padx=4, pady=(2, 4))

        # Row 1: Status indicator + Log tools (Auto-scroll, Copy, Clear)
        row1 = ttk.Frame(toolbar)
        row1.pack(fill="x", pady=(0, 3))

        self.log_status_var = tk.StringVar(value="Idle  |  Lines: 0")
        ttk.Label(row1, textvariable=self.log_status_var, font=("Helvetica", 9, "bold"), foreground="#475569").pack(side="left", padx=(2, 8))

        ttk.Button(row1, text="Clear", width=5, command=self.on_clear_log).pack(side="right", padx=2)
        ttk.Button(row1, text="📋 Copy", width=7, command=self.on_copy_log).pack(side="right", padx=2)
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Auto-scroll", variable=self.autoscroll_var).pack(side="right", padx=4)

        self.copy_toast_var = tk.StringVar(value="")
        self.copy_toast_lbl = ttk.Label(row1, textvariable=self.copy_toast_var, foreground="#16a34a", font=("Helvetica", 9, "bold"))
        self.copy_toast_lbl.pack(side="right", padx=4)

        # Row 2: Filter & Search tools
        row2 = ttk.Frame(toolbar)
        row2.pack(fill="x", pady=(0, 2))

        ttk.Label(row2, text="Filter:").pack(side="left", padx=(2, 2))
        self.filter_var = tk.StringVar(value="All")
        filter_combo = ttk.Combobox(
            row2, textvariable=self.filter_var, values=["All", "Orchestrator", "Errors", "Warnings"], state="readonly", width=12
        )
        filter_combo.pack(side="left", padx=(0, 10))
        filter_combo.bind("<<ComboboxSelected>>", self._on_filter_changed)

        ttk.Label(row2, text="Find:").pack(side="left", padx=(0, 2))
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(row2, textvariable=self.search_var, width=14)
        search_entry.pack(side="left", padx=(0, 2))
        search_entry.bind("<Return>", lambda e: self.on_find_next())

        ttk.Button(row2, text="Find", width=5, command=self.on_find_next).pack(side="left", padx=1)
        ttk.Button(row2, text="✕", width=2, command=self.on_clear_search).pack(side="left", padx=1)

        # Console Text Area
        console_frame = ttk.Frame(log_frame)
        console_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.log_text = tk.Text(
            console_frame,
            wrap="word",
            state="disabled",
            bg="#18181b",
            fg="#f4f4f5",
            insertbackground="#ffffff",
            font=("Monospace", 9),
            borderwidth=0,
            highlightthickness=0,
        )
        self.log_text.pack(fill="both", expand=True, side="left")

        # Configure syntax tags
        self.log_text.tag_configure("orchestrator", foreground="#60a5fa", font=("Monospace", 9, "bold"))
        self.log_text.tag_configure("error", foreground="#f87171", font=("Monospace", 9, "bold"))
        self.log_text.tag_configure("warning", foreground="#fbbf24", font=("Monospace", 9, "bold"))
        self.log_text.tag_configure("success", foreground="#34d399", font=("Monospace", 9, "bold"))
        self.log_text.tag_configure("search_match", background="#eab308", foreground="#000000")

        scrollbar = ttk.Scrollbar(console_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    # ------------------------------------------------------------------
    # field helpers
    # ------------------------------------------------------------------
    def _on_title_typed(self, _event):
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

        try:
            sub_words = int(self.subtitle_max_words_var.get().strip() or 3)
        except ValueError:
            sub_words = 3

        try:
            sub_font_size = int(self.subtitle_font_size_var.get().strip() or 0)
        except ValueError:
            sub_font_size = 0

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
            "audio_preset": self.audio_preset_var.get() or "youtube",
            "animation": self.animation_var.get() or "none",
            "transition": self.transition_var.get() or "none",
            "smoothness": self.smoothness_var.get() or "ease_in_out",
            "fit": self.fit_var.get() or "cover",
            "fps": fps,
            "zoom": zoom,
            "image_source": self.image_source_var.get().strip(),
            "image_subfolder": bool(self.image_subfolder_var.get()),
            "subtitle_style": self.subtitle_style_var.get() or "hormozi",
            "subtitle_animation": self.subtitle_animation_var.get() or "pop",
            "subtitle_position": self.subtitle_position_var.get() or "bottom",
            "subtitle_max_words": sub_words,
            "subtitle_uppercase": bool(self.subtitle_uppercase_var.get()),
            "subtitle_font": self.subtitle_font_var.get().strip(),
            "subtitle_font_size": sub_font_size,
            "use_gpu": bool(self.use_gpu_var.get()),
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
        self.audio_preset_var.set(pick("audio_preset", job_defs.AUDIO_PRESETS, "youtube"))

        self.animation_var.set(pick("animation", job_defs.ANIMATIONS, "none"))
        self.transition_var.set(pick("transition", job_defs.TRANSITIONS, "none"))
        self.smoothness_var.set(pick("smoothness", job_defs.SMOOTHNESS, "ease_in_out"))
        self.fit_var.set(pick("fit", job_defs.FIT_MODES, "cover"))
        self.fps_var.set(str(params.get("fps", 30) or 30))
        self.zoom_var.set(str(params.get("zoom", 0.18) if params.get("zoom", None) not in (None, "") else 0.18))
        self.image_subfolder_var.set(bool(params.get("image_subfolder", False)))
        self.image_source_var.set(str(params.get("image_source", "") or ""))

        self.subtitle_style_var.set(pick("subtitle_style", job_defs.SUBTITLE_STYLES, "hormozi"))
        self.subtitle_animation_var.set(pick("subtitle_animation", job_defs.SUBTITLE_ANIMATIONS, "pop"))
        self.subtitle_position_var.set(pick("subtitle_position", job_defs.SUBTITLE_POSITIONS, "bottom"))
        self.subtitle_max_words_var.set(str(params.get("subtitle_max_words", 3) if params.get("subtitle_max_words") is not None else 3))
        self.subtitle_uppercase_var.set(bool(params.get("subtitle_uppercase", False)))
        self.subtitle_font_var.set(str(params.get("subtitle_font", "") or ""))
        self.subtitle_font_size_var.set(str(params.get("subtitle_font_size", 0) or 0))
        self.use_gpu_var.set(bool(params.get("use_gpu", False)))

        self._on_subfolder_toggled()

    # ------------------------------------------------------------------
    # project actions
    # ------------------------------------------------------------------
    def _refresh_project_list(self):
        projects = PipelineEngine.list_projects(self.config_mgr.pipeline_root)
        names = [p[0] for p in projects]
        self.existing_combo["values"] = names
        if names and not self.existing_var.get():
            self.existing_var.set(names[0])

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
                "(or click 'Save Params') to apply changes."
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
        self.current_project_var.set(f"Project: {slug}")
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
            self._show_toast("Parameters saved!")

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
            messagebox.showinfo("Not yet", f"final.mp4 doesn't exist yet - run the pipeline through step {len(job_defs.JOBS)} first.")
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

    # ------------------------------------------------------------------
    # log console features
    # ------------------------------------------------------------------
    def on_clear_log(self):
        self._raw_logs.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._update_log_status()

    def on_copy_log(self):
        text = self.log_text.get("1.0", "end-1c")
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._show_toast("✓ Copied to clipboard!")

    def _show_toast(self, message):
        self.copy_toast_var.set(message)
        self.after(2000, lambda: self.copy_toast_var.set(""))

    def _on_filter_changed(self, _event=None):
        self.active_log_filter = self.filter_var.get()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")

        for entry in self._raw_logs:
            if self._matches_filter(entry):
                self.log_text.insert("end", entry["line"] + "\n", entry["tag"])

        if self.autoscroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._update_log_status()

    def _matches_filter(self, entry):
        flt = self.active_log_filter
        if flt == "All":
            return True
        elif flt == "Orchestrator":
            return entry["kind"] == "orchestrator"
        elif flt == "Errors":
            return entry["kind"] == "error"
        elif flt == "Warnings":
            return entry["kind"] == "warning"
        return True

    def on_find_next(self):
        query = self.search_var.get().strip()
        self.log_text.tag_remove("search_match", "1.0", "end")
        if not query:
            return

        # Search from current insert position or beginning
        start_pos = self.log_text.index("insert")
        idx = self.log_text.search(query, start_pos, nocase=True, stopindex="end")
        if not idx:
            # Wrap around to start
            idx = self.log_text.search(query, "1.0", nocase=True, stopindex=start_pos)

        if idx:
            end_idx = f"{idx}+{len(query)}c"
            self.log_text.tag_add("search_match", idx, end_idx)
            self.log_text.mark_set("insert", end_idx)
            self.log_text.see(idx)

    def on_clear_search(self):
        self.search_var.set("")
        self.log_text.tag_remove("search_match", "1.0", "end")

    def _update_log_status(self):
        count = len(self._raw_logs)
        state_str = "Running" if self.engine.running else "Idle"
        self.log_status_var.set(f"{state_str} | Lines: {count}")

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
            self._update_log_status()
        self.after(POLL_MS, self._poll_queue)

    def _append_log(self, line):
        tag = None
        log_kind = "info"
        if line.startswith("[orchestrator]"):
            tag = "orchestrator"
            log_kind = "orchestrator"
            if "ERROR" in line or "FAILED" in line:
                tag = "error"
                log_kind = "error"
            elif "WARNING" in line or "PAUSED" in line:
                tag = "warning"
                log_kind = "warning"
        elif "error:" in line.lower() or "exception:" in line.lower() or "traceback" in line.lower():
            tag = "error"
            log_kind = "error"

        entry = {"line": line, "tag": tag, "kind": log_kind}
        self._raw_logs.append(entry)
        if len(self._raw_logs) > MAX_LOG_LINES:
            self._raw_logs.pop(0)

        # Only insert if it matches current filter
        if self._matches_filter(entry):
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

        self._update_log_status()

    def _set_status_label(self, job_id, status, detail=""):
        lbl = self.status_labels.get(job_id)
        if lbl:
            icon = STATUS_ICONS.get(status, "○")
            text = STATUS_LABELS.get(status, status)
            full_text = f"{icon} {text}"
            if detail:
                full_text = f"{full_text} ({detail})"
            color = STATUS_COLORS.get(status, STATUS_COLORS.get(STATUS_PENDING))
            lbl.configure(text=full_text, foreground=color)

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
        pct = int((done / total) * 100) if total else 0
        self.progress_var.set(f"{done}/{total} done ({pct}%)")

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


def main(initial_gpu=None):
    if initial_gpu is None and len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser(description="YouTube Video-Gen Pipeline Orchestrator GUI")
        parser.add_argument(
            "--gpu",
            nargs="?",
            const=True,
            default=None,
            type=str2bool,
            help="Enable GPU acceleration by default for projects (default: False)",
        )
        parsed, _ = parser.parse_known_args()
        if parsed.gpu is not None:
            initial_gpu = parsed.gpu

    app = App(initial_gpu=initial_gpu)
    app.mainloop()
