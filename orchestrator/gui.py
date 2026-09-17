"""
Tkinter GUI for the pipeline orchestrator.

Layout:
    - New / Load Project panel (title, duration, art-style, folder slug
      + existing-project picker)
    - Steps panel: one row per worker, showing live status, with a
      "Run" button per row for manual/one-off triggering
    - Controls: START / STOP / RESUME / QUIT
    - Log console (scrolled text), also written to <project>/pipeline.log

Threading note: PipelineEngine calls log_callback/status_callback from
its background worker thread. Tkinter widgets may only be touched from
the main thread, so those callbacks just push onto a queue.Queue, and
App.poll_queue() (scheduled via root.after) drains it on the main
thread.
"""

import json
import queue
import re
import tkinter as tk
from tkinter import messagebox, ttk

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


def slugify(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "untitled"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Video-Gen Pipeline Orchestrator")
        self.geometry("980x720")
        self.minsize(860, 620)

        self.config_mgr = Config()
        self.settings = self.config_mgr.data

        self.event_queue = queue.Queue()
        self.engine = PipelineEngine(
            self.settings,
            log_callback=self._on_engine_log,
            status_callback=self._on_engine_status,
        )

        self.status_labels = {}
        self.run_buttons = {}

        self._build_widgets()
        self._refresh_project_list()
        self.after(100, self._poll_queue)

        self.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ------------------------------------------------------------------
    # widget construction
    # ------------------------------------------------------------------
    def _build_widgets(self):
        root_pad = {"padx": 10, "pady": 6}

        # ---- Project panel ----
        proj_frame = ttk.LabelFrame(self, text="Project")
        proj_frame.pack(fill="x", **root_pad)

        grid = ttk.Frame(proj_frame)
        grid.pack(fill="x", padx=8, pady=8)

        ttk.Label(grid, text="Title").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(grid, textvariable=self.title_var, width=45)
        title_entry.grid(row=0, column=1, sticky="we", padx=(4, 16))
        title_entry.bind("<KeyRelease>", self._on_title_typed)

        ttk.Label(grid, text="Duration (min)").grid(row=0, column=2, sticky="w")
        self.duration_var = tk.StringVar(value=str(self.settings.get("defaults", {}).get("duration", 3)))
        ttk.Entry(grid, textvariable=self.duration_var, width=8).grid(row=0, column=3, sticky="w", padx=(4, 16))

        ttk.Label(grid, text="Art style").grid(row=0, column=4, sticky="w")
        self.art_style_var = tk.StringVar(value=self.settings.get("defaults", {}).get("art_style", ""))
        ttk.Entry(grid, textvariable=self.art_style_var, width=28).grid(row=0, column=5, sticky="we", padx=(4, 0))

        ttk.Label(grid, text="Output folder name").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.slug_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.slug_var, width=45).grid(row=1, column=1, sticky="we", padx=(4, 16), pady=(6, 0))

        self.create_btn = ttk.Button(grid, text="Create / Load Project", command=self.on_create_project)
        self.create_btn.grid(row=1, column=2, columnspan=2, sticky="we", pady=(6, 0))

        for c in (1, 5):
            grid.columnconfigure(c, weight=1)

        # existing projects row
        existing_row = ttk.Frame(proj_frame)
        existing_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(existing_row, text="Existing projects:").pack(side="left")
        self.existing_var = tk.StringVar()
        self.existing_combo = ttk.Combobox(existing_row, textvariable=self.existing_var, state="readonly", width=40)
        self.existing_combo.pack(side="left", padx=6)
        ttk.Button(existing_row, text="Load Selected", command=self.on_load_existing).pack(side="left", padx=4)
        ttk.Button(existing_row, text="Refresh", command=self._refresh_project_list).pack(side="left", padx=4)

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

            ttk.Label(row, text=job["label"], width=28).pack(side="left")

            status_lbl = ttk.Label(row, text=STATUS_LABELS[STATUS_PENDING], foreground=STATUS_COLORS[STATUS_PENDING], width=12)
            status_lbl.pack(side="left")
            self.status_labels[job["id"]] = status_lbl

            btn = ttk.Button(row, text="Run", width=10, command=lambda jid=job["id"]: self.on_run_single(jid))
            btn.pack(side="right")
            self.run_buttons[job["id"]] = btn

        # ---- Controls ----
        controls = ttk.Frame(self)
        controls.pack(fill="x", **root_pad)

        self.start_btn = ttk.Button(controls, text="START", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(controls, text="STOP", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=4)
        self.resume_btn = ttk.Button(controls, text="RESUME", command=self.on_resume)
        self.resume_btn.pack(side="left", padx=4)
        ttk.Button(controls, text="Edit settings.json", command=self.on_edit_settings).pack(side="left", padx=4)
        ttk.Button(controls, text="QUIT", command=self.on_quit).pack(side="right", padx=4)

        # ---- Log console ----
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **root_pad)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", bg="#111111", fg="#d8d8d8", insertbackground="#ffffff")
        self.log_text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    # ------------------------------------------------------------------
    # project actions
    # ------------------------------------------------------------------
    def _on_title_typed(self, _event):
        # Auto-suggest a slug from the title, but only while the user
        # hasn't started customizing the slug field themselves.
        if not getattr(self, "_slug_manually_edited", False):
            self.slug_var.set(slugify(self.title_var.get()))

    def _refresh_project_list(self):
        pipeline_root = self.config_mgr.pipeline_root
        projects = PipelineEngine.list_projects(pipeline_root)
        self.existing_combo["values"] = [p[0] for p in projects]

    def on_create_project(self):
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("Title required", "Please enter a title for the video.")
            return
        slug = slugify(self.slug_var.get() or title)
        duration = self.duration_var.get().strip() or "3"
        art_style = self.art_style_var.get().strip()

        project_dir, is_new = self.engine.new_project(title, slug, duration, art_style)
        self.current_project_var.set(f"Project: {slug}  ({project_dir})")
        self._sync_all_status_labels()
        self._refresh_project_list()
        if is_new:
            self._append_log(f"[orchestrator] Created new project '{slug}' at {project_dir}")
        else:
            self._append_log(f"[orchestrator] Loaded existing project '{slug}'.")

    def on_load_existing(self):
        slug = self.existing_var.get()
        if not slug:
            messagebox.showinfo("Pick a project", "Select a project from the dropdown first.")
            return
        pipeline_root = self.config_mgr.pipeline_root
        project_dir = pipeline_root / "out" / slug
        self.engine.load_project(project_dir)

        params = self.engine.state.get_params()
        self.title_var.set(params.get("title", ""))
        self.slug_var.set(params.get("slug", slug))
        self.duration_var.set(str(params.get("duration", "")))
        self.art_style_var.set(params.get("art_style", ""))
        self._slug_manually_edited = True

        self.current_project_var.set(f"Project: {slug}  ({project_dir})")
        self._sync_all_status_labels()
        self._append_log(f"[orchestrator] Loaded project '{slug}'.")

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

    def on_start(self):
        if not self._require_project():
            return
        self.engine.start_full_pipeline(from_index=0)

    def on_resume(self):
        if not self._require_project():
            return
        self.engine.resume()

    def on_stop(self):
        self.engine.stop()

    def on_run_single(self, job_id):
        if not self._require_project():
            return
        self.engine.run_single(job_id)

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
    # engine callbacks -> thread-safe queue -> UI
    # ------------------------------------------------------------------
    def _on_engine_log(self, line):
        self.event_queue.put(("log", line))

    def _on_engine_status(self, job_id, status):
        self.event_queue.put(("status", job_id, status))

    def _poll_queue(self):
        try:
            while True:
                item = self.event_queue.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "status":
                    self._set_status_label(item[1], item[2])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _append_log(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status_label(self, job_id, status):
        lbl = self.status_labels.get(job_id)
        if lbl:
            lbl.configure(text=STATUS_LABELS.get(status, status), foreground=STATUS_COLORS.get(status, "#000000"))

    def _sync_all_status_labels(self):
        if self.engine.state is None:
            return
        for jid in job_defs.JOB_IDS:
            status = self.engine.state.get_step_status(jid)
            self._set_status_label(jid, status)


class SettingsEditor(tk.Toplevel):
    """Simple raw-JSON editor for settings.json."""

    def __init__(self, parent, config_mgr, on_saved=None):
        super().__init__(parent)
        self.title("Edit settings.json")
        self.geometry("640x560")
        self.config_mgr = config_mgr
        self.on_saved = on_saved

        self.text = tk.Text(self, wrap="none")
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
        self.config_mgr.save(data)
        if self.on_saved:
            self.on_saved(data)
        self.destroy()


def main():
    app = App()
    app.mainloop()
