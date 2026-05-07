"""MoviePilot V2 plugin: copy transferred media folders via OpenList /api/fs/copy."""
import hashlib
import json
import os
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .client import OpenListClient
from .queue import CopyQueue, CopyTask


DEFAULT_MAPPINGS = [
    {"category": "外语电影", "mp_dir": "/media/外语电影", "src_dir": "/影视库/外语电影", "dst_dir": "/123云盘/影视/外语电影"},
    {"category": "动画电影", "mp_dir": "/media/动画电影", "src_dir": "/影视库/动画电影", "dst_dir": "/123云盘/影视/动画电影"},
    {"category": "华语电影", "mp_dir": "/media/华语电影", "src_dir": "/影视库/华语电影", "dst_dir": "/123云盘/影视/华语电影"},
    {"category": "纪录片", "mp_dir": "/media/纪录片", "src_dir": "/影视库/纪录片", "dst_dir": "/123云盘/影视/纪录片"},
    {"category": "国产剧", "mp_dir": "/media/国产剧", "src_dir": "/影视库/国产剧", "dst_dir": "/123云盘/影视/国产剧"},
    {"category": "欧美剧", "mp_dir": "/media/欧美剧", "src_dir": "/影视库/欧美剧", "dst_dir": "/123云盘/影视/欧美剧"},
    {"category": "日韩剧", "mp_dir": "/media/日韩剧", "src_dir": "/影视库/日韩剧", "dst_dir": "/123云盘/影视/日韩剧"},
    {"category": "国漫", "mp_dir": "/media/国漫", "src_dir": "/影视库/国漫", "dst_dir": "/123云盘/影视/国漫"},
    {"category": "日番", "mp_dir": "/media/日番", "src_dir": "/影视库/日番", "dst_dir": "/123云盘/影视/日番"},
]


class OpenListCopy(_PluginBase):
    plugin_name = "OpenList复制"
    plugin_desc = "监听整理完成事件，通过 OpenList 内部 copy API 复制媒体文件夹到网盘。"
    plugin_icon = "copy.png"
    plugin_version = "0.1.0"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "openlistcopy_"
    plugin_order = 30
    auth_level = 1

    _enabled = False
    _openlist_url = ""
    _username = ""
    _password = ""
    _token = ""
    _timeout = 60
    _skip_existing = True
    _max_retries = 3
    _retry_interval = 300
    _notify = True
    _mappings_text = json.dumps(DEFAULT_MAPPINGS, ensure_ascii=False, indent=2)
    _client: Optional[OpenListClient] = None
    _queue: Optional[CopyQueue] = None
    _mappings: List[dict] = []

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = self._to_bool(config.get("enabled", False))
            self._openlist_url = config.get("openlist_url") or config.get("oplist_url") or ""
            self._username = config.get("username") or ""
            self._password = config.get("password") or ""
            self._token = config.get("token") or ""
            self._timeout = self._to_int(config.get("timeout"), 60)
            self._skip_existing = self._to_bool(config.get("skip_existing", True))
            self._max_retries = self._to_int(config.get("max_retries"), 3)
            self._retry_interval = self._to_int(config.get("retry_interval"), 300)
            self._notify = self._to_bool(config.get("notify", True))
            self._mappings_text = config.get("mappings") or self._mappings_text

        self._mappings = self._load_mappings(self._mappings_text)
        if not self._openlist_url:
            logger.warning("OpenListCopy: OpenList 地址未配置")
            return

        self._client = OpenListClient(
            base_url=self._openlist_url,
            username=self._username,
            password=self._password,
            token=self._token,
            timeout=self._timeout,
        )
        if self._queue:
            self._queue.stop()
        self._queue = CopyQueue(
            client=self._client,
            max_retries=self._max_retries,
            retry_interval=self._retry_interval,
            skip_existing=self._skip_existing,
            notify_callback=self._send_notify,
        )
        if self._enabled:
            self._queue.start()

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled or not self._queue:
            return
        try:
            path = self._extract_target_path(event.event_data or {})
            if not path:
                logger.warning("OpenListCopy: TransferComplete 事件未找到 target_diritem 路径")
                return
            task = self._build_task(path)
            if not task:
                return
            self._queue.enqueue(task)
        except Exception as e:
            logger.error(f"OpenListCopy 处理事件异常: {e}\n{traceback.format_exc()}")

    def _extract_target_path(self, data: Dict[str, Any]) -> str:
        transfer = data.get("transferinfo") or data.get("transfer_info") or data
        target = transfer.get("target_diritem") or transfer.get("target") or transfer.get("target_path")
        if isinstance(target, dict):
            for key in ("path", "file_path", "target_path", "target", "name"):
                if target.get(key):
                    return str(target.get(key))
        if isinstance(target, (str, Path)):
            return str(target)
        if target is not None:
            for key in ("path", "file_path", "target_path", "target", "name"):
                value = getattr(target, key, None)
                if value:
                    return str(value)
        for key in ("target_dir", "target_path", "file_path", "path", "dest", "destination"):
            value = transfer.get(key) if isinstance(transfer, dict) else getattr(transfer, key, None)
            if value:
                return str(value)
        return ""

    def _build_task(self, mp_path: str) -> Optional[CopyTask]:
        norm_path = self._norm_local(mp_path)
        mapping = self._match_mapping(norm_path)
        if not mapping:
            logger.info(f"OpenListCopy: 路径未匹配映射，跳过: {mp_path}")
            return None

        mp_dir = self._norm_local(mapping["mp_dir"])
        rel = norm_path[len(mp_dir):].strip("/")
        if not rel:
            logger.warning(f"OpenListCopy: 目标路径是分类根目录，跳过: {mp_path}")
            return None
        # copy 整个媒体文件夹：取分类目录下的第一层目录名
        name = rel.split("/")[0]
        raw_id = f"{mapping['src_dir']}|{mapping['dst_dir']}|{name}"
        task_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
        return CopyTask(
            task_id=task_id,
            mp_path=mp_path,
            src_dir=self._norm_remote(mapping["src_dir"]),
            dst_dir=self._norm_remote(mapping["dst_dir"]),
            name=name,
            max_retries=self._max_retries,
        )

    def _match_mapping(self, path: str) -> Optional[dict]:
        matched = []
        for item in self._mappings:
            mp_dir = self._norm_local(item.get("mp_dir", ""))
            if mp_dir and (path == mp_dir or path.startswith(mp_dir + "/")):
                matched.append((len(mp_dir), item))
        return sorted(matched, key=lambda x: x[0], reverse=True)[0][1] if matched else None

    @staticmethod
    def _norm_local(path: str) -> str:
        return os.path.normpath(str(path)).replace("\\", "/").rstrip("/")

    @staticmethod
    def _norm_remote(path: str) -> str:
        value = str(PurePosixPath(str(path).replace("\\", "/"))).rstrip("/")
        return value if value.startswith("/") else f"/{value}"


    @staticmethod
    def _to_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on", "启用", "是")
        return bool(value)

    @staticmethod
    def _to_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _load_mappings(self, text) -> List[dict]:
        try:
            data = json.loads(text) if isinstance(text, str) else text
            if isinstance(data, list):
                return [m for m in data if m.get("mp_dir") and m.get("src_dir") and m.get("dst_dir")]
        except Exception as e:
            logger.warning(f"OpenListCopy: 路径映射 JSON 解析失败，使用默认映射: {e}")
        return DEFAULT_MAPPINGS

    def stop_service(self):
        if self._queue:
            self._queue.stop()

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/test_connection", "endpoint": self.api_test_connection, "methods": ["POST"], "summary": "测试 OpenList 连接"},
            {"path": "/queue_stats", "endpoint": self.api_queue_stats, "methods": ["GET"], "summary": "复制队列统计"},
            {"path": "/tasks", "endpoint": self.api_tasks, "methods": ["GET"], "summary": "最近任务"},
            {"path": "/retry_failed", "endpoint": self.api_retry_failed, "methods": ["POST"], "summary": "重试失败任务"},
        ]

    def api_test_connection(self) -> dict:
        client = self._client or OpenListClient(self._openlist_url, self._username, self._password, self._token, self._timeout)
        return {"success": True, "message": client.test_connection()}

    def api_queue_stats(self) -> dict:
        return {"success": True, "data": self._queue.stats() if self._queue else {}}

    def api_tasks(self) -> dict:
        return {"success": True, "data": self._queue.recent_tasks() if self._queue else []}

    def api_retry_failed(self) -> dict:
        count = self._queue.retry_failed() if self._queue else 0
        return {"success": True, "message": f"已重新入队 {count} 个失败任务"}

    def get_page(self) -> List[dict]:
        stats = self._queue.stats() if self._queue else {}
        return [{"component": "div", "text": f"OpenListCopy 队列统计：{stats}"}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {"component": "VForm", "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "监听 TransferComplete，通过 OpenList /api/fs/copy 复制整个媒体文件夹；不会删除本地文件。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "skip_existing", "label": "目标已存在则跳过"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "失败通知"}}]},
                ]},
                {"component": "VTextField", "props": {"model": "openlist_url", "label": "OpenList 地址", "placeholder": "http://192.168.1.100:5244"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "username", "label": "用户名"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "password", "label": "密码", "type": "password"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "token", "label": "Token", "type": "password"}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "请求超时(秒)", "type": "number"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "max_retries", "label": "失败重试次数", "type": "number"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "retry_interval", "label": "重试间隔(秒)", "type": "number"}}]},
                ]},
                {"component": "VTextarea", "props": {"model": "mappings", "label": "9 类路径映射(JSON)", "rows": 15, "auto-grow": True}},
            ]}
        ], {
            "enabled": False,
            "openlist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "timeout": 60,
            "skip_existing": True,
            "max_retries": 3,
            "retry_interval": 300,
            "notify": True,
            "mappings": json.dumps(DEFAULT_MAPPINGS, ensure_ascii=False, indent=2),
        }

    def _send_notify(self, text: str):
        if not self._notify:
            return
        try:
            self.post_message(mtype=NotificationType.Plugin, title="OpenListCopy", text=text)
        except Exception as e:
            logger.warning(f"OpenListCopy 发送通知失败: {e}")
