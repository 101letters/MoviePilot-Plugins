"""MoviePilot V2 plugin: OpenListSync 主入口。"""
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.plugin import _PluginBase
except Exception:
    from app.plugins import _PluginBase
from app.log import logger
try:
    from app.core.event import Event, eventmanager
    from app.schemas.types import EventType
except Exception:
    Event = None
    eventmanager = None
    EventType = None

from .client import OpenListClient
from .task_manager import TaskManager
from .job_manager import JobManager
from .engine import SyncEngine
from .queue import SyncQueue


class OpenListSync(_PluginBase):
    plugin_name = "OpenListSync"
    plugin_desc = "基于 OpenList API 的文件夹同步插件，支持多种同步模式和定时调度。"
    plugin_version = "0.2.0"
    plugin_author = "101letters"
    plugin_icon = "sync.png"
    plugin_config_prefix = "openlistsync_"
    plugin_order = 31
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._client = None
        self._engine = None
        self._task_manager = None
        self._job_manager = None
        self._queue = None
        self._base_url = ""
        self._token = ""
        self._data_dir = ""
        self._notify_enabled = True
        self.config = {}
        self._lock = threading.RLock()

    def init_plugin(self, config: Optional[dict] = None):
        return self.init_service(config)

    def init_service(self, config: Optional[dict] = None):
        with self._lock:
            self.stop_service()

            self.config = dict(config or {})
            enabled = bool(self.config.get("enabled", False))
            openlist_url = str(self.config.get("openlist_url", "") or self.config.get("base_url", "") or "").strip().rstrip("/")
            openlist_token = str(self.config.get("openlist_token", "") or self.config.get("token", "") or "").strip()
            self._notify_enabled = bool(self.config.get("notify", True))
            try:
                max_task_history = int(self.config.get("max_task_history", 100))
            except Exception:
                max_task_history = 100
            data_dir = str(self.config.get("data_dir", "") or "").strip()

            self._base_url = openlist_url
            self._token = openlist_token

            if not data_dir:
                try:
                    data_dir = os.path.join(self.get_data_path(), "openlistsync")
                except Exception:
                    data_dir = os.path.join(os.getcwd(), "openlistsync")
            os.makedirs(data_dir, exist_ok=True)
            self._data_dir = data_dir

            tasks_file = os.path.join(data_dir, "tasks.json")

            self._task_manager = TaskManager(tasks_file=tasks_file, max_history=max_task_history)
            self._job_manager = JobManager(plugin=self)

            if openlist_url and openlist_token:
                self._client = OpenListClient(base_url=openlist_url, token=openlist_token)
                self._engine = SyncEngine(self._client)
                self._queue = SyncQueue(
                    engine=self._engine,
                    task_manager=self._task_manager,
                    job_manager=self._job_manager,
                    notify_callback=self._on_sync_result,
                    logger=logger,
                )

                if enabled:
                    self._queue.start()

                logger.info(f"OpenListSync 插件初始化完成，数据目录: {data_dir}")
            else:
                logger.warning("OpenListSync: openlist_url 或 openlist_token 为空，调度器未启动")

    def stop_service(self):
        if self._queue:
            self._queue.stop()
        self._queue = None
        self._engine = None
        self._client = None
        self._task_manager = None
        self._job_manager = None
        logger.info("OpenListSync 插件已停止")

    def get_state(self) -> bool:
        return bool(self._queue and self._queue.get_status().get("running"))

    def get_form(self) -> List[dict]:
        return [
            {"component": "switch", "name": "enabled", "id": "enabled", "label": "启用插件", "default": False},
            {"component": "input", "name": "openlist_url", "id": "openlist_url", "label": "OpenList 服务地址", "placeholder": "http://192.168.1.100:9090", "required": True},
            {"component": "input", "name": "openlist_token", "id": "openlist_token", "label": "API Token", "placeholder": "请输入 OpenList API Token", "input_type": "password", "required": True},
            {"component": "switch", "name": "notify", "id": "notify", "label": "启用通知", "default": True},
            {"component": "input-number", "name": "max_task_history", "id": "max_task_history", "label": "最大任务记录数", "placeholder": "100", "required": True, "default": 100},
            {"component": "input", "name": "data_dir", "id": "data_dir", "label": "数据目录（留空使用默认）", "required": False},
            {"component": "textarea", "name": "jobs_json", "id": "jobs_json", "label": "作业配置（JSON）", "required": False, "rows": 10},
        ]

    def get_page(self) -> Optional[dict]:
        return None

    def get_api(self) -> List[dict]:
        return [
            {"path": "/jobs", "endpoint": self.api_list_jobs, "methods": ["GET"]},
            {"path": "/jobs/{job_id}", "endpoint": self.api_get_job, "methods": ["GET"]},
            {"path": "/jobs", "endpoint": self.api_create_job, "methods": ["POST"]},
            {"path": "/jobs/{job_id}", "endpoint": self.api_update_job, "methods": ["PUT"]},
            {"path": "/jobs/{job_id}", "endpoint": self.api_delete_job, "methods": ["DELETE"]},
            {"path": "/jobs/{job_id}/run", "endpoint": self.api_run_job, "methods": ["POST"]},
            {"path": "/jobs/{job_id}/sync", "endpoint": self.api_sync_job, "methods": ["POST"]},
            {"path": "/tasks", "endpoint": self.api_list_tasks, "methods": ["GET"]},
            {"path": "/tasks/{task_id}", "endpoint": self.api_get_task, "methods": ["GET"]},
            {"path": "/test_connection", "endpoint": self.api_test_connection, "methods": ["POST"]},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"]},
        ]

    # === 事件监听 ===

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self.config.get("enabled") or not self._queue:
            return
        try:
            event_data = getattr(event, "event_data", None) or getattr(event, "data", None) or {}
            info = self._parse_transfer_event(event_data)
            if not info.get("target_path"):
                return

            matched = self._match_jobs_by_path(info["target_path"])
            for job in matched:
                self._queue.enqueue(
                    job=job,
                    event_path=info["target_path"],
                    file_list_new=info.get("file_list_new"),
                    trigger="transfer_complete",
                )
        except Exception as e:
            logger.error(f"OpenListSync 处理 TransferComplete 事件异常: {e}")

    def _parse_transfer_event(self, event_data: dict) -> dict:
        def _get(obj, key, default=None):
            if obj is None: return default
            if isinstance(obj, dict): return obj.get(key, default)
            return getattr(obj, key, default)

        transferinfo = _get(event_data, "transferinfo")
        target_diritem = _get(transferinfo, "target_diritem")
        target_path = _get(target_diritem, "path", "")

        file_list_new = _get(transferinfo, "file_list_new", []) or []
        if isinstance(file_list_new, str):
            file_list_new = [file_list_new]

        return {"target_path": str(target_path or "").strip(), "file_list_new": file_list_new}

    def _match_jobs_by_path(self, event_path: str) -> list:
        if not self._job_manager:
            return []
        jobs = self._job_manager.list_jobs()
        matched = []
        ep = event_path.rstrip("/")
        for job in jobs:
            if not job.get("enabled"):
                continue
            src_dir = job.get("src_dir", "").rstrip("/")
            if not src_dir:
                continue
            if ep == src_dir or ep.startswith(src_dir + "/"):
                matched.append(job)
        return matched

    # === 通知 ===

    def _on_sync_result(self, job: dict, task_id: str, result: dict = None, error: str = None):
        if not self._notify_enabled:
            return
        job_name = job.get("name", "未知作业")
        if error:
            self._send_text(f"【同步失败】{job_name}\n错误：{error}\n任务：{task_id}")
        else:
            summary = (result or {}).get("summary", {})
            copied = summary.get("copied", 0)
            failed = summary.get("failed", 0)
            deleted = summary.get("deleted", 0)
            conflicts = summary.get("conflicts", 0)
            self._send_text(f"【同步完成】{job_name}\n新增/复制：{copied} 删除：{deleted} 冲突：{conflicts} 失败：{failed}\n任务：{task_id}")

    def _send_text(self, msg: str):
        """发送 MP 系统通知"""
        for name in ("send_text", "post_message", "send_notification"):
            fn = getattr(self, name, None)
            if callable(fn):
                try:
                    fn(msg, title="OpenListSync")
                    return
                except Exception as e:
                    logger.warning(f"通知方法 {name} 失败: {e}")
        logger.info(f"通知: {msg}")

    # === API 实现 ===

    def api_list_jobs(self) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        return {"success": True, "data": self._job_manager.list_jobs()}

    def api_get_job(self, job_id: str) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        job = self._job_manager.get_job(job_id)
        if not job:
            return {"success": False, "message": "作业不存在"}
        return {"success": True, "data": job}

    def api_create_job(self) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        payload = self._get_api_payload() or {}
        try:
            return {"success": True, "data": self._job_manager.create_job(payload)}
        except ValueError as e:
            return {"success": False, "message": str(e)}

    def api_update_job(self, job_id: str) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        payload = self._get_api_payload() or {}
        try:
            job = self._job_manager.update_job(job_id, payload)
            if not job:
                return {"success": False, "message": "作业不存在"}
            return {"success": True, "data": job}
        except ValueError as e:
            return {"success": False, "message": str(e)}

    def api_delete_job(self, job_id: str) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        ok = self._job_manager.delete_job(job_id)
        return {"success": ok, "message": "" if ok else "作业不存在"}

    def api_run_job(self, job_id: str) -> dict:
        if not self._queue:
            return {"success": False, "message": "队列未初始化"}
        payload = self._get_api_payload() or {}
        event_path = str(payload.get("event_path", "")).strip()
        if not event_path:
            return {"success": False, "message": "事件驱动模式下手动执行需要传 event_path"}
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        job = self._job_manager.get_job(job_id)
        if not job:
            return {"success": False, "message": "作业不存在"}
        file_list_new = payload.get("file_list_new") or []
        qid = self._queue.enqueue(job, event_path, file_list_new, trigger="manual")
        if qid:
            return {"success": True, "data": {"queue_id": qid}}
        return {"success": False, "message": "入队失败，可能重复"}

    def api_sync_job(self, job_id: str) -> dict:
        """立即全量同步一次"""
        if not self._queue:
            return {"success": False, "message": "队列未初始化"}
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        job = self._job_manager.get_job(job_id)
        if not job:
            return {"success": False, "message": "作业不存在"}
        if not job.get("enabled"):
            return {"success": False, "message": "作业未启用"}
        qid = self._queue.enqueue_full(job, trigger="manual_full")
        if qid:
            return {"success": True, "data": {"queue_id": qid}}
        return {"success": False, "message": "入队失败，可能已有同步任务执行中"}

    def api_list_tasks(self, limit: int = 20, job_id: str = None, status: str = None) -> dict:
        if not self._task_manager:
            return {"success": False, "message": "插件未初始化"}
        try:
            limit = int(limit)
        except Exception:
            limit = 20
        return {"success": True, "data": self._task_manager.list_tasks(limit=limit, job_id=job_id, status=status)}

    def api_get_task(self, task_id: str) -> dict:
        if not self._task_manager:
            return {"success": False, "message": "插件未初始化"}
        task = self._task_manager.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        return {"success": True, "data": task}

    def api_test_connection(self) -> dict:
        try:
            payload = self._get_api_payload() or {}
            base_url = str(payload.get("openlist_url") or self._base_url or "").strip().rstrip("/")
            token = str(payload.get("openlist_token") or self._token or "").strip()
            if not base_url or not token:
                return {"success": False, "message": "OpenList 地址或 Token 未配置"}
            client = OpenListClient(base_url=base_url, token=token)
            ok, message = client.test_connection()
            return {"success": bool(ok), "message": message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def api_status(self) -> dict:
        enabled = bool(self.config.get("enabled"))
        configured = bool(self._base_url and self._token)
        if not self._queue:
            return {"success": True, "data": {"enabled": enabled, "openlist_configured": configured, "queue": None}}
        status = self._queue.get_status()
        status["enabled"] = enabled
        status["openlist_configured"] = configured
        return {"success": True, "data": status}

    # === 内部工具 ===

    def _get_api_payload(self) -> dict:
        for name in ("get_api_data", "api_data", "parse_body"):
            fn = getattr(self, name, None)
            if callable(fn):
                try:
                    data = fn()
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        return {}
