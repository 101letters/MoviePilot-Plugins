"""任务记录管理：JSON 持久化、原子写入、线程安全、历史裁剪。"""
import copy
import json
import os
import threading
import time
import uuid
from typing import Optional


class TaskManager:
    def __init__(self, tasks_file: str, max_history: int = 100):
        self.tasks_file = tasks_file
        try:
            self.max_history = max(1, int(max_history or 100))
        except Exception:
            self.max_history = 100
        self.lock = threading.RLock()
        self.data = {"tasks": []}
        self._load()

    def _load(self):
        if not os.path.exists(self.tasks_file):
            self.data = {"tasks": []}
            return
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.data = {"tasks": []}
            return
        if not isinstance(data, dict):
            self.data = {"tasks": []}
            return
        if not isinstance(data.get("tasks"), list):
            data["tasks"] = []
        self.data = data

    def _save(self):
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        tmp = self.tasks_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.tasks_file)

    def _trim_history(self):
        tasks = self.data.get("tasks", [])
        if len(tasks) > self.max_history:
            self.data["tasks"] = tasks[-self.max_history:]

    def _generate_id(self):
        ts = time.strftime("%Y%m%d%H%M%S")
        rand = uuid.uuid4().hex[:6]
        return f"task_{ts}_{rand}"

    def _now_str(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def create_task(self, job: dict, trigger: str) -> dict:
        with self.lock:
            now = self._now_str()
            task = {
                "id": self._generate_id(),
                "job_id": job["id"],
                "job_name": job.get("name", ""),
                "trigger": trigger,
                "status": "pending",
                "sync_mode": int(job.get("sync_mode", 0)),
                "src_dir": job.get("src_dir", ""),
                "dst_dir": job.get("dst_dir", ""),
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "summary": {},
                "detail": {},
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            self.data["tasks"].append(task)
            self._trim_history()
            self._save()
            return task

    def mark_running(self, task_id: str):
        with self.lock:
            task = self._find_task(task_id)
            if task:
                now = self._now_str()
                task["status"] = "running"
                task["started_at"] = now
                task["updated_at"] = now
                self._save()

    def mark_success(self, task_id: str, result: dict):
        with self.lock:
            task = self._find_task(task_id)
            if task:
                finished_at = self._now_str()
                task["status"] = "success"
                task["finished_at"] = finished_at
                task["summary"] = result.get("summary", {})
                task["detail"] = {
                    "copied": result.get("copied", []),
                    "deleted": result.get("deleted", []),
                    "moved": result.get("moved", []),
                    "skipped": result.get("skipped", []),
                    "conflicts": result.get("conflicts", []),
                    "failed": result.get("failed", []),
                }
                started = task.get("started_at")
                if started:
                    import datetime
                    try:
                        s = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                        f = datetime.datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S")
                        task["duration_seconds"] = int((f - s).total_seconds())
                    except Exception:
                        task["duration_seconds"] = 0
                task["updated_at"] = finished_at
                self._save()

    def mark_failed(self, task_id: str, error: str):
        with self.lock:
            task = self._find_task(task_id)
            if task:
                finished_at = self._now_str()
                task["status"] = "failed"
                task["finished_at"] = finished_at
                task["error"] = error
                started = task.get("started_at")
                if started:
                    import datetime
                    try:
                        s = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                        f = datetime.datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S")
                        task["duration_seconds"] = int((f - s).total_seconds())
                    except Exception:
                        task["duration_seconds"] = 0
                task["updated_at"] = finished_at
                self._save()

    def list_tasks(self, limit: int = 20, job_id: str = None, status: str = None) -> list:
        with self.lock:
            try:
                limit = int(limit)
            except Exception:
                limit = 20
            limit = max(1, min(limit, 200))
            tasks = self.data.get("tasks", [])
            if job_id:
                tasks = [t for t in tasks if t.get("job_id") == job_id]
            if status:
                tasks = [t for t in tasks if t.get("status") == status]
            tasks = sorted(tasks, key=lambda t: t.get("updated_at", ""), reverse=True)
            return copy.deepcopy(tasks[:limit])

    def get_task(self, task_id: str) -> Optional[dict]:
        with self.lock:
            for t in self.data.get("tasks", []):
                if t.get("id") == task_id:
                    return copy.deepcopy(t)
            return None

    def has_running_task(self, job_id: str) -> bool:
        with self.lock:
            return any(
                t.get("job_id") == job_id and t.get("status") in ("pending", "running")
                for t in self.data.get("tasks", [])
            )

    def _find_task(self, task_id: str) -> Optional[dict]:
        for t in self.data.get("tasks", []):
            if t["id"] == task_id:
                return t
        return None
