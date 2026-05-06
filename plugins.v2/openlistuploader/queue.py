"""上传队列与 Worker — 后台单线程异步上传，支持重试与状态追踪"""

import json
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum

from app.log import logger

from .client import (
    OpenListClient,
    AuthError,
    RetryableError,
    NonRetryableError,
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class UploadTask:
    """上传任务"""

    task_id: str
    local_path: str
    remote_path: str
    size: int
    status: str = TaskStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    next_retry_at: str = ""
    source_event: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class UploadQueue:
    """单 Worker 上传队列

    - 事件回调只入队，不阻塞 MP
    - 后台单线程串行上传
    - 支持重试与指数退避
    """

    def __init__(
        self,
        client: OpenListClient,
        mapper=None,
        filters=None,
        state_file: Optional[Path] = None,
        max_retries: int = 3,
        retry_interval: int = 300,
        exponential_backoff: bool = True,
        skip_existing: bool = True,
        notify_callback=None,
    ):
        self._client = client
        self._mapper = mapper
        self._filters = filters
        self._state_file = state_file
        self._max_retries = max_retries
        self._retry_interval = retry_interval
        self._exponential_backoff = exponential_backoff
        self._skip_existing = skip_existing
        self._notify = notify_callback

        self._queue: List[UploadTask] = []
        self._queue_keys: set = set()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # 已处理任务去重缓存（task_id → timestamp）
        self._recent: Dict[str, float] = {}
        self._recent_ttl = 1800  # 30 分钟

    # ─── 队列操作 ───────────────────────────────────────────────

    def enqueue(self, task: UploadTask) -> bool:
        """添加任务到队列（去重）"""
        with self._lock:
            if task.task_id in self._queue_keys:
                logger.debug(f"任务已存在，跳过: {task.local_path}")
                return False

            if task.task_id in self._recent:
                logger.debug(f"近期已处理，跳过: {task.local_path}")
                return False

            self._queue.append(task)
            self._queue_keys.add(task.task_id)
            logger.info(f"入队: {task.local_path} → {task.remote_path}")
            return True

    def dequeue(self) -> Optional[UploadTask]:
        """取出下一个待处理任务"""
        with self._lock:
            for i, task in enumerate(self._queue):
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.UPLOADING
                    task.updated_at = datetime.now().isoformat()
                    return task
                # 检查是否到了重试时间
                if (
                    task.status == TaskStatus.FAILED
                    and task.retry_count < task.max_retries
                    and task.next_retry_at
                ):
                    try:
                        retry_time = datetime.fromisoformat(task.next_retry_at)
                        if datetime.now() >= retry_time:
                            task.status = TaskStatus.UPLOADING
                            task.updated_at = datetime.now().isoformat()
                            return task
                    except ValueError:
                        pass
            return None

    def update_status(
        self,
        task_id: str,
        status: str,
        error: str = "",
        retry: bool = False,
    ):
        """更新任务状态"""
        with self._lock:
            task = self._find(task_id)
            if not task:
                return

            task.status = status
            task.updated_at = datetime.now().isoformat()

            if status in (TaskStatus.SUCCESS, TaskStatus.SKIPPED):
                self._recent[task_id] = time.time()
                self._queue_keys.discard(task_id)

            if error:
                task.last_error = error

            if retry and status == TaskStatus.FAILED:
                task.retry_count += 1
                if task.retry_count < task.max_retries:
                    delay = self._retry_interval
                    if self._exponential_backoff:
                        delay *= 2 ** (task.retry_count - 1)
                    retry_time = datetime.now().timestamp() + delay
                    task.next_retry_at = datetime.fromtimestamp(
                        retry_time
                    ).isoformat()
                    logger.info(
                        f"任务 {task_id} 将在 {delay} 秒后重试 "
                        f"(第 {task.retry_count}/{task.max_retries} 次)"
                    )
                else:
                    logger.warning(
                        f"任务 {task_id} 已超过最大重试次数: {task.last_error}"
                    )

    def stats(self) -> dict:
        """队列统计"""
        with self._lock:
            total = len(self._queue)
            pending = sum(1 for t in self._queue if t.status == TaskStatus.PENDING)
            uploading = sum(1 for t in self._queue if t.status == TaskStatus.UPLOADING)
            success = sum(1 for t in self._queue if t.status == TaskStatus.SUCCESS)
            failed = sum(1 for t in self._queue if t.status == TaskStatus.FAILED)
            skipped = sum(1 for t in self._queue if t.status == TaskStatus.SKIPPED)
            return {
                "total": total,
                "pending": pending,
                "uploading": uploading,
                "success": success,
                "failed": failed,
                "skipped": skipped,
            }

    def get_tasks(
        self, status: Optional[str] = None, limit: int = 50
    ) -> List[dict]:
        """查询任务列表"""
        with self._lock:
            tasks = list(self._queue)
        if status:
            tasks = [t for t in tasks if t.status == status]
        return [t.to_dict() for t in tasks[-limit:]]

    # ─── Worker ─────────────────────────────────────────────────

    def start(self):
        """启动后台 Worker"""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="OpenListUpload-Worker", daemon=True
        )
        self._worker.start()
        logger.info("上传 Worker 已启动")

    def stop(self, timeout: int = 5):
        """停止 Worker"""
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        logger.info("上传 Worker 已停止")

    def _worker_loop(self):
        """Worker 主循环"""
        while not self._stop.is_set():
            try:
                task = self.dequeue()
                if not task:
                    self._stop.wait(2)
                    continue

                self._process_task(task)
            except Exception as e:
                logger.error(f"Worker 异常: {e}\n{traceback.format_exc()}")
                self._stop.wait(1)

    def _process_task(self, task: UploadTask):
        """处理单个上传任务"""
        local_path = Path(task.local_path)

        # 1. 检查本地文件
        if not local_path.is_file():
            self.update_status(
                task.task_id, TaskStatus.FAILED,
                error=f"文件不存在: {local_path}",
            )
            return

        # 2. 应用过滤器
        if self._filters and not self._filters.should_upload(local_path):
            self.update_status(
                task.task_id, TaskStatus.SKIPPED,
                error=f"被规则排除: {local_path.name}",
            )
            return

        # 3. 检查远端是否已存在
        if self._skip_existing:
            try:
                if self._client.exists(task.remote_path):
                    self.update_status(
                        task.task_id, TaskStatus.SKIPPED,
                        error=f"远端已存在: {task.remote_path}",
                    )
                    return
            except Exception as e:
                logger.warning(f"检查远端文件失败，继续上传: {e}")

        # 4. 确保远端目录存在
        remote_parent = str(Path(task.remote_path).parent).replace("\\", "/")
        try:
            self._client.mkdir(remote_parent)
        except Exception as e:
            logger.warning(f"创建远端目录失败，继续尝试上传: {e}")

        # 5. 执行上传
        try:
            self._client.upload_put(local_path, task.remote_path)
            self.update_status(task.task_id, TaskStatus.SUCCESS)
        except AuthError as e:
            self.update_status(
                task.task_id, TaskStatus.FAILED,
                error=f"认证失败: {e}",
            )
        except NonRetryableError as e:
            self.update_status(
                task.task_id, TaskStatus.FAILED,
                error=f"不可重试: {e}",
            )
        except RetryableError as e:
            self.update_status(
                task.task_id, TaskStatus.FAILED,
                error=str(e), retry=True,
            )
        except Exception as e:
            self.update_status(
                task.task_id, TaskStatus.FAILED,
                error=str(e), retry=True,
            )

    # ─── 内部 ───────────────────────────────────────────────────

    def _find(self, task_id: str) -> Optional[UploadTask]:
        for t in self._queue:
            if t.task_id == task_id:
                return t
        return None
