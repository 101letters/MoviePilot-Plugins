"""Single worker queue for OpenList copy jobs."""
import queue
import threading
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Dict, Optional, Callable

from app.log import logger

from .client import AuthError, NonRetryableError, RetryableError


class TaskStatus(str, Enum):
    PENDING = "pending"
    COPYING = "copying"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CopyTask:
    task_id: str
    mp_path: str
    src_dir: str
    dst_dir: str
    name: str
    status: str = TaskStatus.PENDING.value
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def dst_path(self) -> str:
        return str(PurePosixPath(self.dst_dir) / self.name)


class CopyQueue:
    def __init__(self, client, max_retries: int = 3, retry_interval: int = 300,
                 skip_existing: bool = True, notify_callback: Optional[Callable] = None):
        self._client = client
        self._max_retries = max_retries or 3
        self._retry_interval = retry_interval or 300
        self._skip_existing = skip_existing
        self._notify_callback = notify_callback
        self._queue = queue.Queue()
        self._tasks: Dict[str, CopyTask] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = None

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="OpenListCopyWorker", daemon=True)
        self._worker.start()
        logger.info("OpenListCopy Worker 已启动")

    def stop(self, timeout: int = 10):
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        logger.info("OpenListCopy Worker 已停止")

    def enqueue(self, task: CopyTask) -> bool:
        with self._lock:
            old = self._tasks.get(task.task_id)
            if old and old.status in (TaskStatus.PENDING.value, TaskStatus.COPYING.value, TaskStatus.SUCCESS.value, TaskStatus.SKIPPED.value):
                logger.info(f"OpenListCopy 任务已存在，跳过入队: {task.name}")
                return False
            task.max_retries = self._max_retries
            self._tasks[task.task_id] = task
            self._queue.put(task)
            logger.info(f"OpenListCopy 任务入队: {task.src_dir}/{task.name} -> {task.dst_dir}")
            return True

    def stats(self) -> dict:
        with self._lock:
            counts = {s.value: 0 for s in TaskStatus}
            for task in self._tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
            return {"total": len(self._tasks), "queued": self._queue.qsize(), **counts}

    def recent_tasks(self, limit: int = 20):
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.updated_at, reverse=True)
            return [asdict(t) for t in tasks[:limit]]

    def retry_failed(self) -> int:
        count = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status == TaskStatus.FAILED.value:
                    task.status = TaskStatus.PENDING.value
                    task.retry_count = 0
                    task.last_error = ""
                    task.updated_at = datetime.now().isoformat()
                    self._queue.put(task)
                    count += 1
        return count

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                try:
                    task = self._queue.get(timeout=2)
                except queue.Empty:
                    continue
                self._process(task)
                self._queue.task_done()
            except Exception as e:
                logger.error(f"OpenListCopy Worker 异常: {e}\n{traceback.format_exc()}")
                self._stop.wait(1)

    def _set_status(self, task: CopyTask, status: TaskStatus, error: str = ""):
        with self._lock:
            task.status = status.value
            task.last_error = error or ""
            task.updated_at = datetime.now().isoformat()
            self._tasks[task.task_id] = task

    def _retry_later(self, task: CopyTask, error: str):
        task.retry_count += 1
        if task.retry_count <= task.max_retries:
            delay = self._retry_interval * (2 ** (task.retry_count - 1))
            logger.warning(f"OpenListCopy 任务失败，{delay}s 后重试({task.retry_count}/{task.max_retries}): {task.name}, {error}")
            self._set_status(task, TaskStatus.PENDING, error)

            def delayed_put():
                if not self._stop.wait(delay):
                    self._queue.put(task)

            threading.Thread(target=delayed_put, daemon=True).start()
        else:
            self._set_status(task, TaskStatus.FAILED, error)
            if self._notify_callback:
                self._notify_callback(f"OpenListCopy 复制失败: {task.name}\n{error}")

    def _process(self, task: CopyTask):
        self._set_status(task, TaskStatus.COPYING)
        try:
            if self._skip_existing and self._client.exists(task.dst_path):
                self._set_status(task, TaskStatus.SKIPPED, f"目标已存在: {task.dst_path}")
                logger.info(f"OpenListCopy 跳过，目标已存在: {task.dst_path}")
                return
            self._client.copy(task.src_dir, task.dst_dir, [task.name])
            self._set_status(task, TaskStatus.SUCCESS)
            logger.info(f"OpenListCopy 复制成功: {task.name}")
        except AuthError as e:
            self._set_status(task, TaskStatus.FAILED, f"认证失败: {e}")
        except NonRetryableError as e:
            self._set_status(task, TaskStatus.FAILED, f"不可重试: {e}")
        except RetryableError as e:
            self._retry_later(task, str(e))
        except Exception as e:
            self._retry_later(task, str(e))
