"""
The engine that actually runs the 7 workers.

Design:
- One background thread runs the sequence of steps so the Tkinter
  mainloop never blocks.
- Every worker is invoked as a real subprocess (list-argv for normal
  tools, shell=True for the subtitle step which needs mktemp/&&/;).
  Each subprocess's own process group is created (os.setsid) so Stop
  can kill the whole group, not just the top-level shell.
- Output is streamed line-by-line to a callback (log_callback) AND
  appended to <project>/pipeline.log, so state survives an app
  restart.
- Status per step is written to .pipeline_state.json after every
  transition (pending -> running -> success/failed/stopped), which is
  what makes Resume possible after closing the app.
- Steps can declare auto_pause_after=True (only the image-gen step
  does, currently). After such a step finishes, the engine stops the
  sequence and marks the project as "awaiting manual resume" instead
  of continuing straight to the next step - because that worker exits
  immediately after handing off to the browser extension, well before
  the images actually exist.
- run_single() lets you fire any one worker in isolation (for manual
  recovery when something upstream needed a manual fix), independent
  of the sequential run.
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import jobs as job_defs
from .state import (
    ProjectState,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_STOPPED,
)

IS_POSIX = os.name == "posix"


class PipelineEngine:
    def __init__(self, settings, log_callback=None, status_callback=None):
        """
        settings: dict from Config.data
        log_callback(line: str): called (from the worker thread!) for every
            line of output / orchestrator message. Must be thread-safe on
            the caller's side (e.g. push to a queue.Queue).
        status_callback(job_id: str, status: str): called on every step
            status transition. Same thread-safety note applies.
        """
        self.settings = settings
        self.log_callback = log_callback or (lambda line: None)
        self.status_callback = status_callback or (lambda jid, status: None)

        self.job_ids = job_defs.JOB_IDS
        self.jobs_by_id = job_defs.JOBS_BY_ID

        self.state = None  # ProjectState, set by new_project()/load_project()
        self.ctx = {}

        self._thread = None
        self._stop_event = threading.Event()
        self._current_proc = None
        self._lock = threading.Lock()
        self.running = False

    # ------------------------------------------------------------------
    # project setup
    # ------------------------------------------------------------------
    def new_project(self, title, slug, duration, art_style):
        pipeline_root = Path(self.settings["pipeline_root"])
        project_dir = pipeline_root / "out" / slug

        state = ProjectState(project_dir, self.job_ids)
        if state.exists_on_disk():
            # Don't clobber an existing project's progress - just load it.
            self._log(f"[orchestrator] Project '{slug}' already exists, loading its saved state instead of resetting it.")
            self.state = state
            self._build_ctx()
            return project_dir, False

        d = self.settings.get("defaults", {})
        params = {
            "title": title,
            "slug": slug,
            "duration": duration,
            "art_style": art_style,
            "tone": d.get("tone", ""),
            "depth": d.get("depth", ""),
            "palette": d.get("palette", ""),
            "keywords": d.get("keywords", []),
            "avoid": d.get("avoid", ""),
            "reference_id": d.get("reference_id", ""),
            "animation": d.get("animation", "fadein"),
            "image_source": d.get("image_source", ""),
        }
        state.set_params(params)
        self.state = state
        self._build_ctx()
        return project_dir, True

    def load_project(self, project_dir):
        self.state = ProjectState(project_dir, self.job_ids)
        self._build_ctx()

    def _build_ctx(self):
        params = self.state.get_params()
        ctx = dict(params)
        ctx["project_dir"] = str(self.state.project_dir)
        ctx.setdefault("slug", self.state.project_dir.name)
        self.ctx = ctx

    @staticmethod
    def list_projects(pipeline_root):
        """Return [(slug, project_dir, last_updated)] for every project under out/."""
        out_dir = Path(pipeline_root) / "out"
        results = []
        if not out_dir.exists():
            return results
        for entry in sorted(out_dir.iterdir()):
            if not entry.is_dir():
                continue
            state_file = entry / ".pipeline_state.json"
            if state_file.exists():
                try:
                    import json
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    results.append((entry.name, entry, data.get("last_updated", 0)))
                except Exception:
                    results.append((entry.name, entry, 0))
        results.sort(key=lambda r: r[2], reverse=True)
        return results

    # ------------------------------------------------------------------
    # logging / status helpers
    # ------------------------------------------------------------------
    def _log(self, line):
        for l in str(line).splitlines() or [""]:
            self.log_callback(l)
            if self.state is not None:
                try:
                    with open(self.state.log_path(), "a", encoding="utf-8") as f:
                        f.write(l + "\n")
                except Exception:
                    pass

    def _set_status(self, job_id, status, **extra):
        self.state.set_step_status(job_id, status, **extra)
        self.status_callback(job_id, status)

    # ------------------------------------------------------------------
    # run control (sequential pipeline)
    # ------------------------------------------------------------------
    def start_full_pipeline(self, from_index=0):
        if self.running:
            self._log("[orchestrator] Pipeline is already running.")
            return
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return
        self._stop_event.clear()
        self.state.set_awaiting_manual_resume(False)
        self.running = True
        self._thread = threading.Thread(target=self._run_sequence, args=(from_index,), daemon=True)
        self._thread.start()

    def resume(self):
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return
        idx = self.state.next_incomplete_index()
        if idx >= len(self.job_ids):
            self._log("[orchestrator] All steps already completed for this project.")
            return
        self.start_full_pipeline(from_index=idx)

    def stop(self):
        if not self.running:
            self._log("[orchestrator] Nothing is running.")
            return
        self._log("[orchestrator] Stop requested - terminating current worker...")
        self._stop_event.set()
        with self._lock:
            proc = self._current_proc
        if proc and proc.poll() is None:
            self._kill_process(proc)

    def _kill_process(self, proc):
        try:
            if IS_POSIX:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        # give it a moment, then force-kill if still alive
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                if IS_POSIX:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                pass

    def run_single(self, job_id):
        """Run exactly one step, independent of the sequential pipeline."""
        if self.running:
            self._log("[orchestrator] Cannot manually run a step while the pipeline is running - Stop it first.")
            return
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return
        self._stop_event.clear()
        self.running = True
        t = threading.Thread(target=self._run_single_safe, args=(job_id,), daemon=True)
        t.start()

    def _run_single_safe(self, job_id):
        try:
            self._run_job(job_id)
        finally:
            self.running = False

    # ------------------------------------------------------------------
    # core execution
    # ------------------------------------------------------------------
    def _run_sequence(self, from_index):
        try:
            for idx in range(from_index, len(self.job_ids)):
                if self._stop_event.is_set():
                    self._log("[orchestrator] Pipeline stopped by user.")
                    return
                job_id = self.job_ids[idx]
                ok = self._run_job(job_id)
                if not ok:
                    self._log(f"[orchestrator] Halting sequence - '{job_id}' did not finish successfully.")
                    return

                job_meta = self.jobs_by_id[job_id]
                if job_meta.get("auto_pause_after"):
                    msg = job_meta.get("pause_message", "Manual check required. Click Resume to continue.")
                    self._log(f"[orchestrator] PAUSED after '{job_id}': {msg}")
                    self.state.set_awaiting_manual_resume(True, reason=msg)
                    return

            self._log("[orchestrator] Pipeline complete - all 7 steps finished. \U0001F3AC")
        finally:
            self.running = False

    def _run_job(self, job_id):
        job_meta = self.jobs_by_id[job_id]
        builder = job_meta["builder"]
        cmd, cwd_rel, is_shell = builder(self.ctx, self.settings)
        pipeline_root = Path(self.settings["pipeline_root"])
        cwd = pipeline_root / cwd_rel

        self._set_status(job_id, STATUS_RUNNING, started_at=time.time())
        cmd_display = cmd if isinstance(cmd, str) else " ".join(cmd)
        self._log("")
        self._log(f"[orchestrator] === Running '{job_id}' in {cwd} ===")
        self._log(f"[orchestrator] $ {cmd_display}")

        popen_kwargs = dict(
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if IS_POSIX:
            popen_kwargs["preexec_fn"] = os.setsid

        try:
            if is_shell:
                proc = subprocess.Popen(cmd, shell=True, executable="/bin/bash", **popen_kwargs)
            else:
                proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as e:
            self._log(f"[orchestrator] ERROR: {e}")
            self._set_status(job_id, STATUS_FAILED, error=str(e))
            return False
        except Exception as e:
            self._log(f"[orchestrator] ERROR starting '{job_id}': {e}")
            self._set_status(job_id, STATUS_FAILED, error=str(e))
            return False

        with self._lock:
            self._current_proc = proc

        try:
            for line in proc.stdout:
                self._log(line.rstrip("\n"))
                if self._stop_event.is_set():
                    break
        except Exception as e:
            self._log(f"[orchestrator] ERROR reading output: {e}")

        proc.wait()
        with self._lock:
            self._current_proc = None

        if self._stop_event.is_set() and proc.returncode != 0:
            self._set_status(job_id, STATUS_STOPPED, finished_at=time.time(), returncode=proc.returncode)
            return False

        if proc.returncode == 0:
            self._set_status(job_id, STATUS_SUCCESS, finished_at=time.time(), returncode=0)
            self._log(f"[orchestrator] '{job_id}' finished successfully.")
            return True
        else:
            self._set_status(job_id, STATUS_FAILED, finished_at=time.time(), returncode=proc.returncode)
            self._log(f"[orchestrator] '{job_id}' FAILED (exit code {proc.returncode}).")
            return False
