"""作业管理：从插件配置 jobs_json 中读写同步作业。"""
import copy
import json
import time
import uuid
import datetime
import threading
from typing import Optional


class JobManager:
    def __init__(self, plugin=None):
        self.plugin = plugin
        self._jobs = []
        self.lock = threading.RLock()

    def _load_jobs(self) -> list:
        if not self.plugin:
            return []
        config = getattr(self.plugin, "config", None) or {}
        raw = config.get("jobs_json", "[]") or "[]"
        try:
            jobs = json.loads(raw)
        except Exception:
            return []
        if not isinstance(jobs, list):
            return []
        return jobs

    def _save_jobs(self):
        if not self.plugin:
            return
        if not hasattr(self.plugin, "config") or self.plugin.config is None:
            self.plugin.config = {}
        self.plugin.config["jobs_json"] = json.dumps(self._jobs, ensure_ascii=False, indent=2)
        self.plugin.update_config(self.plugin.config)

    @staticmethod
    def _normalize_path(path: str) -> str:
        path = str(path or "").strip()
        if not path:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    @staticmethod
    def _parse_bool(value, default=True):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "启用")
        return bool(value)

    @staticmethod
    def _parse_int(value, default, label):
        try:
            v = int(value)
        except Exception:
            raise ValueError(f"{label}格式错误，必须为整数")
        return v

    @staticmethod
    def _validate_job(job: dict):
        if not job.get("name"):
            raise ValueError("作业名称不能为空")
        if not job.get("src_dir"):
            raise ValueError("源目录不能为空")
        if not job.get("dst_dir"):
            raise ValueError("目标目录不能为空")
        if job.get("src_dir") == job.get("dst_dir"):
            raise ValueError("源目录和目标目录不能相同")
        mode = job.get("sync_mode")
        if mode not in (0, 1, 2):
            raise ValueError("同步模式必须为 0(仅新增)、1(全同步) 或 2(移动)")
        try:
            interval = int(job.get("interval_minutes", 0))
            if interval < 1:
                raise ValueError
        except Exception:
            raise ValueError("同步间隔必须 >= 1 分钟")
        rules = job.get("exclude_rules")
        if rules is not None and not isinstance(rules, list):
            raise ValueError("排除规则必须为数组")

    def _generate_id(self):
        return f"job_{uuid.uuid4().hex[:12]}"

    def _now_str(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def list_jobs(self) -> list:
        with self.lock:
            self._jobs = self._load_jobs()
            return copy.deepcopy(self._jobs)

    def get_job(self, job_id: str) -> Optional[dict]:
        with self.lock:
            self._jobs = self._load_jobs()
            for j in self._jobs:
                if j.get("id") == job_id:
                    return copy.deepcopy(j)
            return None

    def create_job(self, payload: dict) -> dict:
        with self.lock:
            now = self._now_str()
            job = {
                "id": self._generate_id(),
                "name": str(payload.get("name", "")).strip(),
                "src_dir": self._normalize_path(payload.get("src_dir", "")),
                "dst_dir": self._normalize_path(payload.get("dst_dir", "")),
                "sync_mode": self._parse_int(payload.get("sync_mode", 0), 0, "同步模式"),
                "exclude_rules": payload.get("exclude_rules") or [],
                "interval_minutes": self._parse_int(payload.get("interval_minutes", 30), 30, "同步间隔"),
                "enabled": self._parse_bool(payload.get("enabled"), True),
                "last_run_at": None,
                "next_run_at": None,
                "last_task_id": None,
                "created_at": now,
                "updated_at": now,
            }
            self._validate_job(job)
            self._jobs = self._load_jobs()
            self._jobs.append(job)
            self._save_jobs()
            return copy.deepcopy(job)

    def update_job(self, job_id: str, payload: dict) -> Optional[dict]:
        with self.lock:
            self._jobs = self._load_jobs()
            for idx, j in enumerate(self._jobs):
                if j.get("id") == job_id:
                    updated = copy.deepcopy(j)
                    if "name" in payload:
                        updated["name"] = str(payload["name"]).strip()
                    if "src_dir" in payload:
                        updated["src_dir"] = self._normalize_path(payload["src_dir"])
                    if "dst_dir" in payload:
                        updated["dst_dir"] = self._normalize_path(payload["dst_dir"])
                    if "sync_mode" in payload:
                        updated["sync_mode"] = self._parse_int(payload["sync_mode"], 0, "同步模式")
                    if "exclude_rules" in payload:
                        updated["exclude_rules"] = payload["exclude_rules"] or []
                    if "interval_minutes" in payload:
                        updated["interval_minutes"] = self._parse_int(payload["interval_minutes"], 30, "同步间隔")
                    if "enabled" in payload:
                        updated["enabled"] = self._parse_bool(payload["enabled"])
                    updated["updated_at"] = self._now_str()
                    self._validate_job(updated)
                    self._jobs[idx] = updated
                    self._save_jobs()
                    return copy.deepcopy(updated)
            return None

    def delete_job(self, job_id: str) -> bool:
        with self.lock:
            self._jobs = self._load_jobs()
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.get("id") != job_id]
            if len(self._jobs) < before:
                self._save_jobs()
                return True
            return False

    def mark_job_run(self, job_id: str, task_id: str) -> bool:
        with self.lock:
            self._jobs = self._load_jobs()
            for j in self._jobs:
                if j.get("id") == job_id:
                    now = self._now_str()
                    try:
                        interval = int(j.get("interval_minutes", 30))
                    except Exception:
                        interval = 30
                    interval = max(1, interval)
                    j["last_run_at"] = now
                    j["last_task_id"] = task_id
                    j["updated_at"] = now
                    try:
                        dt = datetime.datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                        delta = datetime.timedelta(minutes=interval)
                        j["next_run_at"] = (dt + delta).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                    self._save_jobs()
                    return True
            return False
