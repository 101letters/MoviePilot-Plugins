"""Task persistence — JSON file storage with atomic writes."""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.log import logger


DATA_DIR = Path(__file__).resolve().parent / "data"
TASKS_FILE = DATA_DIR / "tasks.json"


class TaskManager:
    """Read/write task records to ``data/tasks.json`` with atomic writes."""

    def __init__(self, max_history: int = 100):
        self._max_history = max_history
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> List[dict]:
        """Load all tasks from file.  Returns list (empty if file missing)."""
        if not TASKS_FILE.exists():
            return []
        try:
            text = TASKS_FILE.read_text(encoding="utf-8")
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"加载任务文件失败: {e}")
            return []

    def _save_all(self, tasks: List[dict]) -> None:
        """Atomically write tasks to file.  Truncates beyond *max_history*."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self._max_history > 0 and len(tasks) > self._max_history:
            tasks = tasks[-self._max_history:]
        tmp = TASKS_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(tasks, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(TASKS_FILE))
        except Exception as e:
            logger.error(f"保存任务文件失败: {e}")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _task_id() -> str:
        return f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def create_task(self, job: dict, trigger: str = "schedule") -> dict:
        """Create a new pending task record."""
        task = {
            "id": self._task_id(),
            "job_id": job.get("id", ""),
            "job_name": job.get("name", ""),
            "trigger": trigger,
            "status": "pending",
            "sync_mode": job.get("sync_mode", 0),
            "src_dir": job.get("src_dir", ""),
            "dst_dir": job.get("dst_dir", ""),
            "started_at": self._now(),
            "finished_at": None,
            "duration_seconds": 0,
            "summary": {"copied": 0, "deleted": 0, "moved": 0, "skipped": 0, "conflicts": 0, "failed": 0},
            "detail": {"copied": [], "deleted": [], "moved": [], "skipped": [], "conflicts": [], "failed": []},
            "error": None,
        }
        with self._lock:
            tasks = self._load_all()
            tasks.append(task)
            self._save_all(tasks)
        return task

    def _update(self, task_id: str, updates: dict) -> Optional[dict]:
        """Update a task record and persist."""
        with self._lock:
            tasks = self._load_all()
            for task in tasks:
                if task.get("id") == task_id:
                    task.update(updates)
                    self._save_all(tasks)
                    return task
        return None

    def mark_running(self, task_id: str) -> Optional[dict]:
        return self._update(task_id, {"status": "running"})

    def mark_success(self, task_id: str, summary: dict, detail: dict,
                     started_at: str = "") -> Optional[dict]:
        started_dt = None
        if started_at:
            try:
                started_dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        duration = 0
        if started_dt:
            duration = int((datetime.now() - started_dt).total_seconds())

        return self._update(task_id, {
            "status": "success",
            "finished_at": self._now(),
            "duration_seconds": duration,
            "summary": summary,
            "detail": detail,
        })

    def mark_failed(self, task_id: str, error: str, started_at: str = "") -> Optional[dict]:
        started_dt = None
        if started_at:
            try:
                started_dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        duration = 0
        if started_dt:
            duration = int((datetime.now() - started_dt).total_seconds())

        return self._update(task_id, {
            "status": "failed",
            "finished_at": self._now(),
            "duration_seconds": duration,
            "error": error,
        })

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            tasks = self._load_all()
            for t in tasks:
                if t.get("id") == task_id:
                    return t
        return None

    def list_tasks(self, limit: int = 20, job_id: str = None,
                   status: str = None) -> List[dict]:
        """List recent tasks, optionally filtered."""
        with self._lock:
            tasks = self._load_all()

        if job_id:
            tasks = [t for t in tasks if t.get("job_id") == job_id]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]

        # Return most recent last (so reversed first for slicing)
        tasks = tasks[-limit:] if limit > 0 else tasks
        return list(reversed(tasks))

    def has_running_task(self, job_id: str) -> bool:
        """Check if *job_id* has an active running task."""
        with self._lock:
            tasks = self._load_all()
        for t in tasks:
            if t.get("job_id") == job_id and t.get("status") in ("pending", "running"):
                return True
        return False

    def get_running_task(self, job_id: str) -> Optional[dict]:
        """Return the running/pending task for *job_id*, if any."""
        with self._lock:
            tasks = self._load_all()
        for t in tasks:
            if t.get("job_id") == job_id and t.get("status") in ("pending", "running"):
                return t
        return None
