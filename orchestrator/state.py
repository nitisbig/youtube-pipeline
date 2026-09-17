"""
Per-project state, persisted as JSON inside the project's own output
folder: out/<slug>/.pipeline_state.json

This is what makes the pipeline resumable: it records the status of
every step plus the exact parameters the project was created with, so
you can close the app, reopen it later, pick the project from the
"Existing projects" list, and pick up where you left off.
"""

import json
import time
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

ALL_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED)

STATE_FILENAME = ".pipeline_state.json"
LOG_FILENAME = "pipeline.log"


class ProjectState:
    def __init__(self, project_dir, job_ids):
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / STATE_FILENAME
        self.job_ids = list(job_ids)
        self.load_error = None
        self.data = self._load_or_init()

    # ------------------------------------------------------------------
    def _fresh(self):
        return {
            "project_dir": str(self.project_dir),
            "created_at": time.time(),
            "params": {},
            "steps": {jid: {"status": STATUS_PENDING} for jid in self.job_ids},
            "awaiting_manual_resume": False,
            "pause_reason": None,
            "last_updated": time.time(),
        }

    def _load_or_init(self):
        if not self.path.exists():
            return self._fresh()

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state file is not a JSON object")
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{STATE_FILENAME}.broken-{int(time.time())}")
            try:
                self.path.replace(backup)
                where = f" (moved to {backup.name})"
            except OSError:
                where = ""
            self.load_error = f"Corrupted state file{where}, starting fresh: {exc}"
            return self._fresh()

        steps = data.setdefault("steps", {})
        for jid in self.job_ids:
            steps.setdefault(jid, {"status": STATUS_PENDING})
        # A step that is still marked "running" means the app was killed
        # mid-run. Nothing is running now, so treat it as stopped - otherwise
        # the GUI would show a phantom "running..." forever.
        for step in steps.values():
            if step.get("status") == STATUS_RUNNING:
                step["status"] = STATUS_STOPPED
                step["finished_at"] = step.get("finished_at") or time.time()
        data.setdefault("params", {})
        data.setdefault("awaiting_manual_resume", False)
        data.setdefault("pause_reason", None)
        return data

    def save(self):
        self.data["last_updated"] = time.time()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(STATE_FILENAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)

    def exists_on_disk(self):
        return self.path.exists()

    # ---- params ----
    def set_params(self, params):
        self.data["params"] = dict(params)
        self.save()

    def update_params(self, partial):
        params = self.data.setdefault("params", {})
        params.update(partial or {})
        self.save()

    def get_params(self):
        return self.data.get("params", {})

    # ---- steps ----
    def set_step_status(self, job_id, status, **extra):
        step = self.data.setdefault("steps", {}).setdefault(job_id, {})
        step["status"] = status
        step.update(extra)
        step["updated_at"] = time.time()
        self.save()

    def get_step_status(self, job_id):
        return self.data.get("steps", {}).get(job_id, {}).get("status", STATUS_PENDING)

    def get_step(self, job_id):
        return self.data.get("steps", {}).get(job_id, {})

    def reset_step(self, job_id):
        self.data.setdefault("steps", {})[job_id] = {"status": STATUS_PENDING}
        self.save()

    def reset_all(self):
        self.data["steps"] = {jid: {"status": STATUS_PENDING} for jid in self.job_ids}
        self.data["awaiting_manual_resume"] = False
        self.data["pause_reason"] = None
        self.save()

    def step_elapsed(self, job_id, now=None):
        """Seconds a step has been / was running, or None if it never started."""
        step = self.get_step(job_id)
        started = step.get("started_at")
        if not started:
            return None
        if step.get("status") == STATUS_RUNNING:
            return (now or time.time()) - started
        finished = step.get("finished_at")
        return (finished - started) if finished else None

    def next_incomplete_index(self):
        """Index of the first step that hasn't succeeded yet."""
        for i, jid in enumerate(self.job_ids):
            if self.get_step_status(jid) != STATUS_SUCCESS:
                return i
        return len(self.job_ids)

    def completed_count(self):
        return sum(1 for jid in self.job_ids if self.get_step_status(jid) == STATUS_SUCCESS)

    def any_completed(self):
        return self.completed_count() > 0

    def set_awaiting_manual_resume(self, flag, reason=None):
        self.data["awaiting_manual_resume"] = bool(flag)
        self.data["pause_reason"] = reason
        self.save()

    def is_awaiting_manual_resume(self):
        return bool(self.data.get("awaiting_manual_resume"))

    def all_done(self):
        return all(self.get_step_status(jid) == STATUS_SUCCESS for jid in self.job_ids)

    def log_path(self):
        return self.project_dir / LOG_FILENAME
