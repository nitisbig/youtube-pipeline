"""
The engine that actually runs the 8 workers.

Design:
- One background thread runs the sequence of steps so the Tkinter
  mainloop never blocks.
- Every worker is invoked as a real subprocess (list-argv for normal
  tools, shell=True for the subtitle step which needs mktemp/&&).
  Each subprocess gets its own session (start_new_session) so Stop
  can kill the whole process group, not just the top-level shell.
- Output is streamed line-by-line to a callback (log_callback) AND
  appended to <project>/pipeline.log, so state survives an app
  restart.
- Before a worker is spawned its `precheck` runs: missing inputs
  (voiceover.md, audio.mp3, images ...) fail fast with a message that
  says exactly what is missing instead of a cryptic traceback from the
  worker.
- Failed steps are retried automatically (settings["retries"][job_id]
  times, with settings["retry_delay_seconds"] between attempts).
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

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import jobs as job_defs
from .config import PIPELINE_ROOT
from .state import (
    ProjectState,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_STOPPED,
)

IS_POSIX = os.name == "posix"
KILL_GRACE_SECONDS = 5


class PipelineEngine:
    def __init__(self, settings, log_callback=None, status_callback=None, finished_callback=None):
        """
        settings: dict from Config.data
        log_callback(line: str): called (from the worker thread!) for every
            line of output / orchestrator message. Must be thread-safe on
            the caller's side (e.g. push to a queue.Queue).
        status_callback(job_id: str, status: str, detail: str): called on
            every step status transition. Same thread-safety note applies.
        finished_callback(): called when a sequence or single run ends.
        """
        self.settings = settings
        self.log_callback = log_callback or (lambda line: None)
        self.status_callback = status_callback or (lambda job_id, status, detail="": None)
        self.finished_callback = finished_callback or (lambda: None)

        self.job_ids = job_defs.JOB_IDS
        self.jobs_by_id = job_defs.JOBS_BY_ID

        self.state = None  # ProjectState, set by new_project()/load_project()
        self.ctx = {}

        self._thread = None
        self._stop_event = threading.Event()
        self._current_proc = None
        self._lock = threading.Lock()
        self.running = False
        self.current_job_id = None

    @property
    def pipeline_root(self):
        return Path(self.settings.get("pipeline_root") or PIPELINE_ROOT)

    # ------------------------------------------------------------------
    # project setup
    # ------------------------------------------------------------------
    def new_project(self, slug, params):
        """Create out/<slug>/ with the given params, or load it if it exists.

        Returns (project_dir, is_new).
        """
        project_dir = self.pipeline_root / "out" / slug
        state = ProjectState(project_dir, self.job_ids)
        if state.load_error:
            self._log(f"[orchestrator] WARNING: {state.load_error}")

        if state.exists_on_disk():
            # Don't clobber an existing project's progress - just load it.
            self.state = state
            self._build_ctx()
            self._log(
                f"[orchestrator] Project '{slug}' already exists - loaded its saved state and settings "
                "instead of resetting it."
            )
            return project_dir, False

        full = dict(self.settings.get("defaults", {}))
        full.update(params or {})
        full["slug"] = slug
        state.set_params(full)
        self.state = state
        self._build_ctx()
        self._log(f"[orchestrator] Created new project '{slug}' at {project_dir}")
        return project_dir, True

    def load_project(self, project_dir):
        self.state = ProjectState(project_dir, self.job_ids)
        if self.state.load_error:
            self._log(f"[orchestrator] WARNING: {self.state.load_error}")
        self._build_ctx()

    def update_params(self, params):
        """Merge edited GUI values into the loaded project (slug is immutable)."""
        if self.state is None:
            return
        merged = dict(params or {})
        merged.pop("slug", None)
        self.state.update_params(merged)
        self._build_ctx()

    def _build_ctx(self):
        params = self.state.get_params()
        ctx = dict(params)
        ctx["project_dir"] = str(self.state.project_dir)
        ctx["slug"] = self.state.project_dir.name

        image_source = str(ctx.get("image_source") or "").strip()
        if ctx.get("image_subfolder"):
            downloads = self.settings.get("image_gen", {}).get("downloads_dir") or (Path.home() / "Downloads")
            image_source = str(Path(downloads).expanduser() / ctx["slug"])
        ctx["image_source"] = str(Path(image_source).expanduser()) if image_source else ""
        self.ctx = ctx

    def _write_project_inputs(self):
        """Materialise GUI-only values that workers read from disk."""
        paths = job_defs.project_paths(self.ctx)
        text = str(self.ctx.get("custom_instructions") or "").strip()
        path = paths["instructions"]
        try:
            if text:
                paths["project_dir"].mkdir(parents=True, exist_ok=True)
                path.write_text(text + "\n", encoding="utf-8")
            elif path.exists():
                path.unlink()
        except OSError as exc:
            self._log(f"[orchestrator] WARNING: could not write {path.name}: {exc}")

    def final_video_path(self):
        if self.state is None:
            return None
        return job_defs.project_paths(self.ctx)["final"]

    @staticmethod
    def list_projects(pipeline_root):
        """Return [(slug, project_dir, last_updated)] for every project under out/, newest first."""
        out_dir = Path(pipeline_root) / "out"
        results = []
        if not out_dir.exists():
            return results
        for entry in sorted(out_dir.iterdir()):
            if not entry.is_dir():
                continue
            state_file = entry / ".pipeline_state.json"
            if not state_file.exists():
                continue
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append((entry.name, entry, data.get("last_updated", 0) or 0))
            except (OSError, ValueError):
                results.append((entry.name, entry, 0))
        results.sort(key=lambda r: r[2], reverse=True)
        return results

    # ------------------------------------------------------------------
    # logging / status helpers
    # ------------------------------------------------------------------
    def _log(self, line):
        for text in str(line).splitlines() or [""]:
            self.log_callback(text)
            if self.state is not None:
                try:
                    with open(self.state.log_path(), "a", encoding="utf-8") as f:
                        f.write(text + "\n")
                except OSError:
                    pass

    def _set_status(self, job_id, status, detail="", **extra):
        self.state.set_step_status(job_id, status, **extra)
        self.status_callback(job_id, status, detail)

    def _retries_for(self, job_id):
        try:
            return max(0, int(self.settings.get("retries", {}).get(job_id, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _retry_delay(self):
        try:
            return max(0.0, float(self.settings.get("retry_delay_seconds", 5)))
        except (TypeError, ValueError):
            return 5.0

    # ------------------------------------------------------------------
    # run control (sequential pipeline)
    # ------------------------------------------------------------------
    def start_full_pipeline(self, from_index=0):
        if self.running:
            self._log("[orchestrator] Pipeline is already running.")
            return False
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return False
        self._stop_event.clear()
        self.state.set_awaiting_manual_resume(False)
        self.running = True
        self._thread = threading.Thread(target=self._run_sequence, args=(from_index,), daemon=True)
        self._thread.start()
        return True

    def resume(self):
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return False
        idx = self.state.next_incomplete_index()
        if idx >= len(self.job_ids):
            self._log("[orchestrator] All steps already completed for this project.")
            return False
        self._log(f"[orchestrator] Resuming from step {idx + 1} ('{self.job_ids[idx]}').")
        return self.start_full_pipeline(from_index=idx)

    def stop(self):
        if not self.running:
            self._log("[orchestrator] Nothing is running.")
            return
        self._log("[orchestrator] Stop requested - terminating current worker...")
        self._stop_event.set()
        with self._lock:
            proc = self._current_proc
        if proc and proc.poll() is None:
            # Killing waits up to KILL_GRACE_SECONDS; do it off the GUI thread.
            threading.Thread(target=self._kill_process, args=(proc,), daemon=True).start()

    def _kill_process(self, proc):
        def signal_group(sig):
            if IS_POSIX:
                os.killpg(os.getpgid(proc.pid), sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()

        try:
            signal_group(signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except Exception:
            try:
                signal_group(signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def run_single(self, job_id):
        """Run exactly one step, independent of the sequential pipeline."""
        if self.running:
            self._log("[orchestrator] Cannot manually run a step while the pipeline is running - Stop it first.")
            return False
        if self.state is None:
            self._log("[orchestrator] No project loaded.")
            return False
        if job_id not in self.jobs_by_id:
            self._log(f"[orchestrator] Unknown step '{job_id}'.")
            return False
        self._stop_event.clear()
        self.running = True
        t = threading.Thread(target=self._run_single_safe, args=(job_id,), daemon=True)
        t.start()
        return True

    def _run_single_safe(self, job_id):
        try:
            self._run_job(job_id)
        finally:
            self.running = False
            self.current_job_id = None
            self.finished_callback()

    def reset_step(self, job_id):
        if self.running:
            self._log("[orchestrator] Cannot reset a step while the pipeline is running.")
            return False
        if self.state is None:
            return False
        self.state.reset_step(job_id)
        self.status_callback(job_id, self.state.get_step_status(job_id), "")
        self._log(f"[orchestrator] Step '{job_id}' reset to pending.")
        return True

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
                    if not self._stop_event.is_set():
                        self._log(
                            f"[orchestrator] Halting sequence - '{job_id}' did not finish successfully. "
                            "Fix the problem and click RESUME."
                        )
                    return

                job_meta = self.jobs_by_id[job_id]
                if job_meta.get("auto_pause_after"):
                    msg = job_meta.get("pause_message", "Manual check required. Click Resume to continue.")
                    self._log(f"[orchestrator] PAUSED after '{job_id}': {msg}")
                    self.state.set_awaiting_manual_resume(True, reason=msg)
                    return

            self._log(f"[orchestrator] Pipeline complete - all {len(self.job_ids)} steps finished. \U0001F3AC")
            final = self.final_video_path()
            if final and final.exists():
                self._log(f"[orchestrator] Final video: {final}")
        finally:
            self.running = False
            self.current_job_id = None
            self.finished_callback()

    def _run_job(self, job_id):
        """Precheck + run with retries. Returns True on success."""
        job_meta = self.jobs_by_id[job_id]
        self.current_job_id = job_id
        self._write_project_inputs()

        precheck = job_meta.get("precheck")
        if precheck:
            try:
                errors, warnings = precheck(self.ctx, self.settings)
            except Exception as exc:  # a broken precheck must never block the run
                errors, warnings = [], [f"precheck for '{job_id}' crashed: {exc}"]
            for warning in warnings:
                self._log(f"[orchestrator] WARNING ({job_id}): {warning}")
            if errors:
                for error in errors:
                    self._log(f"[orchestrator] ERROR ({job_id}): {error}")
                now = time.time()
                self._set_status(job_id, STATUS_FAILED, "precheck", started_at=now, finished_at=now, error="; ".join(errors))
                return False

        retries = self._retries_for(job_id)
        total_attempts = retries + 1
        delay = self._retry_delay()

        for attempt in range(1, total_attempts + 1):
            result, returncode = self._run_job_once(job_id, attempt, total_attempts)
            if result == "success":
                return True
            if result == "stopped":
                return False

            if attempt < total_attempts:
                self._log(
                    f"[orchestrator] '{job_id}' failed (exit code {returncode}, attempt {attempt}/{total_attempts}). "
                    f"Retrying in {delay:.0f}s..."
                )
                self._set_status(job_id, STATUS_RUNNING, f"retry {attempt + 1}/{total_attempts} in {delay:.0f}s")
                if self._stop_event.wait(delay):
                    self._set_status(job_id, STATUS_STOPPED, "", finished_at=time.time(), returncode=returncode)
                    self._log(f"[orchestrator] '{job_id}' stopped while waiting to retry.")
                    return False
            else:
                self._set_status(job_id, STATUS_FAILED, f"exit {returncode}", finished_at=time.time(), returncode=returncode)
                self._log(f"[orchestrator] '{job_id}' FAILED (exit code {returncode}).")
        return False

    def _run_job_once(self, job_id, attempt, total_attempts):
        """One attempt. Returns ("success" | "failed" | "stopped", returncode)."""
        job_meta = self.jobs_by_id[job_id]
        try:
            cmd, cwd_rel, is_shell = job_meta["builder"](self.ctx, self.settings)
        except Exception as exc:
            self._log(f"[orchestrator] ERROR building the command for '{job_id}': {exc}")
            self._set_status(job_id, STATUS_FAILED, "bad command", finished_at=time.time(), error=str(exc))
            return "stopped", -1  # not retryable

        cwd = self.pipeline_root / cwd_rel
        if not cwd.is_dir():
            self._log(f"[orchestrator] ERROR: working directory for '{job_id}' does not exist: {cwd}")
            self._set_status(job_id, STATUS_FAILED, "missing folder", finished_at=time.time(), error=f"missing cwd {cwd}")
            return "stopped", -1

        detail = f"attempt {attempt}/{total_attempts}" if total_attempts > 1 else ""
        self._set_status(job_id, STATUS_RUNNING, detail, started_at=time.time(), finished_at=None, attempt=attempt, error=None)
        cmd_display = cmd if isinstance(cmd, str) else " ".join(_quote_for_display(part) for part in cmd)
        self._log("")
        self._log(f"[orchestrator] === Running '{job_id}' in {cwd} ===")
        self._log(f"[orchestrator] $ {cmd_display}")

        popen_kwargs = dict(
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if IS_POSIX:
            popen_kwargs["start_new_session"] = True
        if is_shell and IS_POSIX:
            popen_kwargs["executable"] = "/bin/bash"

        try:
            proc = subprocess.Popen(cmd, shell=is_shell, **popen_kwargs)
        except FileNotFoundError as exc:
            self._log(f"[orchestrator] ERROR: executable not found for '{job_id}': {exc}")
            return "failed", 127
        except Exception as exc:
            self._log(f"[orchestrator] ERROR starting '{job_id}': {exc}")
            return "failed", 1

        with self._lock:
            self._current_proc = proc

        try:
            for line in proc.stdout:
                self._log(line.rstrip("\r\n"))
        except Exception as exc:
            self._log(f"[orchestrator] ERROR reading output: {exc}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

        returncode = proc.wait()
        with self._lock:
            self._current_proc = None

        if returncode == 0:
            elapsed = self.state.step_elapsed(job_id) or 0
            self._set_status(job_id, STATUS_SUCCESS, "", finished_at=time.time(), returncode=0)
            self._log(f"[orchestrator] '{job_id}' finished successfully in {elapsed:.0f}s.")
            return "success", 0

        if self._stop_event.is_set():
            self._set_status(job_id, STATUS_STOPPED, "", finished_at=time.time(), returncode=returncode)
            self._log(f"[orchestrator] '{job_id}' stopped (exit code {returncode}).")
            return "stopped", returncode

        return "failed", returncode


def _quote_for_display(part):
    part = str(part)
    if not part:
        return "''"
    if any(ch.isspace() for ch in part) or '"' in part:
        return '"' + part.replace('"', '\\"') + '"'
    return part
