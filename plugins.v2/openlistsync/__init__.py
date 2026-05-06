"""MoviePilot V2 插件：OpenListSync — OpenList/AList 自动化定时同步。

Provides three sync modes:
- 0 仅新增
- 1 全同步（镜像）
- 2 移动
"""
import json
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

from .client import OpenListClient, OpenListError
from .engine import execute_job
from .job_manager import JobManager
from .task_manager import TaskManager
from .scheduler import Scheduler


class OpenListSync(_PluginBase):
    # ------------------------------------------------------------------
    # plugin metadata
    # ------------------------------------------------------------------
    plugin_name = "OpenList同步"
    plugin_desc = "定时自动同步 OpenList/AList 目录（仅新增/全同步/移动三种模式）"
    plugin_icon = "sync.png"
    plugin_version = "0.1.0"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "openlistsync_"
    plugin_order = 31
    auth_level = 1

    # ------------------------------------------------------------------
    # config defaults
    # ------------------------------------------------------------------
    _enabled = False
    _openlist_url = ""
    _openlist_token = ""
    _notify = True
    _global_interval_seconds = 60
    _jobs_json = "[]"
    _max_task_history = 100

    # ------------------------------------------------------------------
    # runtime state
    # ------------------------------------------------------------------
    _client: Optional[OpenListClient] = None
    _job_manager: Optional[JobManager] = None
    _task_manager: Optional[TaskManager] = None
    _scheduler: Optional[Scheduler] = None
    _routes_registered = False

    # ------------------------------------------------------------------
    # config form
    # ------------------------------------------------------------------

    @staticmethod
    def get_form() -> List[dict]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "cols": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VTextField", "props": {"model": "global_interval_seconds", "label": "全局扫描间隔(秒)", "type": "number", "min": 5}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "cols": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8},
                             "content": [{"component": "VTextField", "props": {"model": "openlist_url", "label": "OpenList 地址", "placeholder": "http://192.168.1.100:5244"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VTextField", "props": {"model": "max_task_history", "label": "任务记录上限", "type": "number", "min": 10}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "cols": [
                            {"component": "VCol", "props": {"cols": 12},
                             "content": [{"component": "VTextField", "props": {"model": "openlist_token", "label": "OpenList Token", "type": "password"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "cols": [
                            {"component": "VCol", "props": {"cols": 12},
                             "content": [{"component": "VTextarea", "props": {"model": "jobs_json", "label": "作业配置 (JSON)", "rows": 10, "placeholder": "[]"}}]},
                        ],
                    },
                ],
            }
        ]

    def get_page(self) -> List[dict]:
        """Extra management page rendered in the plugin detail panel."""
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "API 路径前缀: /api/v1/plugin/OpenListSync/"
                                },
                            }
                        ],
                    }
                ],
            }
        ]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def init_plugin(self, config: dict = None) -> None:
        if config:
            self._enabled = self._to_bool(config.get("enabled", False))
            self._openlist_url = (config.get("openlist_url") or "").strip()
            self._openlist_token = (config.get("openlist_token") or "").strip()
            self._notify = self._to_bool(config.get("notify", True))
            try:
                self._global_interval_seconds = int(config.get("global_interval_seconds", 60) or 60)
            except (ValueError, TypeError):
                self._global_interval_seconds = 60
            self._jobs_json = config.get("jobs_json") or "[]"
            try:
                self._max_task_history = int(config.get("max_task_history", 100) or 100)
            except (ValueError, TypeError):
                self._max_task_history = 100

        # Register API routes (once)
        if not self._routes_registered:
            self._register_routes()
            self._routes_registered = True

        # Stop old scheduler if running
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None

        # Init managers
        self._job_manager = JobManager(save_callback=self._on_jobs_changed)
        self._job_manager.load(self._jobs_json)
        self._job_manager.compute_next_runs()

        self._task_manager = TaskManager(max_history=self._max_task_history)

        # Init client
        if self._openlist_url and self._openlist_token:
            self._client = OpenListClient(
                base_url=self._openlist_url,
                token=self._openlist_token,
                timeout=60,
            )
        else:
            self._client = None

        # Start scheduler if enabled
        if self._enabled:
            self._start_scheduler()

        logger.info(
            f"OpenListSync 初始化完成: enabled={self._enabled}, "
            f"url={self._openlist_url}, jobs={len(self._job_manager.list_jobs())}"
        )

    def stop_service(self) -> None:
        """Called by MP when plugin is disabled or service stops."""
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None
        logger.info("OpenListSync 服务已停止")

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _start_scheduler(self) -> None:
        """Create and start a new scheduler instance."""
        if self._scheduler:
            self._scheduler.stop()
        self._scheduler = Scheduler(
            interval_seconds=self._global_interval_seconds,
            job_manager=self._job_manager,
            task_manager=self._task_manager,
            execute_fn=self._execute_with_client,
            notify_fn=self._on_task_done,
            client_factory=self._make_client,
        )
        self._scheduler.start()

    def _make_client(self) -> OpenListClient:
        """Factory to create an OpenListClient from current config."""
        if not self._openlist_url:
            raise OpenListError("OpenList 地址未配置")
        if not self._openlist_token:
            raise OpenListError("OpenList Token 未配置")
        return OpenListClient(
            base_url=self._openlist_url,
            token=self._openlist_token,
            timeout=60,
        )

    def _execute_with_client(self, job: dict, client: OpenListClient) -> dict:
        """Wrapper so the scheduler doesn't depend on plugin internals."""
        return execute_job(job, client)

    def _on_jobs_changed(self, jobs: List[dict]) -> None:
        """Callback from JobManager: persist jobs_json and update config."""
        new_json = json.dumps(jobs, ensure_ascii=False, indent=2)
        self._jobs_json = new_json
        try:
            self.update_config({"jobs_json": new_json})
        except Exception as e:
            logger.error(f"保存作业配置失败: {e}")

    def _on_task_done(self, job: dict, result: Optional[dict], error: Optional[str]) -> None:
        """Send MP notification after a task completes."""
        if not self._notify:
            return

        job_name = job.get("name", job.get("id", ""))
        mode_names = {0: "仅新增", 1: "全同步", 2: "移动"}
        mode = job.get("sync_mode", 0)
        mode_name = mode_names.get(mode, str(mode))

        if error:
            title = f"OpenListSync 同步失败: {job_name}"
            text = f"模式: {mode_name}\n错误: {error}"
        elif result:
            summary_parts = []
            for key, label in [("copied", "复制"), ("deleted", "删除"), ("moved", "移动"),
                               ("skipped", "跳过"), ("conflicts", "冲突"), ("failed", "失败")]:
                count = len(result.get(key, []))
                if count:
                    summary_parts.append(f"{label}: {count}")
            title = f"OpenListSync 同步完成: {job_name}"
            text = f"模式: {mode_name}\n" + "\n".join(summary_parts)
        else:
            title = f"OpenListSync: {job_name}"
            text = "无结果"

        # Send notification — note: we cannot call notification inside a thread
        # directly.  We schedule it via MP's event system if possible.
        try:
            self._send_notification(title, text)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    def _send_notification(self, title: str, text: str) -> None:
        """Send a system notification through MP."""
        try:
            self.systemmessage.put(
                title=title,
                text=text,
            )
        except Exception:
            # Fallback: use post_message
            self.post_message(
                title=title,
                text=text,
            )

    # ------------------------------------------------------------------
    # API routes
    # ------------------------------------------------------------------

    @property
    def api_prefix(self) -> str:
        return "/api/v1/plugin/OpenListSync"

    def _register_routes(self):
        """Register all API routes.  Called by MP framework."""

        @self.get_api(f"{self.api_prefix}/status")
        async def status(request):
            """GET: plugin status."""
            return self._ok({
                "enabled": self._enabled,
                "openlist_url": self._openlist_url,
                "notify": self._notify,
                "global_interval_seconds": self._global_interval_seconds,
                "max_task_history": self._max_task_history,
                "job_count": len(self._job_manager.list_jobs()) if self._job_manager else 0,
                "scheduler_running": self._scheduler is not None and self._scheduler._thread is not None and self._scheduler._thread.is_alive() if self._scheduler else False,
                "client_ready": self._client is not None,
            })

        @self.get_api(f"{self.api_prefix}/jobs")
        async def list_jobs(request):
            """GET: list all jobs."""
            if not self._job_manager:
                return self._err("插件未初始化")
            return self._ok(self._job_manager.list_jobs())

        @self.get_api(f"{self.api_prefix}/jobs/(?P<job_id>[^/]+)")
        async def get_job(request):
            """GET: get one job by id."""
            job_id = request.path_params.get("job_id", "")
            if not self._job_manager:
                return self._err("插件未初始化")
            job = self._job_manager.get_job(job_id)
            if job is None:
                return self._err(f"作业不存在: {job_id}")
            return self._ok(job)

        @self.post_api(f"{self.api_prefix}/jobs")
        async def create_job(request):
            """POST: create a new job."""
            if not self._job_manager:
                return self._err("插件未初始化")
            try:
                body = await request.json()
            except Exception:
                return self._err("请求体不是有效的 JSON")
            job = self._job_manager.add_job(body)
            # Restart scheduler to pick up changes
            if self._enabled:
                self._start_scheduler()
            return self._ok(job)

        @self.put_api(f"{self.api_prefix}/jobs/(?P<job_id>[^/]+)")
        async def update_job(request):
            """PUT: update an existing job."""
            job_id = request.path_params.get("job_id", "")
            if not self._job_manager:
                return self._err("插件未初始化")
            try:
                body = await request.json()
            except Exception:
                return self._err("请求体不是有效的 JSON")
            job = self._job_manager.update_job(job_id, body)
            if job is None:
                return self._err(f"作业不存在: {job_id}")
            if self._enabled:
                self._start_scheduler()
            return self._ok(job)

        @self.delete_api(f"{self.api_prefix}/jobs/(?P<job_id>[^/]+)")
        async def delete_job(request):
            """DELETE: remove a job."""
            job_id = request.path_params.get("job_id", "")
            if not self._job_manager:
                return self._err("插件未初始化")
            ok = self._job_manager.delete_job(job_id)
            if not ok:
                return self._err(f"作业不存在: {job_id}")
            if self._enabled:
                self._start_scheduler()
            return self._ok({"deleted": job_id})

        @self.post_api(f"{self.api_prefix}/jobs/(?P<job_id>[^/]+)/run")
        async def run_job(request):
            """POST: manually trigger a job."""
            job_id = request.path_params.get("job_id", "")
            if not self._job_manager:
                return self._err("插件未初始化")
            job = self._job_manager.get_job(job_id)
            if job is None:
                return self._err(f"作业不存在: {job_id}")

            # Concurrency guard
            if self._task_manager and self._task_manager.has_running_task(job_id):
                running = self._task_manager.get_running_task(job_id)
                return self._err(f"作业正在运行中: {running.get('id', '') if running else job_id}")

            # Create task
            task = None
            if self._task_manager:
                task = self._task_manager.create_task(job, trigger="manual")
                self._task_manager.mark_running(task["id"])

            # Build client
            try:
                client = self._make_client()
            except OpenListError as e:
                if task and self._task_manager:
                    self._task_manager.mark_failed(task["id"], str(e), task.get("started_at", ""))
                return self._err(str(e))

            # Execute
            result = None
            error = None
            try:
                result = execute_job(job, client)
            except Exception as e:
                error = str(e)
                logger.error(f"手动执行失败: {e}")
                logger.debug(traceback.format_exc())

            # Update task
            if task and self._task_manager:
                if error:
                    self._task_manager.mark_failed(task["id"], error, task.get("started_at", ""))
                else:
                    summary = {
                        "copied": len(result.get("copied", [])),
                        "deleted": len(result.get("deleted", [])),
                        "moved": len(result.get("moved", [])),
                        "skipped": len(result.get("skipped", [])),
                        "conflicts": len(result.get("conflicts", [])),
                        "failed": len(result.get("failed", [])),
                    }
                    detail = {
                        "copied": result.get("copied", []),
                        "deleted": result.get("deleted", []),
                        "moved": result.get("moved", []),
                        "skipped": result.get("skipped", []),
                        "conflicts": result.get("conflicts", []),
                        "failed": result.get("failed", []),
                    }
                    self._task_manager.mark_success(task["id"], summary, detail, task.get("started_at", ""))

            # Update job runtime
            if self._job_manager:
                self._job_manager.mark_run_complete(job_id)

            # Notify
            self._on_task_done(job, result, error)

            if error:
                return self._err(error)
            return self._ok({
                "task_id": task["id"] if task else None,
                "job_id": job_id,
                "result": {
                    "copied": len(result.get("copied", [])),
                    "deleted": len(result.get("deleted", [])),
                    "moved": len(result.get("moved", [])),
                    "skipped": len(result.get("skipped", [])),
                    "conflicts": len(result.get("conflicts", [])),
                    "failed": len(result.get("failed", [])),
                },
            })

        @self.get_api(f"{self.api_prefix}/tasks")
        async def list_tasks(request):
            """GET: list task records."""
            if not self._task_manager:
                return self._err("任务管理器未初始化")
            limit = int(request.query_params.get("limit", 20))
            job_id = request.query_params.get("job_id", "")
            status = request.query_params.get("status", "")
            tasks = self._task_manager.list_tasks(
                limit=limit, job_id=job_id or None, status=status or None
            )
            return self._ok(tasks)

        @self.get_api(f"{self.api_prefix}/tasks/(?P<task_id>[^/]+)")
        async def get_task(request):
            """GET: one task detail."""
            task_id = request.path_params.get("task_id", "")
            if not self._task_manager:
                return self._err("任务管理器未初始化")
            task = self._task_manager.get_task(task_id)
            if task is None:
                return self._err(f"任务不存在: {task_id}")
            return self._ok(task)

        @self.post_api(f"{self.api_prefix}/test_connection")
        async def test_connection(request):
            """POST: test OpenList connectivity."""
            url = self._openlist_url
            token = self._openlist_token

            # Allow overriding from request body
            try:
                body = await request.json()
            except Exception:
                body = {}
            if body.get("url"):
                url = body["url"]
            if body.get("token"):
                token = body["token"]

            if not url:
                return self._err("OpenList 地址未配置")
            if not token:
                return self._err("OpenList Token 未配置")

            try:
                client = OpenListClient(base_url=url, token=token, timeout=10)
                info = client.get("/")
                return self._ok({"connected": True, "info": info})
            except OpenListError as e:
                return self._err(f"连接失败: {e}")

    # ------------------------------------------------------------------
    # response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any) -> dict:
        return {"success": True, "data": data}

    @staticmethod
    def _err(message: str) -> dict:
        return {"success": False, "message": message}

    # ------------------------------------------------------------------
    # utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)
