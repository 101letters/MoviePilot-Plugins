"""Background scheduler — timer-based job scanning."""
import threading
import traceback
from datetime import datetime
from typing import Optional, Callable

from app.log import logger


class Scheduler:
    """Background thread that periodically scans enabled jobs and executes them."""

    def __init__(
        self,
        interval_seconds: int = 60,
        job_manager=None,        # JobManager instance
        task_manager=None,       # TaskManager instance
        execute_fn: Optional[Callable] = None,   # (job, client) -> result
        notify_fn: Optional[Callable] = None,     # (job, result, error) -> None
        client_factory: Optional[Callable] = None,  # () -> OpenListClient
    ):
        self._interval = max(interval_seconds, 5)
        self._job_manager = job_manager
        self._task_manager = task_manager
        self._execute_fn = execute_fn
        self._notify_fn = notify_fn
        self._client_factory = client_factory
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background scheduler thread."""
        if self._thread and self._thread.is_alive():
            logger.info("Scheduler 已在运行，跳过启动")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="OpenListSyncScheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Scheduler 已启动，扫描间隔 {self._interval}s")

    def stop(self, timeout: float = 10.0) -> None:
        """Signal stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("Scheduler 已停止")

    def update_interval(self, seconds: int) -> None:
        self._interval = max(seconds, 5)

    # ------------------------------------------------------------------
    # run loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main loop: sleep, then scan jobs."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._interval)
            if self._stop_event.is_set():
                break
            self._scan_and_run()

    def _scan_and_run(self) -> None:
        """Scan all enabled jobs; execute those due."""
        if self._job_manager is None:
            return

        try:
            jobs = self._job_manager.get_enabled_jobs()
        except Exception as e:
            logger.error(f"Scheduler 获取作业列表失败: {e}")
            return

        now = datetime.now()

        for job in jobs:
            if self._stop_event.is_set():
                break

            job_id = job.get("id", "")
            next_run_str = job.get("next_run_at")

            # Parse next_run_at
            if next_run_str:
                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except (ValueError, TypeError):
                    next_run = None
            else:
                next_run = None

            # If no next_run_at, compute it now
            if next_run is None:
                self._job_manager.mark_run_complete(job_id)
                continue

            if now < next_run:
                continue  # not due yet

            # Check concurrency
            if self._task_manager and self._task_manager.has_running_task(job_id):
                logger.info(f"Scheduler 跳过 job={job.get('name', job_id)}（正在运行）")
                continue

            # Execute
            self._execute_one(job)

    def _execute_one(self, job: dict) -> None:
        """Create a task record, execute, update records, notify."""
        job_id = job.get("id", "")
        job_name = job.get("name", job_id)

        # Create task
        task = None
        if self._task_manager:
            task = self._task_manager.create_task(job, trigger="schedule")
            self._task_manager.mark_running(task["id"])

        # Build client
        client = None
        if self._client_factory:
            try:
                client = self._client_factory()
            except Exception as e:
                logger.error(f"Scheduler 创建 OpenListClient 失败: {e}")
                if task and self._task_manager:
                    self._task_manager.mark_failed(task["id"], str(e), task.get("started_at", ""))
                self._notify(job, None, f"创建客户端失败: {e}")
                return

        if client is None:
            err = "Scheduler: client_factory 不可用"
            logger.error(err)
            if task and self._task_manager:
                self._task_manager.mark_failed(task["id"], err, task.get("started_at", ""))
            return

        # Execute
        result = None
        error = None
        try:
            result = self._execute_fn(job, client)
        except Exception as e:
            error = str(e)
            logger.error(f"Scheduler 执行 job={job_name} 失败: {e}")
            logger.debug(traceback.format_exc())

        # Mark task
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
        self._notify(job, result, error)

    def _notify(self, job: dict, result: Optional[dict], error: Optional[str]) -> None:
        """Call user-provided notify callback."""
        if self._notify_fn:
            try:
                self._notify_fn(job, result, error)
            except Exception as e:
                logger.error(f"通知回调失败: {e}")
