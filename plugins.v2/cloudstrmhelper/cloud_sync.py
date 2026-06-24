"""cloud_sync.py — 本地媒体文件复制到云端（AList）

实现要点（参考 taosync + AList API）：
- taosync 只做 AList↔AList copy，本地→云端必须用 AList 的 `PUT /api/fs/put`（流式上传）。
- `As-Task: true` 让上传变为可轮询进度的 AList 任务，轮询 `POST /api/admin/task/upload/info?tid=`（注意是 `/upload/` 任务组，非 `/copy/`）。
- 鉴权用裸 `Authorization` token（无 Bearer），`GET /api/me` 校验。
- 增量判定：list 远端目录一次建 name→size dict，仅缺失才上传，远端已存在永不覆盖。
- 队列/进度：照 taosync `JobTask` 三列表（waiting/doing/finish）简化，并发上限可配。
"""
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger


# AList 任务状态码（来自 tache，与 taosync 一致）
# 0=Pending,1=Running,2=Succeeded,3=Canceling,4=Canceled,5=Errored,6=Failing,7=Failed
TASK_SUCCEEDED = 2
TASK_CANCELED = 4
TASK_ERRORED = 5
TASK_FAILING = 6
TASK_FAILED = 7
TASK_SKIPPED = 8
TASK_TERMINAL = (TASK_SUCCEEDED, TASK_CANCELED, TASK_FAILED)


def _convert_bytes(val: float) -> str:
    """字节数转可读字符串（照 taosync commonUtils.convertBytes，修 0 边界）。"""
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
    """秒转 (时, 分, 秒)。"""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return hours, minutes, secs


class AlistError(Exception):
    """AList API 错误。"""


class AlistAlreadyExists(AlistError):
    """AList 上传目标已存在。"""


def _is_already_exists_message(message: Any) -> bool:
    """判断 AList 返回文本是否表示目标已存在。"""
    text = str(message or "").lower()
    return any(token in text for token in ("exist", "already", "存在", "已存在"))


class AlistClient:
    """AList HTTP 客户端。

    照 taosync `service/alist/alistClient.py` 改写，并新增本地文件上传 `put_stream`。
    鉴权：裸 `Authorization: <token>`（AList admin 静态 token 或用户 JWT，无 Bearer 前缀）。
    """

    def __init__(self, url: str, token: str, timeout: Tuple[int, int] = (60, 300)):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.user: Optional[str] = None
        # 仅在 token 存在时校验；未配置时延后到首次调用报错
        if self.url and self.token:
            self.verify()

    def verify(self) -> str:
        """调用 GET /api/me 校验 token 并取用户名。"""
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

        # AList 统一响应：{"code":200,"message":...,"data":...}
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
        """列出目录，返回 {name: size(文件) 或 {}(目录)} dict（照 taosync fileListApi）。

        refresh=True 绕过 AList 目录缓存。
        """
        data = self.post("/api/fs/list", data={
            "path": path,
            "refresh": refresh,
            "page": 1,
            "per_page": -1,  # -1 = 全部
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
        """创建目录（幂等：已存在时 AList 返回 500 内层码，忽略）。"""
        try:
            self.post("/api/fs/mkdir", data={"path": path})
        except AlistError as e:
            # 已存在目录 AList 返回 "path already exists" 之类，忽略
            msg = str(e)
            if "exist" in msg.lower() or "500" in msg:
                logger.debug(f"mkdir 忽略已存在目录: {path} ({msg})")
                return
            raise

    def fs_get(self, path: str) -> Dict[str, Any]:
        """获取单个文件/目录信息，含 raw_url / sign / size（用于构建直链）。"""
        return self.post("/api/fs/get", data={"path": path}) or {}

    def remove_file(self, path: str) -> bool:
        """删除远端单个文件。

        AList/OpenList 删除接口需要 dir + names。目标不存在时视为已删除，返回 False。
        """
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
        """流式上传本地文件到 AList 远端路径。

        PUT /api/fs/put，请求体即文件字节，目标路径/大小等由 header 传递。
        as_task=True → 返回 AList 任务 id（轮询 upload_task_info）；False → 直接上传，返回 None。
        返回：任务 id 或 None（直接上传成功）。
        """
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
            # 流式上传：用 data=文件对象，requests 自动按 chunk 发送
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
        # as_task 时 data 含 {"task": {...}}，否则 data 通常为 null/None
        if as_task and isinstance(data, dict):
            task = data.get("task") or {}
            tid = task.get("id")
            if tid is None and isinstance(task, list) and task:
                tid = task[0].get("id")
            return str(tid) if tid is not None else None
        return None

    # ---- 上传任务轮询 ----
    def upload_task_info(self, tid: str) -> Dict[str, Any]:
        """轮询上传任务状态。POST /api/admin/task/upload/info?tid=<id>（注意 /upload/ 组）。"""
        data = self.post("/api/admin/task/upload/info", params={"tid": tid})
        if not isinstance(data, dict):
            return {"state": None, "progress": None, "error": "响应格式异常"}
        return {
            "state": data.get("state"),
            "progress": data.get("progress"),
            "error": data.get("error"),
        }

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
    """单个待同步文件（照 taosync CopyItem 简化）。"""

    def __init__(self, local_path: str, remote_path: str, file_size: int,
                 mediainfo: Any = None, meta: Any = None):
        self.local_path = local_path
        self.remote_path = remote_path
        self.file_size = file_size
        self.mediainfo = mediainfo
        self.meta = meta
        # 运行态
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
    """同步引擎：waiting/doing/finish 三列表 + 并发 worker（照 taosync JobTask 简化）。

    本期用内存队列（重启即重跑，靠增量判定幂等）。后续可用 plugin.save_data 持久化任务表。
    """

    def __init__(self, plugin: Any, alist_client: Optional[AlistClient],
                 sync_mode: str = "copy", concurrency: int = 3):
        self.plugin = plugin
        self.alist = alist_client
        self.sync_mode = sync_mode  # 固定仅新增；保留字段用于状态兼容
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

    def prepare_batch(self) -> None:
        """开始新批次前重置批次状态并确保分发线程运行。"""
        with self._cond:
            self.create_time = time.time()
            self.scan_finish = False
            self.finish = []
            self.break_flag = False
            self._cond.notify_all()
        self.start()

    def stop(self) -> None:
        self.break_flag = True
        with self._cond:
            self._cond.notify_all()
        # 取消进行中的 AList 任务
        with self._lock:
            doing_items = list(self.doing.values())
        for item in doing_items:
            if item.alist_task_id:
                if self.alist:
                    self.alist.upload_task_cancel(item.alist_task_id)
            item.status = 4  # 已取消
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
        self._started = False

    # ---- 入队 ----
    def enqueue_file(self, local_path: str, remote_path: str,
                     mediainfo: Any = None, meta: Any = None) -> None:
        """入队单个文件（增量判定在外层完成；这里只排队）。"""
        if not os.path.exists(local_path):
            logger.warning(f"【云同步】本地文件不存在，跳过: {local_path}")
            return
        size = os.path.getsize(local_path)
        item = _SyncItem(local_path, remote_path, size, mediainfo, meta)
        with self._cond:
            self.waiting.append(item)
            self._cond.notify()
        logger.info(f"【云同步】入队: {local_path} -> {remote_path} ({_convert_bytes(size)})")

    def mark_scan_finish(self) -> None:
        with self._cond:
            self.scan_finish = True
            self._cond.notify_all()

    # ---- 分发循环 ----
    def _dispatch(self) -> None:
        while not self.break_flag:
            time.sleep(0.5)
            with self._lock:
                doing_nums = len(self.doing)
                waiting_nums = len(self.waiting)
            if not self.scan_finish or doing_nums != 0 or waiting_nums != 0:
                # 只要并发有空位且还有等待项就派发
                while doing_nums < self.concurrency:
                    if self.break_flag:
                        break
                    with self._cond:
                        if not self.waiting:
                            break
                        self._queue_num += 1
                        key = self._queue_num
                        item = self.waiting.pop(0)
                        item.doing_key = key
                        self.doing[key] = item
                    # 起独立线程处理单文件
                    t = threading.Thread(target=self._process_item, args=(item,), daemon=True)
                    t.start()
                    with self._lock:
                        doing_nums = len(self.doing)
                        waiting_nums = len(self.waiting)
            else:
                break

        # 等待进行中收尾（最多 6s）
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
                logger.info(f"【云同步】开始上传: {Path(item.local_path).name} ({size_mb:.1f} MB) -> {item.remote_path}")
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
        # 1. 建远端父目录（幂等）
        parent = item.remote_parent
        if parent and parent != "/" and parent != ".":
            self.alist.mkdir(parent)
        # 2. 上传（带 3 次指数退避重试，大文件需要）
        tid = None
        last_err = None
        for attempt in range(3):
            try:
                tid = self.alist.put_stream(item.local_path, item.remote_path, as_task=True)
                break
            except AlistAlreadyExists:
                logger.info(f"【云同步】远端已存在，跳过上传: {item.remote_path}")
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
        # 3. 直接上传（无任务 id）即视为成功
        if tid is None:
            item.status = TASK_SUCCEEDED
            logger.info(f"【云同步】上传完成: {Path(item.local_path).name} -> {item.remote_path}")
            return
        # 4. 轮询任务直到终态
        self._poll_task(item)
        if item.status == TASK_SUCCEEDED:
            logger.info(f"【云同步】上传完成: {Path(item.local_path).name} -> {item.remote_path}")

    def _poll_task(self, item: _SyncItem) -> None:
        while not self.break_flag:
            # 自适应轮询间隔：有 UI 查看（last_watching 3s 内）用 0.61s，否则 2s 省 API
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
            # 终态：删除 AList 任务记录避免堆积
            if state in TASK_TERMINAL:
                if item.alist_task_id and self.alist:
                    self.alist.upload_task_delete(item.alist_task_id)
                break

    def _finish_item(self, item: _SyncItem) -> None:
        # 移入 finish
        with self._lock:
            self.doing.pop(getattr(item, "doing_key", -1), None)
            self.finish.append(item)
    # ---- 批次结束 ----
    def _on_batch_finish(self) -> None:
        if not self.plugin or not getattr(self.plugin, "_notify_enabled", False):
            return
        try:
            success = [it for it in self.finish if it.status in (TASK_SUCCEEDED, TASK_SKIPPED)]
            failed = [it for it in self.finish if it.status in (TASK_FAILED, TASK_CANCELED)]
            total_size = sum(it.file_size for it in success if it.file_size)
            duration = time.time() - self.create_time
            hours, minutes, seconds = _convert_seconds(duration)
            duration_text = f"{hours}时{minutes}分{seconds}秒"
            if not self.finish:
                return
            title = "云端STRM整理助手"
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

    def wait_for_batch(self, timeout: Optional[float] = None) -> List[_SyncItem]:
        """等待当前批次完成并返回完成列表快照。"""
        start = time.time()
        while True:
            with self._lock:
                done = self.scan_finish and not self.waiting and not self.doing
                finish = list(self.finish)
            if done:
                return finish
            if timeout is not None and time.time() - start >= timeout:
                return finish
            time.sleep(0.5)

    # ---- 状态快照 ----
    def get_status(self) -> Dict[str, Any]:
        """照 taosync getCurrent() 返回进度快照。调用即刷新 last_watching（加快轮询）。"""
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
            "scanFinish": self.scan_finish,
            "createTime": int(self.create_time),
            "duration": int(time.time() - self.create_time),
            "firstSync": int(self.first_sync) if self.first_sync else None,
            "num": {
                "waiting": len(current["waiting"]),
                "running": len(current["doing"]),
                "success": len(_bucket(current["finish"], TASK_SUCCEEDED)) + len(_bucket(current["finish"], TASK_SKIPPED)),
                "fail": len(_bucket(current["finish"], TASK_FAILED))
                       + len(_bucket(current["finish"], TASK_CANCELED)),
            },
            "size": {
                "waiting": sum(it.file_size for it in current["waiting"] if it.file_size),
                "running": sum(it.file_size for it in current["doing"] if it.file_size),
                "success": sum(
                    it.file_size for it in current["finish"]
                    if it.status in (TASK_SUCCEEDED, TASK_SKIPPED) and it.file_size
                ),
                "fail": sum(it.file_size for it in current["finish"] if it.status in (TASK_FAILED, TASK_CANCELED) and it.file_size),
            },
            "items": [
                {
                    "local": it.local_path,
                    "remote": it.remote_path,
                    "size": it.file_size,
                    "status": it.status,
                    "progress": it.progress,
                    "error": it.err_msg,
                }
                for it in (current["doing"] + current["waiting"])[-20:]
            ],
        }
        return result

    # ---- 增量判定 ----
    def need_upload(self, local_path: str, remote_path: str) -> bool:
        """增量判定：仅远端缺失才上传。

        需求要求 never overwrite，因此只要远端存在同名文件就跳过，不按 size/hash 重传。
        """
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
            return True  # 是目录，同名冲突
        return False

    def preload_remote_dirs(self, remote_roots: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量预加载云端目录列表（refresh=False，用 AList 缓存），供扫描期快速判定。

        扫描成千上万文件时，逐文件 list_dir(refresh=True) 会非常慢
        （每个文件一次网络往返 + 云盘刷新）。预加载每个云端根目录一次，
        扫描期 need_upload_cached 只查内存，避免 N 次远端请求。
        """
        cache: Dict[str, Dict[str, Any]] = {}
        if not self.alist:
            return cache
        for root in remote_roots:
            root = (root or "").strip().rstrip("/")
            if not root:
                continue
            # 递归预加载：root 及其一层子目录（媒体文件通常在 Season/剧集 子目录）
            self._preload_one(cache, root)
        return cache

    def _preload_one(self, cache: Dict[str, Dict[str, Any]], path: str) -> None:
        if path in cache:
            return
        try:
            listing = self.alist.list_dir(path, refresh=False)
            cache[path] = listing
        except AlistError as e:
            logger.debug(f"【云同步】预加载远端目录失败: {path} ({e})")
            cache[path] = {}
            return
        # 递归一层子目录（影视库通常 根/类型/剧名/Season/文件）
        for name, val in list(listing.items()):
            if isinstance(val, dict) and name.endswith("/"):
                child = f"{path}/{name.rstrip('/')}"
                self._preload_one(cache, child)

    def need_upload_cached(self, remote_path: str, cache: Dict[str, Dict[str, Any]]) -> bool:
        """基于预加载缓存判定是否需要上传；缓存未命中时回退实时 list_dir。"""
        remote_name = Path(remote_path).name
        remote_parent = str(Path(remote_path).parent)
        if remote_parent in ("", ".", "/"):
            remote_parent = "/"
        listing = cache.get(remote_parent)
        if listing is None:
            # 缓存未命中（子目录层级深），回退实时查询（不强制刷新）
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
