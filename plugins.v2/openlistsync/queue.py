"""异步任务队列，事件触发后异步执行同步，支持重试。"""
import json
import queue
import threading
import time
import uuid
from typing import Optional, Callable


class SyncQueue:
    def __init__(self, engine, task_manager, job_manager, notify_callback=None, logger=None, max_retries=3):
        self._engine = engine
        self._task_manager = task_manager
        self._job_manager = job_manager
        self._notify_callback = notify_callback
        self._logger = logger
        self._max_retries = max_retries
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = None
        self._running = False
        self._active_keys = set()
        self._lock = threading.RLock()
        self._current_item = None

    def _log(self, msg, level="info"):
        if self._logger:
            getattr(self._logger, level, print)(msg)

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="openlist_sync_queue")
            self._thread.start()
            self._log("SyncQueue 已启动")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=10)
        self._log("SyncQueue 已停止")

    def _make_key(self, job, event_path, file_list_new):
        job_id = job["id"] if isinstance(job, dict) else str(job)
        files = ",".join(sorted(file_list_new or []))
        return f"{job_id}:{event_path}:{files}"

    def enqueue(self, job, event_path, file_list_new=None, trigger="transfer_complete") -> Optional[str]:
        with self._lock:
            if not self._running:
                self._log("SyncQueue 未运行，无法入队", "warning")
                return None

            key = self._make_key(job, event_path, file_list_new)
            if key in self._active_keys:
                self._log(f"重复事件跳过: {key}", "info")
                return None

            item = {
                "queue_id": f"q_{uuid.uuid4().hex[:12]}",
                "job": job,
                "event_path": event_path,
                "file_list_new": file_list_new or [],
                "trigger": trigger,
                "retry": 0,
                "max_retries": self._max_retries,
                "dedupe_key": key,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            self._active_keys.add(key)
            self._queue.put(item)
            self._log(f"入队: {item['queue_id']} path={event_path}")
            return item["queue_id"]

    def enqueue_full(self, job: dict, trigger: str = "manual") -> Optional[str]:
        """入队一个全量同步任务"""
        with self._lock:
            if not self._running:
                self._log("SyncQueue 未运行，无法入队", "warning")
                return None

            key = f"full:{job['id']}"
            if key in self._active_keys:
                self._log(f"全量同步已提交，跳过: {key}", "info")
                return None

            item = {
                "queue_id": f"q_{uuid.uuid4().hex[:12]}",
                "job": job,
                "event_path": "",
                "file_list_new": [],
                "trigger": trigger,
                "full_sync": True,
                "retry": 0,
                "max_retries": 1,
                "dedupe_key": key,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            self._active_keys.add(key)
            self._queue.put(item)
            self._log(f"全量同步入队: {item['queue_id']} job={job.get('name', job['id'])}")
            return item["queue_id"]

    def _retry_delay(self, retry):
        return min(30, 5 * retry)

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            self._current_item = item
            try:
                self._process_item(item)
            finally:
                self._queue.task_done()
                self._current_item = None

    def _process_item(self, item):
        job = item["job"]
        event_path = item["event_path"]
        file_list_new = item.get("file_list_new") or []
        trigger = item.get("trigger", "transfer_complete")

        task = self._task_manager.create_task(job, trigger)
        task_id = task["id"]
        self._task_manager.mark_running(task_id)

        try:
            if item.get("full_sync"):
                result = self._engine.execute(job)
            else:
                result = self._engine.sync_event(job, event_path, file_list_new)
            self._task_manager.mark_success(task_id, result)

            if item.get("trigger") != "manual":
                self._job_manager.mark_job_run(job["id"], task_id)

            if self._notify_callback:
                self._notify_callback(job, task_id, result=result)

            self._log(f"完成: {item['queue_id']} path={event_path} "
                      f"copied={result.get('summary', {}).get('copied', 0)} "
                      f"failed={result.get('summary', {}).get('failed', 0)}")
        except Exception as e:
            self._log(f"失败: {item['queue_id']} error={e}", "error")
            if item["retry"] < item["max_retries"]:
                item["retry"] += 1
                delay = self._retry_delay(item["retry"])
                self._log(f"重试 {item['retry']}/{item['max_retries']} 等待 {delay}s", "warning")
                # 重试时不释放 dedupe_key
                def _requeue():
                    time.sleep(delay)
                    self._queue.put(item)
                threading.Thread(target=_requeue, daemon=True).start()
                return  # 不释放 dedupe_key
            else:
                self._task_manager.mark_failed(task_id, str(e))
                if self._notify_callback:
                    self._notify_callback(job, task_id, error=str(e))

        # 只有最终完成或放弃重试才释放 dedupe_key
        self._active_keys.discard(item["dedupe_key"])

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "queue_size": self._queue.qsize(),
                "active_keys": len(self._active_keys),
                "current_item": self._current_item["queue_id"] if self._current_item else None,
            }

    def is_busy(self) -> bool:
        return self._current_item is not None
