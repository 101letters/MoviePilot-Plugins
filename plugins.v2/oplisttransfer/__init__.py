import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
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
    plugin_version = "1.2.0"
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
    _exclude = "下载文件/\n123网盘/\nXiuren/\n私人影视/"
    _delay_seconds = 5
    _dispatch_interval = 3
    _interval_minutes = 360
    _enabled_types = ["movie", "tv"]
    _dedupe_ttl_seconds = 1800
    _max_queue_size = 1000
    _timeout = 60
    _poll_interval = 3
    _poll_max_times = 120

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
        self._exclude = config.get("exclude") or "下载文件/\n123网盘/\nXiuren/\n私人影视/"
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
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "delay_seconds", "label": "入队延迟(秒)", "type": "number"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "dispatch_interval", "label": "下发间隔(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "interval_minutes", "label": "同步间隔(分钟)", "type": "number", "disabled": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": "enabled_types", "label": "处理媒体类型", "multiple": True, "chips": True, "items": [{"title": "电影", "value": "movie"}, {"title": "剧集", "value": "tv"}]}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "调用方式固定为事件触发，不显示也不开放底层 OpenList API 路径配置。同步方法固定为仅新增：目标目录存在同名文件则跳过，不重复上传。"}}]}]
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "路径规则：从 MP 整理路径中截取 /media 后面的相对路径，如 /media/华语电影/xxx/abc.mkv -> 华语电影/xxx；源目录 = 源目录前缀 + 相对目录；目标目录 = 目标目录前缀 + 相对目录；文件名保持不变。"}}]}]
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "exclude", "label": "排除项规则", "rows": 6, "placeholder": "下载文件/\n123网盘/\nXiuren/\n私人影视/"}}]}]
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
            "exclude": "下载文件/\n123网盘/\nXiuren/\n私人影视/",
            "delay_seconds": 5,
            "dispatch_interval": 3,
            "interval_minutes": 360,
            "enabled_types": ["movie", "tv"],
        }

    def get_page(self) -> List[dict]:
        last = self.get_data("last_result") or {}
        queue_size = len(self._queue)
        lines = [f"当前队列长度: {queue_size}"]
        for key in ["title", "source_path", "src_dir", "dst_dir", "name", "result", "message", "task_id", "time"]:
            value = last.get(key)
            if value not in [None, ""]:
                lines.append(f"{key}: {value}")
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "\n".join(lines)}}]

    def stop_service(self):
        self._stop_event.set()

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

        if self.__is_excluded(relative_dir, name):
            logger.info(f"OpenList 文件转运：命中排除规则，跳过 {source_path}")
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
            if len(self._queue) >= self._max_queue_size:
                logger.warning("OpenList 文件转运：队列已满，丢弃新任务")
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
            self._queue.append(item)
            self._queue_keys.add(dedupe_key)
            self._recent_tasks[dedupe_key] = now

        self.__start_worker()

    def __start_worker(self):
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self.__worker_loop, name="oplisttransfer-worker", daemon=True)
            self._worker_thread.start()

    def __worker_loop(self):
        while not self._stop_event.is_set():
            item = None
            now = time.time()
            with self._queue_lock:
                if self._queue and self._queue[0].get("not_before", 0) <= now:
                    item = self._queue.pop(0)
                    self._queue_keys.discard(item.get("dedupe_key"))
            if not item:
                time.sleep(1)
                continue
            try:
                self.__handle_item(item)
            except Exception as e:
                logger.error(f"OpenList 文件转运：处理队列任务异常 {e}\n{traceback.format_exc()}")
                self.__record_and_notify(item=item, result="failed", message=str(e), task_id=None)
            if self._dispatch_interval > 0:
                time.sleep(self._dispatch_interval)

    def __handle_item(self, item: Dict[str, Any]):
        token = self.__get_token()
        if not token:
            raise Exception("未配置 OpenList token")

        if self.__target_exists(token, item["dst_dir"], item["name"]):
            self.__record_and_notify(item=item, result="skipped", message="目标目录已存在同名文件，跳过", task_id=None)
            return

        self.__mkdir(token, item["dst_dir"])
        task_id = self.__copy_file(token=token, src_dir=item["src_dir"], dst_dir=item["dst_dir"], name=item["name"])
        if not task_id:
            raise Exception("OpenList 未返回 copy task id")

        self.__wait_task_done(token, task_id)
        self.__record_and_notify(item=item, result="success", message="copy 任务完成", task_id=task_id)

    def __extract_relative_dir_and_name(self, source_path: str) -> Tuple[Optional[str], Optional[str]]:
        p = Path(source_path)
        name = p.name
        parent = str(p.parent)
        root = self._mp_relative_root.rstrip("/") or "/"
        if not parent.startswith(root):
            return None, None
        relative_dir = parent[len(root):].strip("/")
        return relative_dir, name

    def __is_excluded(self, relative_dir: str, name: str) -> bool:
        candidates = [relative_dir, f"{relative_dir}/{name}".strip("/")]
        rules = [line.strip() for line in (self._exclude or "").splitlines() if line.strip()]
        for rule in rules:
            rule = rule.strip("/")
            for candidate in candidates:
                if candidate.startswith(rule):
                    return True
        return False

    def __headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": token, "Content-Type": "application/json"}

    def __get_token(self) -> str:
        return self._token or ""

    def __post_json(self, token: str, api_path: str, payload: Optional[Dict[str, Any]] = None,
                    params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._oplist_url}{api_path}"
        resp = requests.post(url, headers=self.__headers(token), json=payload, params=params, timeout=(60, 300))
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:500]}")
        data = self.__safe_json(resp)
        if not isinstance(data, dict):
            raise Exception("OpenList 返回非 JSON")
        code = data.get("code")
        if code != 200:
            raise Exception(f"AList/OpenList 返回 {code}：{data.get('message')}")
        return data.get("data")

    def __mkdir(self, token: str, path: str):
        try:
            self.__post_json(token, self._mkdir_api_path, payload={"path": path})
        except Exception as e:
            logger.warning(f"OpenList mkdir 失败但继续：{e}")

    def __copy_file(self, token: str, src_dir: str, dst_dir: str, name: str) -> Optional[str]:
        data = self.__post_json(token, self._copy_api_path, payload={
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "overwrite": True,
            "names": [name],
        })
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if tasks:
            first = tasks[0]
            if isinstance(first, dict):
                return first.get("id")
        return None

    def __target_exists(self, token: str, dst_dir: str, name: str) -> bool:
        try:
            data = self.__post_json(token, self._list_api_path, payload={
                "path": dst_dir,
                "refresh": self._use_cache_t != 1,
            })
            content = data.get("content") if isinstance(data, dict) else None
            if not content:
                return False
            for item in content:
                if item.get("name") == name:
                    return True
            return False
        except Exception as e:
            logger.warning(f"OpenList 检查目标是否存在失败，按不存在处理：{e}")
            return False

    def __wait_task_done(self, token: str, task_id: str):
        for _ in range(self._poll_max_times):
            info = self.__post_json(token, self._task_info_api_path, params={"tid": task_id})
            if not isinstance(info, dict):
                raise Exception("任务状态返回异常")
            state = info.get("state")
            error = info.get("error")
            if state == 2:
                return
            if error:
                raise Exception(f"copy 任务失败：{error}")
            time.sleep(self._poll_interval)
        raise Exception("等待 OpenList copy 任务完成超时")

    def __safe_json(self, resp) -> Any:
        try:
            return resp.json()
        except Exception:
            return {}

    def __record_and_notify(self, item: Dict[str, Any], result: str, message: str, task_id: Optional[str]):
        record = {
            "title": item.get("title"),
            "source_path": item.get("source_path"),
            "src_dir": item.get("src_dir"),
            "dst_dir": item.get("dst_dir"),
            "name": item.get("name"),
            "result": result,
            "message": message,
            "task_id": task_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("last_result", record)
        if not self._notify:
            return
        mapping = {"success": "成功", "failed": "失败", "skipped": "跳过"}
        notify_title = f"【OpenList 文件转运{mapping.get(result, result)}】"
        notify_text = (
            f"作业：{self._job_name}\n"
            f"媒体：{item.get('title')}\n"
            f"源目录：{item.get('src_dir')}\n"
            f"目标目录：{item.get('dst_dir')}\n"
            f"文件名：{item.get('name')}\n"
            f"结果：{result}\n"
            f"任务ID：{task_id or '-'}\n"
            f"详情：{message}"
        )
        self.post_message(mtype=NotificationType.Plugin, title=notify_title, text=notify_text)

    def __cleanup_recent_locked(self, now: float):
        expired = [k for k, ts in self._recent_tasks.items() if now - ts >= self._dedupe_ttl_seconds]
        for k in expired:
            self._recent_tasks.pop(k, None)

    def __join_path(self, *parts: str) -> str:
        cleaned = []
        for idx, part in enumerate(parts):
            if part is None:
                continue
            part = str(part).strip()
            if not part:
                continue
            if idx == 0 and part == "/":
                cleaned.append("")
                continue
            cleaned.append(part.strip("/"))
        result = "/" + "/".join([p for p in cleaned if p != ""])
        return result or "/"
