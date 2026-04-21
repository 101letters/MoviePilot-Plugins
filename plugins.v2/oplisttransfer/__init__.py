import fnmatch
import json
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

try:
    import requests
except ImportError:
    requests = None


class OplistTransfer(_PluginBase):
    plugin_name = "OpenList 文件转运"
    plugin_desc = "监听整理完成事件，将文件传输任务排队后交给 OpenList 内部 copy 执行。"
    plugin_icon = "refresh2.png"
    plugin_version = "1.3.1"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "oplisttransfer_"
    plugin_order = 66
    auth_level = 1

    _enabled = True
    _notify = True
    _oplist_url = ""
    _token = ""
    _engine_name = ""
    _src_path = "/影视库/"
    _dst_path = "/123云盘/影视/"
    _job_name = "影视库"
    _method = 0
    _use_cache_t = 1
    _scan_interval_t = 1
    _use_cache_s = 0
    _scan_interval_s = 0
    _exclude_dirs = "下载文件/\n123网盘/\nXiuren/\n私人影视/"
    _exclude_files = "*.nfo\n*.jpg\n*.jpeg\n*.png\n*.txt\n*.torrent"
    _delay_seconds = 5
    _dispatch_interval = 3
    _interval_minutes = 360
    _enabled_types = ["movie", "tv"]
    _dedupe_ttl_seconds = 1800
    _max_queue_size = 1000
    _timeout = 60
    _poll_interval = 3
    _poll_max_times = 120
    _aggregate_enabled = True
    _aggregate_window = 10
    _aggregate_max_files = 20

    _copy_api_path = "/api/fs/copy"
    _mkdir_api_path = "/api/fs/mkdir"
    _list_api_path = "/api/fs/list"
    _task_info_api_path = "/api/admin/task/copy/info"
    _mp_relative_root = "/media"

    _queue_lock = threading.Lock()
    _worker_lock = threading.Lock()
    _queue: List[Dict[str, Any]] = []
    _queue_keys = set()
    _recent_tasks: Dict[str, float] = {}
    _aggregate_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    _aggregate_timers: Dict[str, threading.Timer] = {}
    _worker_thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()

    def init_plugin(self, config: dict = None):
        if requests is None:
            logger.error("OpenList 文件转运插件缺少 requests 依赖")
            return

        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._notify = bool(config.get("notify", True))
        self._oplist_url = (config.get("oplist_url") or "").rstrip("/")
        self._token = (config.get("token") or "").strip()
        self._engine_name = config.get("engine_name") or ""
        self._src_path = config.get("src_path") or "/影视库/"
        self._dst_path = config.get("dst_path") or "/123云盘/影视/"
        self._job_name = config.get("job_name") or "影视库"
        self._method = int(config.get("method", 0))
        self._use_cache_t = int(config.get("use_cache_t", 1))
        self._scan_interval_t = int(config.get("scan_interval_t", 1))
        self._use_cache_s = int(config.get("use_cache_s", 0))
        self._scan_interval_s = int(config.get("scan_interval_s", 0))
        self._exclude_dirs = config.get("exclude_dirs") or "下载文件/\n123网盘/\nXiuren/\n私人影视/"
        self._exclude_files = config.get("exclude_files") or "*.nfo\n*.jpg\n*.jpeg\n*.png\n*.txt\n*.torrent"
        self._delay_seconds = int(config.get("delay_seconds") or 5)
        self._dispatch_interval = int(config.get("dispatch_interval") or 3)
        self._interval_minutes = int(config.get("interval_minutes") or 360)
        enabled_types = config.get("enabled_types") or ["movie", "tv"]
        self._enabled_types = [str(x).lower() for x in enabled_types if x]
        self._dedupe_ttl_seconds = int(config.get("dedupe_ttl_seconds") or 1800)
        self._max_queue_size = int(config.get("max_queue_size") or 1000)
        self._timeout = int(config.get("timeout") or 60)
        self._poll_interval = int(config.get("poll_interval") or 3)
        self._poll_max_times = int(config.get("poll_max_times") or 120)
        self._aggregate_enabled = bool(config.get("aggregate_enabled", True))
        self._aggregate_window = int(config.get("aggregate_window") or 10)
        self._aggregate_max_files = int(config.get("aggregate_max_files") or 20)

        self._stop_event.clear()
        if self._enabled:
            self.__start_worker()
        else:
            self.stop_service()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "是否启用"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "开启通知"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "job_name", "label": "作业名称", "placeholder": "影视库"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{"component": "VTextField", "props": {"model": "oplist_url", "label": "引擎", "placeholder": "http://192.168.31.6:5244"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "engine_name", "label": "引擎备注", "placeholder": "可选"}}]}
                        ]
                    },
                    {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": {"model": "token", "label": "Token", "type": "password"}}]}]},
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "src_path", "label": "源目录", "placeholder": "/影视库/"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "dst_path", "label": "目标目录", "placeholder": "/123云盘/影视/"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": "method", "label": "同步方法", "items": [{"title": "仅新增", "value": 0}]}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": "use_cache_t", "label": "目标目录扫描缓存", "items": [{"title": "不使用", "value": 0}, {"title": "使用", "value": 1}]}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "scan_interval_t", "label": "目标目录操作间隔(秒)", "type": "number"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": "use_cache_s", "label": "源目录扫描缓存", "items": [{"title": "不使用", "value": 0}, {"title": "使用", "value": 1}]}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "scan_interval_s", "label": "源目录操作间隔(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "aggregate_enabled", "label": "启用目录聚合"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "props": {"show": "{{aggregate_enabled}}"},
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "aggregate_window", "label": "聚合窗口(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "aggregate_max_files", "label": "最大聚合文件数", "type": "number"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "delay_seconds", "label": "入队延迟(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "dispatch_interval", "label": "下发间隔(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": "enabled_types", "label": "处理媒体类型", "multiple": True, "chips": True, "items": [{"title": "电影", "value": "movie"}, {"title": "剧集", "value": "tv"}]}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": "exclude_dirs", "label": "不同步目录", "rows": 6, "placeholder": "下载文件/\n123网盘/\nXiuren/\n私人影视/"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": "exclude_files", "label": "不同步文件", "rows": 6, "placeholder": "*.nfo\n*.jpg\n*.jpeg\n*.png\n*.txt\n*.torrent"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "调用方式固定为事件触发。同步方法固定为仅新增：目标目录存在同名文件则跳过，不重复上传。不同步目录按路径前缀匹配，不同步文件支持精确名和通配符。"}}]}]
                    }
                ]
            }
        ], {
            "enabled": True,
            "notify": True,
            "oplist_url": "",
            "token": "",
            "engine_name": "",
            "src_path": "/影视库/",
            "dst_path": "/123云盘/影视/",
            "job_name": "影视库",
            "method": 0,
            "use_cache_t": 1,
            "scan_interval_t": 1,
            "use_cache_s": 0,
            "scan_interval_s": 0,
            "exclude_dirs": "下载文件/\n123网盘/\nXiuren/\n私人影视/",
            "exclude_files": "*.nfo\n*.jpg\n*.jpeg\n*.png\n*.txt\n*.torrent",
            "delay_seconds": 5,
            "dispatch_interval": 3,
            "interval_minutes": 360,
            "enabled_types": ["movie", "tv"],
            "aggregate_enabled": True,
            "aggregate_window": 10,
            "aggregate_max_files": 20,
        }

    def get_page(self) -> List[dict]:
        last = self.get_data("last_result") or {}
        queue_size = len(self._queue)
        agg_buckets = len(self._aggregate_buckets)
        lines = [f"当前队列长度: {queue_size}", f"聚合桶数量: {agg_buckets}"]
        for key in ["title", "source_path", "src_dir", "dst_dir", "name", "result", "message", "task_id", "time"]:
            value = last.get(key)
            if value not in [None, ""]:
                lines.append(f"{key}: {value}")
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "\n".join(lines)}}]

    def stop_service(self):
        self._stop_event.set()
        for timer in self._aggregate_timers.values():
            timer.cancel()
        self._aggregate_timers.clear()

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled or requests is None:
            return
        if not event or not event.event_data:
            return

        event_info: dict = event.event_data
        transferinfo = event_info.get("transferinfo")
        mediainfo = event_info.get("mediainfo")
        if not transferinfo:
            return

        source_path = getattr(transferinfo, "target_path", None) or event_info.get("dest")
        if not source_path:
            return

        media_type = str(getattr(mediainfo, "type", "") or "").lower()
        if media_type and self._enabled_types and media_type not in self._enabled_types:
            return

        relative_dir, name = self.__extract_relative_dir_and_name(source_path)
        if relative_dir is None or not name:
            logger.warning(f"OpenList 文件转运：无法从路径提取相对目录，跳过 {source_path}")
            return

        if self.__is_excluded_dir(relative_dir) or self.__is_excluded_file(name):
            logger.info(f"OpenList 文件转运：命中不同步规则，跳过 {source_path}")
            return

        src_dir = self.__join_path(self._src_path, relative_dir)
        dst_dir = self.__join_path(self._dst_path, relative_dir)
        dedupe_key = f"{src_dir}|{dst_dir}|{name}"
        title = getattr(mediainfo, "title_year", None) or getattr(mediainfo, "title", None) or name

        now = time.time()
        with self._queue_lock:
            self.__cleanup_recent_locked(now)
            if dedupe_key in self._queue_keys:
                return
            recent_at = self._recent_tasks.get(dedupe_key)
            if recent_at and now - recent_at < self._dedupe_ttl_seconds:
                return

            item = {
                "title": title,
                "source_path": source_path,
                "relative_dir": relative_dir,
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "name": name,
                "created_at": now,
                "not_before": now + self._delay_seconds,
                "dedupe_key": dedupe_key,
            }

            if self._aggregate_enabled:
                bucket_key = f"{src_dir}|{dst_dir}"
                bucket = self._aggregate_buckets[bucket_key]
                bucket.append(item)
                self._queue_keys.add(dedupe_key)
                self._recent_tasks[dedupe_key] = now

                if bucket_key not in self._aggregate_timers:
                    timer = threading.Timer(self._aggregate_window, self.__flush_bucket, args=[bucket_key])
                    timer.daemon = True
                    timer.start()
                    self._aggregate_timers[bucket_key] = timer

                if len(bucket) >= self._aggregate_max_files:
                    timer = self._aggregate_timers.pop(bucket_key, None)
                    if timer:
                        timer.cancel()
                    self.__flush_bucket(bucket_key)
                    return

                logger.info(f"OpenList 文件转运：文件加入聚合桶 {bucket_key}，当前 {len(bucket)} 个文件")
            else:
                self._queue.append(item)
                self._queue_keys.add(dedupe_key)
                self._recent_tasks[dedupe_key] = now
                logger.info(f"OpenList 文件转运：任务入队 {src_dir} -> {dst_dir} / {name}")

        self.__start_worker()

    def __flush_bucket(self, bucket_key: str):
        with self._queue_lock:
            items = self._aggregate_buckets.pop(bucket_key, [])
            timer = self._aggregate_timers.pop(bucket_key, None)
            if timer:
                timer.cancel()
            if not items:
                return

            names = sorted({item["name"] for item in items})
            queue_item = {
                "title": items[0].get("title") or names[0],
                "source_path": items[0].get("source_path"),
                "relative_dir": items[0].get("relative_dir"),
                "src_dir": items[0].get("src_dir"),
                "dst_dir": items[0].get("dst_dir"),
                "name": ", ".join(names[:3]) + (" ..." if len(names) > 3 else ""),
                "names": names,
                "created_at": min(item["created_at"] for item in items),
                "not_before": max(item["not_before"] for item in items),
                "dedupe_key": bucket_key,
                "aggregate": True,
                "aggregate_count": len(names),
            }
            self._queue.append(queue_item)
            logger.info(f"OpenList 文件转运：聚合任务入队 {bucket_key}，共 {len(names)} 个文件")

        self.__start_worker()

    def __start_worker(self):
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._worker_thread = threading.Thread(target=self.__worker_loop, daemon=True)
            self._worker_thread.start()

    def __worker_loop(self):
        while not self._stop_event.is_set():
            item = None
            with self._queue_lock:
                if self._queue:
                    if self._queue[0].get("not_before", 0) <= time.time():
                        item = self._queue.pop(0)
                        if not item.get("aggregate"):
                            self._queue_keys.discard(item.get("dedupe_key"))
                else:
                    break

            if item is None:
                time.sleep(1)
                continue

            try:
                self.__process_item(item)
            except Exception as err:
                logger.error(f"OpenList 文件转运：处理任务失败 {err}\n{traceback.format_exc()}")
                self.__notify_result(item, False, f"处理异常：{err}")
            finally:
                time.sleep(max(self._dispatch_interval, 0))

    def __process_item(self, item: Dict[str, Any]):
        src_dir = item["src_dir"]
        dst_dir = item["dst_dir"]
        names = item.get("names") or [item["name"]]
        missing_names = [name for name in names if not self.__target_exists(dst_dir, name)]

        if not missing_names:
            self.__notify_result(item, True, "目标已存在，已跳过", skipped=True)
            return

        self.__ensure_dir(dst_dir)
        task_id = self.__submit_copy(src_dir, dst_dir, missing_names)
        task_result = self.__wait_task(task_id)
        success = task_result.get("success", False)
        message = task_result.get("message") or ("copy 成功" if success else "copy 失败")
        self.__notify_result(item, success, message, task_id=task_id, transferred_names=missing_names)

    def __target_exists(self, dst_dir: str, name: str) -> bool:
        payload = {"path": dst_dir, "password": "", "page": 1, "per_page": 0, "refresh": False}
        result = self.__request("POST", self._list_api_path, payload)
        content = result.get("data", {}).get("content") or []
        for entry in content:
            if str(entry.get("name") or "") == name:
                return True
        return False

    def __ensure_dir(self, dst_dir: str):
        parent = str(Path(dst_dir).parent)
        name = Path(dst_dir).name
        if not name:
            return
        self.__request("POST", self._mkdir_api_path, {"path": parent, "name": name})

    def __submit_copy(self, src_dir: str, dst_dir: str, names: List[str]) -> str:
        payload = {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names,
            "overwrite": True,
        }
        result = self.__request("POST", self._copy_api_path, payload)
        data = result.get("data")
        if isinstance(data, dict):
            return str(data.get("task_id") or data.get("id") or "")
        return str(data or "")

    def __wait_task(self, task_id: str) -> Dict[str, Any]:
        if not task_id:
            return {"success": True, "message": "copy 已提交"}

        for _ in range(self._poll_max_times):
            result = self.__request("POST", self._task_info_api_path, {"tid": task_id})
            data = result.get("data") or {}
            status = str(data.get("status") or data.get("state") or "").lower()
            if status in ["done", "success", "finished", "complete", "completed"]:
                return {"success": True, "message": data.get("msg") or "copy 成功"}
            if status in ["failed", "error", "fail"]:
                return {"success": False, "message": data.get("msg") or "copy 失败"}
            time.sleep(max(self._poll_interval, 1))

        return {"success": False, "message": "任务轮询超时"}

    def __request(self, method: str, api_path: str, payload: Optional[dict] = None) -> dict:
        if not self._oplist_url:
            raise ValueError("未配置 OpenList 地址")
        if not self._token:
            raise ValueError("未配置 OpenList Token")
        url = f"{self._oplist_url}{api_path}"
        headers = {"Authorization": self._token, "Content-Type": "application/json"}
        response = requests.request(method=method, url=url, headers=headers, json=payload or {}, timeout=self._timeout)
        response.raise_for_status()
        result = response.json() or {}
        if result.get("code") != 200:
            raise ValueError(result.get("message") or result.get("msg") or f"OpenList API 返回异常: {json.dumps(result, ensure_ascii=False)}")
        return result

    def __notify_result(
        self,
        item: Dict[str, Any],
        success: bool,
        message: str,
        task_id: Optional[str] = None,
        skipped: bool = False,
        transferred_names: Optional[List[str]] = None,
    ):
        result_text = "跳过" if skipped else ("成功" if success else "失败")
        name_text = ", ".join((transferred_names or item.get("names") or [item.get("name")])[:5])
        if transferred_names and len(transferred_names) > 5:
            name_text += " ..."
        notify_msg = (
            f"作业：{self._job_name}\n"
            f"结果：{result_text}\n"
            f"源目录：{item.get('src_dir')}\n"
            f"目标目录：{item.get('dst_dir')}\n"
            f"文件：{name_text}\n"
            f"说明：{message}"
        )
        if task_id:
            notify_msg += f"\n任务ID：{task_id}"

        self.save_data(
            key="last_result",
            value={
                "title": item.get("title"),
                "source_path": item.get("source_path"),
                "src_dir": item.get("src_dir"),
                "dst_dir": item.get("dst_dir"),
                "name": name_text,
                "result": result_text,
                "message": message,
                "task_id": task_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
        )

        if self._notify:
            self.post_message(
                title=f"OpenList 文件转运{result_text}",
                mtype=NotificationType.Plugin,
                text=notify_msg,
            )

    def __extract_relative_dir_and_name(self, source_path: str) -> Tuple[Optional[str], Optional[str]]:
        path_text = str(source_path).replace("\\", "/")
        marker = f"{self._mp_relative_root.rstrip('/')}/"
        if marker not in path_text:
            return None, None
        relative_full = path_text.split(marker, 1)[1].strip("/")
        if not relative_full:
            return None, None
        relative_path = Path(relative_full)
        return str(relative_path.parent).replace("\\", "/"), relative_path.name

    def __is_excluded_dir(self, relative_dir: str) -> bool:
        relative_dir = (relative_dir or "").strip("/")
        if not relative_dir:
            return False
        path_text = f"{relative_dir}/"
        for rule in self.__split_lines(self._exclude_dirs):
            normalized = rule.strip().strip("/")
            if normalized and path_text.startswith(f"{normalized}/"):
                return True
        return False

    def __is_excluded_file(self, name: str) -> bool:
        filename = (name or "").strip()
        if not filename:
            return False
        for rule in self.__split_lines(self._exclude_files):
            pattern = rule.strip()
            if not pattern:
                continue
            if fnmatch.fnmatch(filename, pattern):
                return True
            if filename == pattern:
                return True
        return False

    def __join_path(self, base: str, relative: str) -> str:
        base = "/" + str(base or "").strip().strip("/")
        relative = str(relative or "").strip().strip("/")
        if relative:
            return f"{base}/{relative}"
        return base

    def __split_lines(self, text: str) -> List[str]:
        return [line.strip() for line in str(text or "").splitlines() if line.strip()]

    def __cleanup_recent_locked(self, now_ts: float):
        expire_before = now_ts - self._dedupe_ttl_seconds
        expired_keys = [key for key, ts in self._recent_tasks.items() if ts < expire_before]
        for key in expired_keys:
            self._recent_tasks.pop(key, None)
