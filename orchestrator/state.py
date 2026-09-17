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

STATE_FILENAME = ".pipeline_state.json"
LOG_FILENAME = "pipeline.log"


class ProjectState:
    def __init__(self, project_dir, job_ids):
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / STATE_FILENAME
        self.job_ids = list(job_ids)
        self.data = self._load_or_init()

    def _load_or_init(self):
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # make sure any newly-added job ids show up as pending
            for jid in self.job_ids:
                data.setdefault("steps", {}).setdefault(jid, {"status": STATUS_PENDING})
            return data
        return {
            "project_dir": str(self.project_dir),
            "created_at": time.time(),
            "params": {},
            "steps": {jid: {"status": STATUS_PENDING} for jid in self.job_ids},
            "awaiting_manual_resume": False,
            "pause_reason": None,
            "last_updated": time.time(),
        }

    def save(self):
        self.data["last_updated"] = time.time()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def exists_on_disk(self):
        return self.path.exists()

    # ---- params ----
    def set_params(self, params):
        self.data["params"] = params
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

    def next_incomplete_index(self):
        """Index of the first step that hasn't succeeded yet."""
        for i, jid in enumerate(self.job_ids):
            if self.get_step_status(jid) != STATUS_SUCCESS:
                return i
        return len(self.job_ids)

    def set_awaiting_manual_resume(self, flag, reason=None):
        self.data["awaiting_manual_resume"] = flag
        self.data["pause_reason"] = reason
        self.save()

    def is_awaiting_manual_resume(self):
        return bool(self.data.get("awaiting_manual_resume"))

    def all_done(self):
        return all(self.get_step_status(jid) == STATUS_SUCCESS for jid in self.job_ids)

    def log_path(self):
        return self.project_dir / LOG_FILENAME
