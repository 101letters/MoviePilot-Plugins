"""后台调度器：定期扫描作业并串行执行同步任务。"""
import threading
import time
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

try:
    from .engine import SyncEngine
    from .task_manager import TaskManager
    from .job_manager import JobManager
    from .client import OpenListClient
except ImportError:
    from engine import SyncEngine
    from task_manager import TaskManager
    from job_manager import JobManager
    from client import OpenListClient


class Scheduler:
    def __init__(self, client: OpenListClient, task_manager: TaskManager, job_manager: JobManager, logger=None):
        self.client = client
        self.task_manager = task_manager
        self.job_manager = job_manager
        self.logger = logger or logging.getLogger(__name__)
        self._engine = SyncEngine(client)
        self._running = False
        self._stopped = threading.Event()
        self._scan_interval = 60
        self._executor = None
        self._scan_thread = None
        self._current_future = None
        self._current_task_id = None
        self._running_jobs = set()
        self._lock = threading.RLock()

    def start(self):
        """启动后台扫描线程。"""
        with self._lock:
            if self._running:
                return
            if self._executor is None or getattr(self._executor, "_shutdown", False):
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openlist_sync")
            self._running = True
            self._stopped.clear()
            self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True, name="openlist_scan")
            self._scan_thread.start()
        self.logger.info("OpenListSync 调度器已启动")

    def stop(self):
        """停止调度器。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stopped.set()
            scan_thread = self._scan_thread
            self._scan_thread = None
            executor = self._executor
            self._executor = None

        if scan_thread and scan_thread.is_alive():
            scan_thread.join(timeout=30)

        if executor:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

        self.logger.info("OpenListSync 调度器已停止")

    def _scan_loop(self):
        """后台扫描循环。"""
        while not self._stopped.is_set():
            try:
                now = time.time()
                jobs = self.job_manager.list_jobs()
                for job in jobs:
                    if self._stopped.is_set():
                        break
                    if not job.get("enabled", False):
                        continue
                    if self._is_job_due(job, now):
                        self._submit_job(job)
            except Exception as e:
                self.logger.error(f"扫描循环出错: {e}")
            self._stopped.wait(self._scan_interval)

    def _is_job_due(self, job: dict, now: float) -> bool:
        """判断作业是否到执行时间。"""
        next_run_at = job.get("next_run_at")
        if not next_run_at:
            return True
        try:
            dt = datetime.datetime.strptime(next_run_at, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp() <= now + 5
        except Exception:
            return True

    def submit_job(self, job_id: str, trigger: str = "manual") -> Optional[str]:
        """手动提交一个作业，返回 task_id 或 None。"""
        with self._lock:
            job = self.job_manager.get_job(job_id)
            if not job:
                self.logger.warning(f"作业 {job_id} 不存在")
                return None
            return self._submit_job(job, trigger=trigger)

    def _submit_job(self, job: dict, trigger: str = "schedule") -> Optional[str]:
        """内部提交执行。"""
        with self._lock:
            if not self._running or self._stopped.is_set():
                return None
            if not self._executor:
                return None
            if self.is_busy():
                self.logger.info("已有同步任务执行中，跳过本次提交")
                return None

            job_id = job["id"]

            if job_id in self._running_jobs:
                self.logger.info(f"作业 {job.get('name', job_id)} 已提交或执行中，跳过")
                return None

            if self.task_manager.has_running_task(job_id):
                self.logger.info(f"作业 {job.get('name', job_id)} 已有运行中的任务，跳过")
                return None

            task = self.task_manager.create_task(job, trigger)
            task_id = task["id"]

            self._running_jobs.add(job_id)

            try:
                future = self._executor.submit(self._run_job, job, task_id, trigger)
            except Exception:
                self._running_jobs.discard(job_id)
                self.task_manager.mark_failed(task_id, "提交任务到执行器失败")
                raise

            self._current_future = future
            self._current_task_id = task_id
            self.job_manager.mark_job_run(job_id, task_id)

            return task_id

    def _run_job(self, job: dict, task_id: str, trigger: str):
        """实际执行同步逻辑，在 executor 线程中运行。"""
        job_id = job.get("id", "")
        self.task_manager.mark_running(task_id)

        try:
            result = self._engine.execute(job)

            self.task_manager.mark_success(task_id, result)
            self.logger.info(
                f"作业 {job.get('name', job_id)} 同步完成: "
                f"copied={result.get('summary', {}).get('copied', 0)}, "
                f"failed={result.get('summary', {}).get('failed', 0)}, "
                f"deleted={result.get('summary', {}).get('deleted', 0)}, "
                f"conflicts={result.get('summary', {}).get('conflicts', 0)}"
            )
        except Exception as e:
            self.task_manager.mark_failed(task_id, str(e))
            self.logger.error(f"作业 {job.get('name', job_id)} 同步失败: {e}", exc_info=True)
        finally:
            with self._lock:
                self._running_jobs.discard(job_id)
                if self._current_task_id == task_id:
                    self._current_task_id = None
                    self._current_future = None

    def is_busy(self) -> bool:
        """检查是否有任务正在执行。"""
        return self._current_future is not None and not self._current_future.done()

    def get_status(self) -> dict:
        """获取调度器状态。"""
        return {
            "running": self._running,
            "busy": self.is_busy(),
            "current_task_id": self._current_task_id if self.is_busy() else None,
        }
