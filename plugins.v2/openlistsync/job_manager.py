"""Job CRUD — parse & modify the ``jobs_json`` config field."""
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Callable


class JobManager:
    """Manage sync jobs stored in the plugin's ``jobs_json`` config field."""

    def __init__(self, save_callback: Optional[Callable[[List[dict]], None]] = None):
        """*save_callback* is called with the full job list after any mutation."""
        self._jobs: List[dict] = []
        self._save_callback = save_callback

    # ------------------------------------------------------------------
    # load / save
    # ------------------------------------------------------------------

    def load(self, jobs_json: str) -> None:
        """Parse *jobs_json* string into internal list."""
        text = (jobs_json or "").strip()
        if not text:
            self._jobs = []
            return
        try:
            parsed = json.loads(text)
            self._jobs = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            self._jobs = []

    def to_json(self) -> str:
        """Export current jobs as a JSON string."""
        return json.dumps(self._jobs, ensure_ascii=False, indent=2)

    def _save(self) -> None:
        if self._save_callback:
            self._save_callback(self._jobs)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _normalize_job(self, job: dict, existing: Optional[dict] = None) -> dict:
        """Fill defaults and normalize a job dict."""
        normalized = {
            "id": job.get("id") or str(uuid.uuid4())[:8],
            "name": job.get("name", ""),
            "src_dir": (job.get("src_dir", "") or "").rstrip("/"),
            "dst_dir": (job.get("dst_dir", "") or "").rstrip("/"),
            "sync_mode": int(job.get("sync_mode", 0)),
            "exclude_rules": list(job.get("exclude_rules") or []),
            "interval_minutes": int(job.get("interval_minutes", 60) or 60),
            "enabled": bool(job.get("enabled", True)),
        }
        if normalized["src_dir"] and not normalized["src_dir"].startswith("/"):
            normalized["src_dir"] = "/" + normalized["src_dir"]
        if normalized["dst_dir"] and not normalized["dst_dir"].startswith("/"):
            normalized["dst_dir"] = "/" + normalized["dst_dir"]

        # Preserve runtime fields from existing
        if existing:
            normalized["last_run_at"] = existing.get("last_run_at")
            normalized["next_run_at"] = existing.get("next_run_at")
        else:
            normalized["last_run_at"] = None
            normalized["next_run_at"] = None

        return normalized

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def list_jobs(self) -> List[dict]:
        return list(self._jobs)

    def get_job(self, job_id: str) -> Optional[dict]:
        for j in self._jobs:
            if j.get("id") == job_id:
                return j
        return None

    def add_job(self, job: dict) -> dict:
        """Add a new job.  Generates ``id`` if missing."""
        normalized = self._normalize_job(job)
        self._jobs.append(normalized)
        self._save()
        return normalized

    def update_job(self, job_id: str, updates: dict) -> Optional[dict]:
        """Update an existing job.  Returns updated job or None."""
        existing = self.get_job(job_id)
        if existing is None:
            return None
        merged = {**existing, **updates}
        normalized = self._normalize_job(merged, existing=existing)
        normalized["id"] = job_id  # keep original id
        for i, j in enumerate(self._jobs):
            if j.get("id") == job_id:
                self._jobs[i] = normalized
                break
        self._save()
        return normalized

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by id.  Returns True if deleted."""
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.get("id") != job_id]
        if len(self._jobs) != before:
            self._save()
            return True
        return False

    def get_enabled_jobs(self) -> List[dict]:
        return [j for j in self._jobs if j.get("enabled", True)]

    # ------------------------------------------------------------------
    # runtime tracking
    # ------------------------------------------------------------------

    def mark_run_complete(self, job_id: str) -> None:
        """Update last_run_at and compute next_run_at."""
        job = self.get_job(job_id)
        if job is None:
            return
        now = datetime.now()
        job["last_run_at"] = now.isoformat()
        interval = int(job.get("interval_minutes", 60) or 60)
        job["next_run_at"] = (now + timedelta(minutes=interval)).isoformat()
        self._save()

    def compute_next_runs(self) -> None:
        """Ensure every enabled job has a next_run_at."""
        now = datetime.now()
        changed = False
        for job in self._jobs:
            if not job.get("enabled", True):
                continue
            if not job.get("next_run_at"):
                interval = int(job.get("interval_minutes", 60) or 60)
                job["next_run_at"] = (now + timedelta(minutes=interval)).isoformat()
                changed = True
        if changed:
            self._save()
