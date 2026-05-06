"""MoviePilot V2 plugin: OpenListSync 主入口。"""
import os
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.plugin import _PluginBase
except Exception:  # pragma: no cover
    from app.plugins import _PluginBase
from app.log import logger
try:
    from app.schemas.plugin import PluginConfig
except Exception:  # pragma: no cover
    PluginConfig = None

from .client import OpenListClient
from .task_manager import TaskManager
from .job_manager import JobManager
from .scheduler import Scheduler


class OpenListSync(_PluginBase):
    plugin_name = "OpenListSync"
    plugin_desc = "基于 OpenList API 的文件夹同步插件，支持多种同步模式和定时调度。"
    plugin_version = "0.1.0"
    plugin_author = "101letters"
    plugin_icon = "sync.png"
    plugin_config_prefix = "openlistsync_"
    plugin_order = 31
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._client = None
        self._task_manager = None
        self._job_manager = None
        self._scheduler = None
        self._base_url = ""
        self._token = ""
        self._data_dir = ""
        self._notify_enabled = True
        self.config = {}
        self._lock = threading.RLock()

    def init_plugin(self, config: Optional[dict] = None):
        """兼容 MoviePilot V2 插件初始化入口。"""
        return self.init_service(config)

    def init_service(self, config: Optional[dict] = None):
        """MP 插件初始化入口。"""
        with self._lock:
            self.stop_service()
            self.config = dict(config or {})

            enabled = bool(self.config.get("enabled", False))
            openlist_url = str(self.config.get("openlist_url", "")).strip().rstrip("/")
            openlist_token = str(self.config.get("openlist_token", "")).strip()
            notify_enabled = bool(self.config.get("notify", True))
            try:
                global_interval_seconds = int(self.config.get("global_interval_seconds", 60))
            except Exception:
                global_interval_seconds = 60
            try:
                max_task_history = int(self.config.get("max_task_history", 100))
            except Exception:
                max_task_history = 100
            data_dir = str(self.config.get("data_dir", "") or "").strip()

            if not openlist_url:
                openlist_url = str(self.config.get("base_url", "")).strip().rstrip("/")
            if not openlist_token:
                openlist_token = str(self.config.get("token", "")).strip()
            if global_interval_seconds == 60 and self.config.get("global_interval"):
                try:
                    global_interval_seconds = int(self.config.get("global_interval"))
                except Exception:
                    pass

            self._notify_enabled = notify_enabled
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
                self._scheduler = Scheduler(
                    client=self._client,
                    task_manager=self._task_manager,
                    job_manager=self._job_manager,
                    logger=logger,
                )
                self._scheduler._scan_interval = max(5, global_interval_seconds)

                original_run_job = self._scheduler._run_job

                def notify_wrapper(job, task_id, trigger):
                    try:
                        original_run_job(job, task_id, trigger)
                    finally:
                        if self._notify_enabled and self._task_manager:
                            task = self._task_manager.get_task(task_id)
                            if task:
                                if task.get("status") == "success":
                                    self._notify_sync_result(
                                        job,
                                        task_id,
                                        result={"summary": task.get("summary", {})},
                                    )
                                elif task.get("status") == "failed":
                                    self._notify_sync_result(
                                        job,
                                        task_id,
                                        error=task.get("error") or "未知错误",
                                    )

                self._scheduler._run_job = notify_wrapper

                if enabled:
                    self._scheduler.start()
            else:
                logger.warning("OpenListSync: openlist_url 或 openlist_token 为空，调度器未启动")

            logger.info(f"OpenListSync 插件初始化完成，数据目录: {data_dir}")

    def stop_service(self):
        """插件停止入口。"""
        if self._scheduler:
            self._scheduler.stop()
        self._scheduler = None
        self._client = None
        self._task_manager = None
        self._job_manager = None
        logger.info("OpenListSync 插件已停止")

    def get_state(self) -> bool:
        return self._client is not None

    def get_form(self) -> List[dict]:
        return [
            {
                "component": "switch",
                "name": "enabled",
                "id": "enabled",
                "label": "启用插件",
                "default": False,
            },
            {
                "component": "input",
                "name": "openlist_url",
                "id": "openlist_url",
                "label": "OpenList 服务地址",
                "placeholder": "http://192.168.1.100:9090",
                "required": True,
            },
            {
                "component": "input",
                "name": "openlist_token",
                "id": "openlist_token",
                "label": "API Token",
                "placeholder": "请输入 OpenList API Token",
                "input_type": "password",
                "required": True,
            },
            {
                "component": "switch",
                "name": "notify",
                "id": "notify",
                "label": "启用通知",
                "default": True,
            },
            {
                "component": "input-number",
                "name": "global_interval_seconds",
                "id": "global_interval_seconds",
                "label": "全局扫描间隔（秒）",
                "placeholder": "60",
                "required": True,
                "default": 60,
            },
            {
                "component": "input-number",
                "name": "max_task_history",
                "id": "max_task_history",
                "label": "最大任务记录数",
                "placeholder": "100",
                "required": True,
                "default": 100,
            },
            {
                "component": "input",
                "name": "data_dir",
                "id": "data_dir",
                "label": "数据目录（留空使用默认）",
                "required": False,
            },
            {
                "component": "textarea",
                "name": "jobs_json",
                "id": "jobs_json",
                "label": "作业配置（JSON）",
                "required": False,
                "rows": 10,
            },
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
            {"path": "/tasks", "endpoint": self.api_list_tasks, "methods": ["GET"]},
            {"path": "/tasks/{task_id}", "endpoint": self.api_get_task, "methods": ["GET"]},
            {"path": "/test_connection", "endpoint": self.api_test_connection, "methods": ["POST"]},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"]},
        ]

    def api_list_jobs(self) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        jobs = self._job_manager.list_jobs()
        return {"success": True, "data": jobs}

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
        payload = self._get_api_payload()
        try:
            job = self._job_manager.create_job(payload)
            return {"success": True, "data": job}
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.error(f"OpenListSync 创建作业失败: {e}", exc_info=True)
            return {"success": False, "message": str(e)}

    def api_update_job(self, job_id: str) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        payload = self._get_api_payload()
        try:
            job = self._job_manager.update_job(job_id, payload)
            if not job:
                return {"success": False, "message": "作业不存在"}
            return {"success": True, "data": job}
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.error(f"OpenListSync 更新作业失败: {e}", exc_info=True)
            return {"success": False, "message": str(e)}

    def api_delete_job(self, job_id: str) -> dict:
        if not self._job_manager:
            return {"success": False, "message": "插件未初始化"}
        ok = self._job_manager.delete_job(job_id)
        if not ok:
            return {"success": False, "message": "作业不存在"}
        return {"success": True}

    def api_run_job(self, job_id: str) -> dict:
        if not self._scheduler:
            return {"success": False, "message": "调度器未初始化"}
        try:
            task_id = self._scheduler.submit_job(job_id, trigger="manual")
            if task_id:
                return {"success": True, "data": {"task_id": task_id}}
            return {"success": False, "message": "作业无法执行（可能已有任务运行中）"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def api_list_tasks(self, limit: int = 20, job_id: str = None, status: str = None) -> dict:
        if not self._task_manager:
            return {"success": False, "message": "插件未初始化"}
        tasks = self._task_manager.list_tasks(limit=limit, job_id=job_id, status=status)
        return {"success": True, "data": tasks}

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
        if not self._scheduler:
            return {"success": False, "message": "调度器未初始化"}
        status = self._scheduler.get_status()
        return {"success": True, "data": status}

    def _notify_sync_result(self, job: dict, task_id: str, result: dict = None, error: str = None):
        """发送同步结果通知。"""
        job_name = job.get("name", "未知作业") if isinstance(job, dict) else "未知作业"
        if error:
            msg = f"【同步失败】{job_name}\n错误：{error}"
        else:
            summary = (result or {}).get("summary", {})
            copied = summary.get("copied", 0)
            failed = summary.get("failed", 0)
            deleted = summary.get("deleted", 0)
            conflicts = summary.get("conflicts", 0)
            msg = f"【同步完成】{job_name}\n新增/复制：{copied}，删除：{deleted}，冲突：{conflicts}，失败：{failed}"
        self._send_text(msg)

    def _send_text(self, msg: str):
        """兼容不同 MoviePilot 通知方法。"""
        try:
            if hasattr(self, "send_text"):
                return self.send_text(msg)
            if hasattr(self, "post_message"):
                return self.post_message(title="OpenListSync", text=msg)
            if hasattr(self, "send_notification"):
                return self.send_notification(title="OpenListSync", text=msg)
        except Exception as e:
            logger.warning(f"OpenListSync 发送通知失败: {e}")
        logger.info(f"OpenListSync 通知: {msg}")

    def _get_api_payload(self) -> dict:
        """获取 API 请求体，兼容不同 MP V2 方法名。"""
        for name in ("get_api_data", "api_data", "parse_body"):
            func = getattr(self, name, None)
            if callable(func):
                try:
                    data = func()
                    if isinstance(data, dict):
                        return data
                except TypeError:
                    continue
                except Exception:
                    break
        return {}
