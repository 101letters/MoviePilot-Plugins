"""cloud_sync.py — 本地媒体文件上传到云端（AList）

核心组件：
- AlistClient：AList HTTP 客户端封装，支持 fs/put 流式上传、fs/list 目录列表。
- CloudSync：同步引擎，waiting/doing/finish 三列表 + 并发 worker。

实现要点：
- 增量判定：list 远端目录一次建 name→size dict，仅缺失才上传。
- As-Task: true 让上传变为可轮询进度的 AList 任务。
- 并发上限可配，默认 3。
"""
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger


# AList 任务状态码（与 taosync 一致）
TASK_SUCCEEDED = 2
TASK_CANCELED = 4
TASK_ERRORED = 5
TASK_FAILING = 6
TASK_FAILED = 7
TASK_SKIPPED = 8
TASK_SUCCESS_STATES = (TASK_SUCCEEDED, TASK_SKIPPED)
TASK_FAILED_STATES = (TASK_CANCELED, TASK_ERRORED, TASK_FAILING, TASK_FAILED)
TASK_TERMINAL = (TASK_SUCCEEDED, TASK_CANCELED, TASK_ERRORED, TASK_FAILING, TASK_FAILED)


def _convert_bytes(val: float) -> str:
    if not val:
        return "0 B"
    unit_list = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while i < len(unit_list):
        i += 1
        if val < 1024 ** (i + 1):
            return f"{val / (1024 ** i):.2f} {unit_list[i]}"
    return f"{val / (1024 ** (i - 1)):.2f} {unit_list[i - 1]}"


def _convert_seconds(seconds: float) -> Tuple[int, int, int]:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return hours, minutes, secs


class AlistError(Exception):
    """AList API 错误。"""


class AlistAlreadyExists(AlistError):
    """AList 上传目标已存在。"""


def _is_already_exists_message(message: Any) -> bool:
    text = str(message or "").lower()
    return any(token in text for token in ("exist", "already", "存在", "已存在"))


class AlistClient:
    """AList HTTP 客户端。

    鉴权：裸 Authorization: <token>（无 Bearer 前缀）。
    """

    def __init__(self, url: str, token: str, timeout: Tuple[int, int] = (60, 300)):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.user: Optional[str] = None
        if self.url and self.token:
            self.verify()

    def verify(self) -> str:
        data = self._req("GET", "/api/me")
        self.user = (data or {}).get("username")
        return self.user or ""

    # ---- 通用请求 ----
    def _req(self, method: str, path: str, data: Any = None, params: Any = None) -> Any:
        headers = {"Authorization": self.token} if self.token else None
        try:
            r = requests.request(
                method, self.url + path,
                json=data, params=params, headers=headers, timeout=self.timeout,
            )
        except requests.exceptions.InvalidURL:
            raise AlistError("AList 地址不正确")
        except requests.exceptions.ConnectionError:
            raise AlistError("AList 连接失败")
        except Exception as e:
            raise AlistError(f"AList 请求异常: {e}")

        if r.status_code != 200:
            raise AlistError(f"AList HTTP {r.status_code}")

        try:
            res = r.json()
        except Exception:
            raise AlistError("AList 响应非 JSON")

        if res.get("code") != 200:
            code = res.get("code")
            message = res.get("message") or ""
            if code == 401:
                raise AlistError("AList 未授权（token 错误）")
            raise AlistError(f"AList 错误 [{code}]: {message}")
        return res.get("data")

    def post(self, path: str, data: Any = None, params: Any = None) -> Any:
        return self._req("POST", path, data=data, params=params)

    def get(self, path: str, params: Any = None) -> Any:
        return self._req("GET", path, params=params)

    # ---- 文件系统操作 ----
    def list_dir(self, path: str, refresh: bool = True) -> Dict[str, Any]:
        data = self.post("/api/fs/list", data={
            "path": path, "refresh": refresh,
            "page": 1, "per_page": -1,
        })
        content = (data or {}).get("content") or []
        result: Dict[str, Any] = {}
        for item in content:
            name = item.get("name")
            if not name:
                continue
            if item.get("is_dir"):
                result[f"{name}/"] = {}
            else:
                result[name] = item.get("size", 0)
        return result

    def mkdir(self, path: str) -> None:
        try:
            self.post("/api/fs/mkdir", data={"path": path})
        except AlistError as e:
            msg = str(e)
            if "exist" in msg.lower() or "500" in msg:
                logger.debug(f"mkdir 忽略已存在目录: {path} ({msg})")
                return
            raise

    def fs_get(self, path: str) -> Dict[str, Any]:
        return self.post("/api/fs/get", data={"path": path}) or {}

    def remove_file(self, path: str) -> bool:
        remote = Path(path)
        parent = str(remote.parent)
        if parent == ".":
            parent = "/"
        name = remote.name
        if not name:
            raise AlistError("AList 删除路径无效")
        try:
            self.post("/api/fs/remove", data={"dir": parent, "names": [name]})
            return True
        except AlistError as e:
            msg = str(e).lower()
            if any(token in msg for token in ("not exist", "not found", "不存在", "404")):
                logger.info(f"【云同步】远端文件不存在，视为已删除: {path}")
                return False
            raise

    # ---- 上传 ----
    def put_stream(self, local_path: str, remote_path: str, as_task: bool = True) -> Optional[str]:
        from urllib.parse import quote
        size = os.path.getsize(local_path)
        headers = {
            "Authorization": self.token,
            "File-Path": quote(remote_path, safe="/"),
            "Content-Length": str(size),
            "Overwrite": "false",
        }
        if as_task:
            headers["As-Task"] = "true"

        with open(local_path, "rb") as f:
            r = requests.put(self.url + "/api/fs/put", data=f, headers=headers, timeout=self.timeout)

        if r.status_code != 200:
            if _is_already_exists_message(getattr(r, "text", "")):
                raise AlistAlreadyExists(f"AList 上传目标已存在: {remote_path}")
            raise AlistError(f"AList 上传 HTTP {r.status_code}: {r.text[:200]}")
        try:
            res = r.json()
        except Exception:
            raise AlistError("AList 上传响应非 JSON")
        if res.get("code") != 200:
            message = str(res.get("message") or "")
            if _is_already_exists_message(message):
                raise AlistAlreadyExists(f"AList 上传目标已存在: {remote_path}")
            raise AlistError(f"AList 上传错误 [{res.get('code')}]: {message}")
        data = res.get("data") or {}
        if as_task and isinstance(data, dict):
            task = data.get("task") or {}
            tid = task.get("id")
            if tid is None and isinstance(task, list) and task:
                tid = task[0].get("id")
            return str(tid) if tid is not None else None
        return None

    # ---- 上传任务轮询 ----
    def upload_task_info(self, tid: str) -> Dict[str, Any]:
        data = self.post("/api/admin/task/upload/info", params={"tid": tid})
        if not isinstance(data, dict):
            return {"state": None, "progress": None, "error": "响应格式异常"}
        return {"state": data.get("state"), "progress": data.get("progress"), "error": data.get("error")}

    def upload_task_cancel(self, tid: str) -> None:
        try:
            self.post("/api/admin/task/upload/cancel", params={"tid": tid})
        except Exception as e:
            logger.debug(f"取消上传任务 {tid} 异常: {e}")

    def upload_task_delete(self, tid: str) -> None:
        try:
            self.post("/api/admin/task/upload/delete", params={"tid": tid})
        except Exception as e:
            logger.debug(f"删除上传任务 {tid} 异常: {e}")


class _SyncItem:
    """单个待同步文件。"""

    def __init__(self, local_path: str, remote_path: str, file_size: int,
                 mediainfo: Any = None, meta: Any = None,
                 log_detail: bool = True):
        self.local_path = local_path
        self.remote_path = remote_path
        self.file_size = file_size
        self.mediainfo = mediainfo
        self.meta = meta
        self.log_detail = log_detail
        self.alist_task_id: Optional[str] = None
        self.status: int = 0  # 0-等待 1-进行中 2-成功 7-失败
        self.progress: Optional[float] = None
        self.err_msg: Optional[str] = None
        self.create_time: float = time.time()

    @property
    def remote_parent(self) -> str:
        return str(Path(self.remote_path).parent)

    @property
    def remote_name(self) -> str:
        return Path(self.remote_path).name


class CloudSync:
    """同步引擎：waiting/doing/finish 三列表 + 并发 worker。"""

    def __init__(self, plugin: Any, alist_client: Optional[AlistClient],
                 sync_mode: str = "copy", concurrency: int = 3):
        self.plugin = plugin
        self.alist = alist_client
        self.sync_mode = sync_mode
        self.concurrency = max(1, int(concurrency or 3))

        self.waiting: List[_SyncItem] = []
        self.doing: Dict[int, _SyncItem] = {}
        self.finish: List[_SyncItem] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue_num = 0
        self.break_flag = False
        self.scan_finish = False
        self.first_sync: Optional[float] = None
        self.create_time = time.time()
        self.batch_id = ""
        self.batch_label = ""
        self._failed_samples: List[Dict[str, Any]] = []
        self._last_watching = 0.0
        self._worker_thread: Optional[threading.Thread] = None
        self._started = False

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._started:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._worker_thread = threading.Thread(target=self._dispatch, daemon=True, name="CloudSyncDispatch")
            self._worker_thread.start()
            return
        self._started = True
        self.break_flag = False
        self.scan_finish = False
        self._worker_thread = threading.Thread(target=self._dispatch, daemon=True, name="CloudSyncDispatch")
        self._worker_thread.start()

    def prepare_batch(self, label: str = "") -> None:
        with self._cond:
            self.create_time = time.time()
            self.batch_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.create_time))
            self.batch_label = label or "云同步"
            self.scan_finish = False
            self.finish = []
            self._failed_samples = []
            self.break_flag = False
            self._cond.notify_all()
        logger.info(
            "【云同步】批次开始：id=%s，任务=%s，并发=%d",
            self.batch_id, self.batch_label, self.concurrency,
        )
        self.start()

    def stop(self) -> None:
        self.break_flag = True
        with self._cond:
            self._cond.notify_all()
        with self._lock:
            doing_items = list(self.doing.values())
        for item in doing_items:
            if item.alist_task_id:
                if self.alist:
                    self.alist.upload_task_cancel(item.alist_task_id)
            item.status = 4
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
        self._started = False

    # ---- 入队 ----
    def enqueue_file(self, local_path: str, remote_path: str,
                     mediainfo: Any = None, meta: Any = None,
                     log_detail: bool = True) -> None:
        if not os.path.exists(local_path):
            logger.warning(f"【云同步】本地文件不存在，跳过: {local_path}")
            return
        size = os.path.getsize(local_path)
        item = _SyncItem(local_path, remote_path, size, mediainfo, meta, log_detail=log_detail)
        with self._cond:
            self.waiting.append(item)
            self._cond.notify()
        message = f"【云同步】入队: {local_path} -> {remote_path} ({_convert_bytes(size)})"
        if log_detail:
            logger.info(message)
        else:
            logger.debug(message)
        self.start()  # 无论是否已启动，确保分发线程存活

    def mark_scan_finish(self) -> None:
        with self._cond:
            self.scan_finish = True
            self._cond.notify_all()

    # ---- 分发循环 ----
    def _dispatch(self) -> None:
        """分发循环：持续运行直到 break_flag=True（stop() 时设置）。

        不按 scan_finish 退出，避免跨批次时 waiting 队列有文件但线程已死。
        """
        while not self.break_flag:
            time.sleep(0.5)
            with self._lock:
                doing_nums = len(self.doing)
                waiting_nums = len(self.waiting)

            while doing_nums < self.concurrency and not self.break_flag:
                with self._cond:
                    if not self.waiting:
                        break
                    self._queue_num += 1
                    key = self._queue_num
                    item = self.waiting.pop(0)
                    item.doing_key = key
                    self.doing[key] = item
                t = threading.Thread(target=self._process_item, args=(item,), daemon=True)
                t.start()
                with self._lock:
                    doing_nums = len(self.doing)
                    waiting_nums = len(self.waiting)

        # break_flag 时排水：等进行中任务完成（最多 6s）
        drain_tries = 0
        while True:
            with self._lock:
                if not self.doing:
                    break
            drain_tries += 1
            if drain_tries > 12:
                break
            time.sleep(0.5)

        self._on_batch_finish()

    # ---- 单文件处理 ----
    def _process_item(self, item: _SyncItem) -> None:
        try:
            if self.break_flag:
                item.status = 4
            else:
                size_mb = (item.file_size or 0) / 1024 / 1024
                self._log_item_info(
                    item,
                    f"【云同步】开始上传: {Path(item.local_path).name} ({size_mb:.1f} MB) -> {item.remote_path}",
                )
                self._do_upload(item)
        except Exception as e:
            item.status = 7
            item.err_msg = str(e)
            logger.error(f"【云同步】上传失败: {item.local_path} -> {item.remote_path}: {e}")
        finally:
            self._finish_item(item)

    def _do_upload(self, item: _SyncItem) -> None:
        if not self.alist:
            raise AlistError("AList 客户端未初始化")
        parent = item.remote_parent
        if parent and parent != "/" and parent != ".":
            self.alist.mkdir(parent)
        tid = None
        last_err = None
        for attempt in range(3):
            try:
                tid = self.alist.put_stream(item.local_path, item.remote_path, as_task=True)
                break
            except AlistAlreadyExists:
                self._log_item_info(item, f"【云同步】远端已存在，跳过上传: {item.remote_path}")
                item.status = TASK_SKIPPED
                return
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, AlistError) as e:
                last_err = e
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"【云同步】上传重试 {attempt + 1}/3: {item.remote_path} ({e}), {wait}s 后重试")
                    time.sleep(wait)
                else:
                    raise
        item.alist_task_id = tid
        if tid is None:
            item.status = TASK_SUCCEEDED
            self._log_item_info(item, f"【云同步】上传完成: {Path(item.local_path).name} -> {item.remote_path}")
            return
        self._poll_task(item)
        if item.status == TASK_SUCCEEDED:
            self._log_item_info(item, f"【云同步】上传完成: {Path(item.local_path).name} -> {item.remote_path}")

    @staticmethod
    def _log_item_info(item: _SyncItem, message: str) -> None:
        if getattr(item, "log_detail", True):
            logger.info(message)
        else:
            logger.debug(message)

    def _poll_task(self, item: _SyncItem) -> None:
        while not self.break_flag:
            now = time.time()
            interval = 0.61 if (now - self._last_watching < 3) else 2.0
            time.sleep(interval)
            if self.break_flag:
                item.status = TASK_CANCELED
                if item.alist_task_id and self.alist:
                    self.alist.upload_task_cancel(item.alist_task_id)
                break
            try:
                info = self.alist.upload_task_info(item.alist_task_id) if self.alist else {}
            except Exception as e:
                msg = str(e)
                if "404" in msg:
                    msg = "任务可能已被删除"
                info = {"state": TASK_FAILED, "progress": None, "error": msg}

            state = info.get("state")
            progress = info.get("progress")
            error = info.get("error") or None
            if state in (TASK_ERRORED, TASK_FAILING, TASK_FAILED) and _is_already_exists_message(error):
                item.status = TASK_SKIPPED
                item.err_msg = None
                if item.alist_task_id and self.alist:
                    self.alist.upload_task_delete(item.alist_task_id)
                break
            if state == item.status and progress == item.progress:
                continue
            item.status = state if state is not None else item.status
            item.progress = progress
            item.err_msg = error
            if state in TASK_TERMINAL:
                if item.alist_task_id and self.alist:
                    self.alist.upload_task_delete(item.alist_task_id)
                break

    def _finish_item(self, item: _SyncItem) -> None:
        with self._lock:
            self.doing.pop(getattr(item, "doing_key", -1), None)
            self.finish.append(item)
            if item.status not in TASK_SUCCESS_STATES and len(self._failed_samples) < 10:
                self._failed_samples.append({
                    "local": item.local_path,
                    "remote": item.remote_path,
                    "status": item.status,
                    "error": item.err_msg,
                })

    # ---- 批次结束 ----
    def _on_batch_finish(self) -> None:
        if not self.plugin or not getattr(self.plugin, "_notify_enabled", False):
            return
        try:
            success = [it for it in self.finish if it.status in TASK_SUCCESS_STATES]
            failed = [it for it in self.finish if it.status in TASK_FAILED_STATES]
            total_size = sum(it.file_size for it in success if it.file_size)
            duration = time.time() - self.create_time
            hours, minutes, seconds = _convert_seconds(duration)
            duration_text = f"{hours}时{minutes}分{seconds}秒"
            if not self.finish:
                return
            title = "云盘上传助手"
            if failed and success:
                text = (f"同步部分完成：成功 {len(success)} 个，失败 {len(failed)} 个，"
                        f"共 {_convert_bytes(total_size)}，用时 {duration_text}")
            elif failed and not success:
                text = f"同步失败：{len(failed)} 个文件失败，用时 {duration_text}"
            else:
                text = (f"同步完成：成功 {len(success)} 个文件，"
                        f"共 {_convert_bytes(total_size)}，用时 {duration_text}")
            if self.plugin and hasattr(self.plugin, "_notify"):
                self.plugin._notify(title, text)
        except Exception as e:
            logger.debug(f"【云同步】批次结束通知异常: {e}")

    def wait_for_batch(self, timeout: Optional[float] = None,
                       progress_label: str = "",
                       progress_interval: float = 30.0) -> List[_SyncItem]:
        start = time.time()
        last_progress_log = 0.0
        while True:
            with self._lock:
                done = self.scan_finish and not self.waiting and not self.doing
                finish = list(self.finish)
            now = time.time()
            if progress_interval and (now - last_progress_log >= progress_interval or done):
                self._log_batch_progress(progress_label=progress_label, finished=finish, done=done)
                last_progress_log = now
            if done:
                return finish
            if timeout is not None and time.time() - start >= timeout:
                self._log_batch_progress(progress_label=progress_label, finished=finish, done=False)
                return finish
            time.sleep(0.5)

    def _log_batch_progress(self, progress_label: str = "",
                            finished: Optional[List[_SyncItem]] = None,
                            done: bool = False) -> None:
        with self._lock:
            waiting = list(self.waiting)
            doing = list(self.doing.values())
            finish = list(finished if finished is not None else self.finish)
        success = [it for it in finish if it.status in TASK_SUCCESS_STATES]
        failed = [it for it in finish if it.status not in TASK_SUCCESS_STATES]
        running_names = ", ".join(Path(it.local_path).name for it in doing[:3])
        if len(doing) > 3:
            running_names += f" 等 {len(doing)} 个"
        label = progress_label or self.batch_label or "云同步"
        stage = "完成" if done else "进度"
        logger.info(
            "【云同步】批次%s：id=%s，任务=%s，等待 %d，上传中 %d，成功/跳过 %d，失败 %d，"
            "待传 %s，上传中 %s，已用 %ds%s",
            stage, self.batch_id or "-", label,
            len(waiting), len(doing), len(success), len(failed),
            _convert_bytes(sum(it.file_size for it in waiting if it.file_size)),
            _convert_bytes(sum(it.file_size for it in doing if it.file_size)),
            int(time.time() - self.create_time),
            f"，当前: {running_names}" if running_names else "",
        )

    # ---- 状态快照 ----
    def get_status(self) -> Dict[str, Any]:
        self._last_watching = time.time()
        with self._lock:
            current = {
                "waiting": list(self.waiting),
                "doing": list(self.doing.values()),
                "finish": list(self.finish),
            }

        def _bucket(items: List[_SyncItem], state: int) -> List[_SyncItem]:
            return [it for it in items if it.status == state]

        result = {
            "batch": {"id": self.batch_id, "label": self.batch_label},
            "scanFinish": self.scan_finish,
            "createTime": int(self.create_time),
            "duration": int(time.time() - self.create_time),
            "firstSync": int(self.first_sync) if self.first_sync else None,
            "num": {
                "waiting": len(current["waiting"]),
                "running": len(current["doing"]),
                "success": len(_bucket(current["finish"], TASK_SUCCEEDED)) + len(_bucket(current["finish"], TASK_SKIPPED)),
                "fail": sum(len(_bucket(current["finish"], state)) for state in TASK_FAILED_STATES),
            },
            "size": {
                "waiting": sum(it.file_size for it in current["waiting"] if it.file_size),
                "running": sum(it.file_size for it in current["doing"] if it.file_size),
                "success": sum(it.file_size for it in current["finish"] if it.status in TASK_SUCCESS_STATES and it.file_size),
                "fail": sum(it.file_size for it in current["finish"] if it.status in TASK_FAILED_STATES and it.file_size),
            },
            "items": [
                {"local": it.local_path, "remote": it.remote_path, "size": it.file_size,
                 "status": it.status, "progress": it.progress, "error": it.err_msg}
                for it in (current["doing"] + current["waiting"])[-20:]
            ],
            "failedSamples": list(self._failed_samples),
        }
        return result

    # ---- 增量判定 ----
    def need_upload(self, local_path: str, remote_path: str) -> bool:
        if not self.alist:
            return True
        remote_name = Path(remote_path).name
        remote_parent = str(Path(remote_path).parent)
        if remote_parent in ("", ".", "/"):
            remote_parent = "/"
        try:
            listing = self.alist.list_dir(remote_parent, refresh=True)
        except AlistError as e:
            logger.debug(f"【云同步】增量判定 list 远端失败，按需上传: {remote_parent} ({e})")
            return True
        if remote_name not in listing:
            return True
        remote_size = listing[remote_name]
        if not isinstance(remote_size, int):
            return True
        return False

    def preload_remote_dirs(self, remote_roots: List[str]) -> Dict[str, Dict[str, Any]]:
        cache: Dict[str, Dict[str, Any]] = {}
        if not self.alist:
            return cache
        stats = {"dirs": 0, "failed": 0, "last_log": time.time(), "start": time.time()}
        for root in remote_roots:
            root = (root or "").strip().rstrip("/")
            if not root:
                continue
            self._preload_one(cache, root, stats)
        logger.info(
            "【云同步】远端目录预加载完成：目录 %d，失败 %d，用时 %ds",
            stats["dirs"], stats["failed"],
            int(time.time() - stats["start"]),
        )
        return cache

    def _preload_one(self, cache: Dict[str, Dict[str, Any]], path: str,
                     stats: Optional[Dict[str, Any]] = None) -> None:
        if path in cache:
            return
        try:
            listing = self.alist.list_dir(path, refresh=False)
            cache[path] = listing
            if stats is not None:
                stats["dirs"] = int(stats.get("dirs") or 0) + 1
                now = time.time()
                if now - float(stats.get("last_log") or 0) >= 15:
                    logger.info(
                        "【云同步】远端目录预加载中：已缓存 %d 个目录，当前 %s",
                        stats["dirs"], path,
                    )
                    stats["last_log"] = now
        except AlistError as e:
            logger.debug(f"【云同步】预加载远端目录失败: {path} ({e})")
            cache[path] = {}
            if stats is not None:
                stats["failed"] = int(stats.get("failed") or 0) + 1
            return
        for name, val in list(listing.items()):
            if isinstance(val, dict) and name.endswith("/"):
                child = f"{path}/{name.rstrip('/')}"
                self._preload_one(cache, child, stats)

    def need_upload_cached(self, remote_path: str, cache: Dict[str, Dict[str, Any]]) -> bool:
        if not self.alist:
            return True
        remote_name = Path(remote_path).name
        remote_parent = str(Path(remote_path).parent)
        if remote_parent in ("", ".", "/"):
            remote_parent = "/"
        listing = cache.get(remote_parent)
        if listing is None:
            try:
                listing = self.alist.list_dir(remote_parent, refresh=False)
                cache[remote_parent] = listing
            except AlistError as e:
                logger.debug(f"【云同步】缓存未命中实时 list 失败，按需上传: {remote_parent} ({e})")
                return True
        if remote_name not in listing:
            return True
        remote_size = listing[remote_name]
        if not isinstance(remote_size, int):
            return True
        return False
