"""CloudSyncHelper — 云盘上传助手 MoviePilot V2 插件

链路：Phase 1 监听整理完成事件 → Phase 2 上传 AList 云端。

仅保留「监听文件整理 + 上传云盘」核心功能，去除了 STRM 生成、302 跳转、Emby 代理等附属能力。
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

import pathspec
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .cloud_sync import (
    TASK_SKIPPED,
    TASK_SUCCEEDED,
    AlistClient,
    CloudSync,
)
from .sse_listener import MoviePilotSseListener
from .transfer_listener import TransferListener, TransferRecord


# 默认可处理媒体扩展名
DEFAULT_MEDIA_EXTS = (
    "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v"
)
DEFAULT_MOVIEPILOT_ADDRESS = "http://192.168.31.6:3000"
DEFAULT_ALIST_URL = "http://192.168.31.6:5244/"
DEFAULT_UPLOAD_PATH_MAPPINGS = (
    "/media/movies#/123云盘/影视/华语电影\n"
    "/media/tv#/123云盘/影视/电视剧"
)
DEFAULT_EXCLUDE_PATTERNS = "*.tmp\n**/.DS_Store\n/sample/**"
DEFAULT_EVENT_FILTERS = "/media/movies\n/media/tv"
BULK_DETAIL_LOG_THRESHOLD = 100
BULK_PROGRESS_LOG_INTERVAL = 15.0


class ManualActionParams(BaseModel):
    """首页列表内单条操作的 POST body 参数。"""
    action: str = Field("", description="reupload/delete_remote/delete_remote_and_local")
    local: str = Field("", description="本地源文件路径（reupload/delete_remote_and_local 需要）")
    remote: str = Field("", description="云端路径（所有动作都需要）")

    def validate_action(self) -> Optional[str]:
        action = (self.action or "").strip().lower()
        if action not in {"reupload", "delete_remote", "delete_remote_and_local"}:
            return f"未知动作: {action}"
        if not (self.remote or "").strip():
            return "缺少 remote 参数"
        if action in ("reupload", "delete_remote_and_local"):
            if not (self.local or "").strip():
                return "缺少 local 参数"
        return None


class CloudSyncHelper(_PluginBase):
    """云盘上传助手。"""

    # ---- 插件元数据 ----
    plugin_name = "云盘上传助手"
    plugin_desc = "监听 MP 整理完成事件，自动上传媒体文件到 AList/OpenList 云盘"
    plugin_icon = "https://raw.githubusercontent.com/101letters/MoviePilot-Plugins/main/icons/cloudsynchelper.png"
    plugin_version = "1.0.0"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "cloudsynchelper_"
    plugin_order = 99
    auth_level = 1

    # ---- 私有状态 ----
    _enabled = False
    _moviepilot_address = ""
    _cloud_storage_type = "alist"
    _alist_url = ""
    _alist_token = ""
    _alist_target_path = ""
    _upload_path_mappings = ""
    _upload_mappings: List[Tuple[str, str]] = []
    _local_media_path = ""
    _local_media_roots: List[str] = []
    _sync_mode = "copy"
    _exclude_patterns = ""
    _exclude_spec: Optional[pathspec.PathSpec] = None
    _event_filters = ""
    _event_filter_prefixes: List[str] = []
    _notify_enabled = True
    _rmt_mediaext: List[str] = []
    _upload_concurrency = 3
    _once_sync = False
    _once_upload_full = False
    _once_upload_incremental = False
    _sse_enabled = False
    _manual_upload_action = "none"
    _manual_upload_target = ""
    _manual_confirm = False
    _manual_execute = False
    _pending_manual_action: Optional[Dict[str, Any]] = None

    _scheduler: Optional[BackgroundScheduler] = None
    _alist_client: Optional[AlistClient] = None
    _cloud_sync: Optional[CloudSync] = None
    _listener: Optional[TransferListener] = None
    _sse_listener: Optional[MoviePilotSseListener] = None
    _sync_lock = threading.Lock()
    _stats: Dict[str, Any] = {}
    _last_upload_batch: Dict[str, int] = {}

    # ============================================================
    # 生命周期
    # ============================================================
    def init_plugin(self, config: Dict[str, Any] = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._moviepilot_address = (config.get("moviepilot_address") or DEFAULT_MOVIEPILOT_ADDRESS).strip()
            self._cloud_storage_type = "alist"
            self._alist_url = (config.get("alist_url") or DEFAULT_ALIST_URL).strip()
            self._alist_token = (config.get("alist_token") or "").strip()
            self._upload_path_mappings = self._normalize_upload_path_mappings(config).strip()
            self._sync_mode = self._normalize_sync_mode(config.get("sync_mode") or "copy")
            self._exclude_patterns = (
                config.get("exclude_patterns")
                if config.get("exclude_patterns") is not None
                else DEFAULT_EXCLUDE_PATTERNS
            )
            self._event_filters = (
                config.get("event_filters")
                if config.get("event_filters") is not None
                else DEFAULT_EVENT_FILTERS
            )
            self._notify_enabled = bool(config.get("notify_enabled", True))
            ext_str = (config.get("rmt_mediaext") or DEFAULT_MEDIA_EXTS)
            self._rmt_mediaext = [e.strip().lower().lstrip(".") for e in ext_str.split(",") if e.strip()]
            try:
                self._upload_concurrency = max(1, int(config.get("upload_concurrency") or 3))
            except (TypeError, ValueError):
                self._upload_concurrency = 3
            self._once_sync = bool(config.get("once_sync", False))
            self._once_upload_full = bool(config.get("once_upload_full", False))
            self._once_upload_incremental = bool(config.get("once_upload_incremental", False))
            self._sse_enabled = bool(config.get("sse_enabled", False))
            self._manual_upload_action = self._normalize_manual_upload_action(
                config.get("manual_upload_action") or "none")
            self._manual_upload_target = config.get("manual_upload_target") or ""
            self._manual_confirm = bool(config.get("manual_confirm", False))
            self._manual_execute = bool(config.get("manual_execute", False))
            self._pending_manual_action = None
            self._update_config()

        # 派生
        self._exclude_spec = self._build_exclude_spec(self._exclude_patterns)
        self._upload_mappings = self._parse_path_mappings(self._upload_path_mappings)
        self._local_media_roots = [local for local, _ in self._upload_mappings]
        self._local_media_path = "\n".join(self._local_media_roots)
        self._event_filter_prefixes = [
            p.strip() for p in (self._event_filters or "").splitlines() if p.strip()
        ]
        self._stats = self._load_stats()

        # 先停旧资源
        self.stop_service()

        if not self._enabled:
            return

        # 校验必填
        if not self._upload_mappings:
            logger.warning("【云盘上传】未配置上传路径映射，插件不启动")
            return

        # 构建云端客户端
        try:
            self._alist_client = self._build_cloud_client()
        except Exception as e:
            logger.error(f"【云盘上传】云端客户端初始化失败: {e}", exc_info=True)
            self._alist_client = None

        # 构建各模块
        self._cloud_sync = CloudSync(
            self, self._alist_client, sync_mode=self._sync_mode,
            concurrency=self._upload_concurrency,
        )
        self._cloud_sync.start()
        self._listener = TransferListener(self)
        self._sse_listener = None
        if self._sse_enabled:
            self._sse_listener = MoviePilotSseListener(self)
            self._sse_listener.start()

        logger.info("【云盘上传】插件已启动：storage=%s, sync=%s, 并发=%d, SSE=%s",
                    self._cloud_storage_type, self._sync_mode, self._upload_concurrency,
                    "on" if self._sse_enabled else "off")

        once_actions = self._collect_once_actions()
        if once_actions:
            self._reset_once_flags()
            self._update_config()
            self._schedule_once_actions(once_actions)

    def _build_cloud_client(self):
        st = self._cloud_storage_type
        if st == "alist":
            if not self._alist_url or not self._alist_token:
                raise Exception("AList 类型需配置 alist_url 和 alist_token")
            return AlistClient(self._alist_url, self._alist_token)
        else:
            raise Exception(f"未知云端存储类型: {st}")

    def _collect_once_actions(self) -> List[Tuple[str, str, Any]]:
        actions: List[Tuple[str, str, Any]] = []
        if self._once_sync:
            actions.append(("legacy_sync", "立即全量同步上传", self.run_once))
        if self._once_upload_full:
            actions.append(("upload_full", "立即全量同步上传云端", self.run_upload_full_once))
        if self._once_upload_incremental:
            actions.append(("upload_incremental", "立即增量同步上传云端", self.run_upload_incremental_once))
        return actions

    def _reset_once_flags(self) -> None:
        self._once_sync = False
        self._once_upload_full = False
        self._once_upload_incremental = False

    def _schedule_once_actions(self, actions: List[Tuple[str, str, Any]]) -> None:
        if not actions:
            return
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = BackgroundScheduler()
            run_time = self._now_plus_seconds(3)
            for key, label, func in actions:
                self._scheduler.add_job(
                    self._safe_run_named, "date",
                    run_date=run_time, id=f"cloudsyncheler_once_{key}",
                    args=[label, func],
                )
            self._scheduler.start()
            logger.info("【云盘上传】已排定 3s 后执行一次性任务: %s",
                        ", ".join(label for _, label, _ in actions))
        except Exception as e:
            logger.error(f"【云盘上传】排定一次性任务失败: {e}", exc_info=True)

    def _safe_run_named(self, label: str, func) -> None:
        try:
            logger.info(f"【云盘上传】开始执行: {label}")
            func()
        except Exception as e:
            logger.error(f"【云盘上传】{label} 异常: {e}", exc_info=True)

    @staticmethod
    def _now_plus_seconds(seconds: int):
        from datetime import datetime, timedelta
        return datetime.now() + timedelta(seconds=seconds)

    def stop_service(self) -> None:
        try:
            if self._sse_listener:
                self._sse_listener.stop()
                self._sse_listener = None
        except Exception as e:
            logger.error(f"【云盘上传】停止 SSE 监听失败: {e}")
        try:
            if self._scheduler:
                try:
                    self._scheduler.remove_all_jobs()
                except Exception:
                    pass
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as e:
            logger.error(f"【云盘上传】停止 scheduler 失败: {e}")
        try:
            if self._cloud_sync:
                self._cloud_sync.stop()
        except Exception as e:
            logger.error(f"【云盘上传】停止云同步失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        """首页统计面板：2 统计卡片 + 最近上传表。"""
        stats = getattr(self, "_stats", None) or {}
        if not stats and not isinstance(self, type):
            stats = self._load_stats()
        recent_uploads = stats.get("recent_uploads") or []
        upload_count = stats.get("upload_count") or 0
        last_upload_time = stats.get("last_upload_time") or ""

        def _status_text(s):
            return {
                "uploaded": "已上传",
                "remote_deleted": "已删云端",
                "local_deleted": "已删本地",
            }.get(s, s or "-")

        def _fmt_size(v):
            try:
                v = int(v or 0)
            except Exception:
                v = 0
            if v <= 0:
                return "-"
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if v < 1024:
                    return f"{v:.2f} {unit}"
                v /= 1024
            return f"{v:.2f} PB"

        manual_api = f"plugin/{self.__class__.__name__}/manual_action?apikey={settings.API_TOKEN}"
        clear_upload_api = f"plugin/{self.__class__.__name__}/clear_upload_history?apikey={settings.API_TOKEN}"

        def _menu_item(text, icon, params, color="primary"):
            return {
                "component": "VListItem",
                "props": {"base-color": color, "prepend-icon": icon, "title": text},
                "events": {"click": {"api": manual_api, "method": "post", "params": params}},
            }

        def _action_menu_cell(items):
            if not items:
                return {"component": "td", "props": {"class": "text-grey"}, "text": "-"}
            return {
                "component": "td",
                "content": [{
                    "component": "VBtn",
                    "props": {
                        "size": "small", "variant": "tonal", "color": "primary",
                        "icon": "mdi-dots-horizontal", "density": "compact",
                    },
                    "content": [{
                        "component": "VMenu",
                        "props": {"activator": "parent", "close-on-content-click": True},
                        "content": [{
                            "component": "VList",
                            "props": {"density": "compact", "nav": True},
                            "content": items,
                        }],
                    }],
                }],
            }

        def _upload_action_cell(it):
            local = it.get("local") or ""
            remote = it.get("remote") or ""
            status = it.get("status") or ""
            items = []
            if local and remote and status == "uploaded":
                items.append(_menu_item(
                    "重新上传", "mdi-upload-refresh",
                    {"action": "reupload", "local": local, "remote": remote}))
            if remote:
                items.append(_menu_item(
                    "删除云端", "mdi-cloud-remove",
                    {"action": "delete_remote", "local": "", "remote": remote},
                    color="warning"))
            if local and remote:
                items.append(_menu_item(
                    "删云端和本地", "mdi-delete-forever",
                    {"action": "delete_remote_and_local", "local": local, "remote": remote},
                    color="error"))
            return _action_menu_cell(items)

        upload_rows = [
            {"component": "tr", "content": [
                {"component": "td", "text": it.get("name") or "-"},
                {"component": "td", "text": _status_text(it.get("status"))},
                {"component": "td", "text": _fmt_size(it.get("size"))},
                {"component": "td", "text": it.get("time") or "-"},
                {"component": "td", "props": {"class": "text-caption text-grey"}, "text": it.get("remote") or "-"},
                _upload_action_cell(it),
            ]}
            for it in recent_uploads[:5]
        ]
        if not upload_rows:
            upload_rows = [{"component": "tr", "content": [
                {"component": "td", "props": {"colspan": 6, "class": "text-grey"}, "text": "暂无记录"},
            ]}]

        def _stat_card(caption, value):
            return {
                "component": "VCard", "props": {"variant": "outlined"},
                "content": [{"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": caption},
                    {"component": "div", "props": {"class": "text-h5"}, "text": str(value)},
                ]}],
            }

        return [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6},
                     "content": [_stat_card("累计上传数量", upload_count)]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6},
                     "content": [_stat_card("最近上传时间", last_upload_time or "-")]},
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center py-3 px-4"},
                     "content": [
                         {"component": "span", "text": "最近上传列表"},
                         {"component": "VSpacer"},
                         {"component": "VBtn", "props": {
                             "prepend-icon": "mdi-delete-sweep", "variant": "tonal",
                             "color": "info", "size": "small", "density": "compact",
                         }, "text": "清除上传历史",
                          "events": {"click": {"api": clear_upload_api, "method": "post"}}},
                     ]},
                    {"component": "VTable", "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "文件名"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "大小"},
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "云端路径"},
                            {"component": "th", "text": "操作"},
                        ]}]},
                        {"component": "tbody", "content": upload_rows},
                    ]},
                ],
            },
        ]

    # ============================================================
    # API 端点
    # ============================================================
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/status",
                "endpoint": self.status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "同步状态",
                "description": "查询当前云同步任务进度",
            },
            {
                "path": "/diagnose",
                "endpoint": self.diagnose,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "运行诊断",
                "description": "查询脱敏配置、路径映射、模块状态；probe=true 时只读探测 AList",
            },
            {
                "path": "/sync_now",
                "endpoint": self.sync_now,
                "methods": ["GET", "POST"],
                "auth": "apikey",
                "summary": "手动同步",
                "description": "手动触发同步；action 可选 upload_full/upload_incremental",
            },
            {
                "path": "/clear_upload_history",
                "endpoint": self.clear_upload_history,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "清除上传历史",
                "description": "仅清除最近上传列表记录，不删除云端/本地文件",
            },
            {
                "path": "/manual_action",
                "endpoint": self.manual_action,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "单条手动处理",
                "description": "对最近上传记录执行单条操作：reupload/delete_remote/delete_remote_and_local",
            },
        ]

    def status(self, request: Request = None):
        if self._cloud_sync is None:
            return JSONResponse({"state": False, "message": "云同步未初始化"}, status_code=503)
        return JSONResponse({"state": True, "data": self._cloud_sync.get_status()})

    def diagnose(self, request: Request = None, probe: bool = False):
        return JSONResponse({"state": True, "data": self._diagnostic_snapshot(probe=probe)})

    def sync_now(self, request: Request = None, action: str = ""):
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)
        if not self._local_media_path:
            return JSONResponse({"state": False, "message": "未配置本地媒体路径"}, status_code=400)
        action_key, label, func = self._resolve_sync_action(action)
        if not func:
            return JSONResponse({"state": False, "message": f"未知同步动作: {action}"}, status_code=400)
        threading.Thread(
            target=self._safe_run_named, args=(label, func),
            daemon=True, name=f"CloudSyncSyncNow-{action_key}",
        ).start()
        return JSONResponse({"state": True, "message": f"已触发: {label}"})

    def clear_upload_history(self, request: Request = None):
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)
        try:
            stats = getattr(self, "_stats", None) or self._load_stats()
            stats["recent_uploads"] = []
            stats["upload_count"] = 0
            stats["last_upload_time"] = ""
            self._stats = stats
            self._save_stats()
            logger.info("【云盘上传】上传历史已清除（云端/本地文件未删除）")
            return JSONResponse({"state": True, "message": "上传历史已清除（云端/本地文件未删除）"})
        except Exception as e:
            logger.error(f"【云盘上传】清除上传历史失败: {e}", exc_info=True)
            return JSONResponse({"state": False, "message": f"清除失败: {e}"}, status_code=500)

    def _resolve_sync_action(self, action: str) -> Tuple[str, str, Any]:
        normalized = (action or "").strip().lower()
        mapping = {
            "": ("legacy_sync", "立即全量同步上传", self._safe_run_once),
            "sync": ("legacy_sync", "立即全量同步上传", self._safe_run_once),
            "all": ("legacy_sync", "立即全量同步上传", self._safe_run_once),
            "upload_full": ("upload_full", "立即全量同步上传云端", self.run_upload_full_once),
            "full_upload": ("upload_full", "立即全量同步上传云端", self.run_upload_full_once),
            "upload_incremental": ("upload_incremental", "立即增量同步上传云端", self.run_upload_incremental_once),
            "incremental_upload": ("upload_incremental", "立即增量同步上传云端", self.run_upload_incremental_once),
        }
        return mapping.get(normalized, (normalized, "", None))

    def manual_action(self, params: ManualActionParams):
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)

        err = params.validate_action()
        if err:
            return JSONResponse({"state": False, "message": err}, status_code=400)

        action = (params.action or "").strip().lower()
        local_path = (params.local or "").strip()
        remote_path = (params.remote or "").strip()

        # 后台执行
        threading.Thread(
            target=self._manual_action_worker,
            args=(action, local_path, remote_path), daemon=True,
            name="CloudSyncManualAction",
        ).start()
        logger.info(f"【手动处理】已开始执行单条操作: action={action}, remote={remote_path}")
        return JSONResponse({"state": True, "message": f"已开始执行: {action}"})

    def _manual_action_worker(self, action: str, local_path: str, remote_path: str) -> None:
        try:
            if action == "reupload":
                self._validate_reupload_paths(local_path, remote_path)
                self._manual_reupload_worker(local_path, remote_path)
            elif action == "delete_remote":
                self._manual_delete_remote_worker(remote_path)
            elif action == "delete_remote_and_local":
                self._manual_delete_remote_worker(remote_path)
                self._manual_delete_local_file(local_path)
            else:
                raise Exception(f"未知上传手动动作: {action}")
        except Exception as e:
            logger.error(f"【手动处理】执行失败: {e}", exc_info=True)
            self._notify("云盘上传手动处理失败", str(e))

    def _manual_reupload_worker(self, local_path: str, remote_path: str) -> None:
        try:
            if not self._alist_client:
                raise Exception("AList 客户端未初始化")
            try:
                self._alist_client.remove_file(remote_path)
            except Exception as e:
                logger.warning(f"【手动处理】删除云端文件失败，继续尝试上传: {remote_path} ({e})")
            parent = str(Path(remote_path).parent)
            if parent and parent not in (".", "/"):
                self._alist_client.mkdir(parent)
            self._alist_client.put_stream(local_path, remote_path, as_task=False)
            size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            self._record_upload_stat(local_path, remote_path, size, status="uploaded")
            logger.info(f"【手动处理】重新上传完成: {local_path} -> {remote_path}")
            self._notify("云盘上传手动处理完成", f"重新上传完成：{Path(local_path).name}")
        except Exception as e:
            logger.error(f"【手动处理】重新上传失败: {local_path} -> {remote_path}: {e}", exc_info=True)
            raise

    def _manual_delete_remote_worker(self, remote_path: str) -> None:
        if not self._alist_client:
            raise Exception("AList 客户端未初始化")
        remote_path = self._validate_remote_path(remote_path)
        self._alist_client.remove_file(remote_path)
        self._record_upload_stat("", remote_path, 0, status="remote_deleted")
        logger.info(f"【手动处理】云端文件已删除: {remote_path}")
        self._notify("云盘上传手动处理完成", f"云端文件已删除：{Path(remote_path).name}")

    def _manual_delete_local_file(self, local_path: str) -> None:
        local_path = unquote_plus(str(local_path or "").strip())
        if not local_path:
            raise Exception("缺少 local 参数")
        if self._match_upload_mapping(local_path) is None:
            raise Exception("local 不在上传映射范围内")
        if not os.path.isfile(local_path):
            logger.info(f"【手动处理】本地文件不存在，视为已删除: {local_path}")
            return
        os.remove(local_path)
        self._record_upload_stat(local_path, self._build_remote_path(local_path) or "", 0, status="local_deleted")
        logger.info(f"【手动处理】本地文件已删除: {local_path}")
        self._notify("云盘上传手动处理完成", f"本地文件已删除：{Path(local_path).name}")

    def _validate_reupload_paths(self, local_path: str, remote_path: str = "") -> Tuple[str, str]:
        local_path = unquote_plus(str(local_path or "").strip())
        remote_path = self._normalize_remote_path_arg(remote_path)
        if not local_path:
            raise Exception("缺少 local 参数")
        if not os.path.isfile(local_path):
            raise Exception(f"本地文件不存在: {local_path}")
        expected_remote = self._build_remote_path(local_path)
        if not expected_remote:
            raise Exception("local 不在上传映射范围内")
        if not remote_path:
            remote_path = expected_remote
        if remote_path != expected_remote:
            raise Exception("remote 与上传映射计算结果不一致，已拒绝")
        return local_path, remote_path

    @staticmethod
    def _normalize_remote_path_arg(path: str) -> str:
        value = str(path or "").strip()
        for _ in range(2):
            decoded = unquote_plus(value)
            if decoded == value:
                break
            value = decoded
        if value and not value.startswith("/"):
            value = "/" + value
        return value

    def _validate_remote_path(self, remote_path: str) -> str:
        value = self._normalize_remote_path_arg(remote_path)
        if not value:
            raise Exception("缺少 remote 参数")
        if self._is_known_remote_path(value):
            return value
        raise Exception("remote 不在已配置的云端路径范围内")

    def _is_known_remote_path(self, remote_path: str) -> bool:
        roots = [cloud for _, cloud in self._upload_mappings or []]
        return any(self._has_path_prefix(Path(remote_path), Path(root)) for root in roots if root)

    @staticmethod
    def _has_path_prefix(full: Path, prefix: Path) -> bool:
        full_parts, prefix_parts = full.parts, prefix.parts
        return len(prefix_parts) <= len(full_parts) and full_parts[:len(prefix_parts)] == prefix_parts

    @staticmethod
    def _normalize_manual_upload_action(value: str) -> str:
        normalized = (value or "none").strip().lower()
        if normalized in {"reupload", "delete_remote", "delete_remote_and_local"}:
            return normalized
        return "none"

    def _safe_run_once(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            logger.error(f"【云盘上传】手动同步异常: {e}", exc_info=True)

    # ============================================================
    # 事件处理（核心触发）
    # ============================================================
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled:
            return
        if self._listener is None:
            return
        records = self._listener.handle(event)
        self._accept_phase1_records(records)

    def _accept_phase1_records(self, records: List[TransferRecord]) -> None:
        records = records or []
        if not records:
            return
        logger.info(f"【云盘上传】Phase 1 完成：记录有效事件 {len(records)} 条")
        threading.Thread(
            target=self._run_records_pipeline,
            args=(records,), daemon=True,
            name="CloudSyncPipeline",
        ).start()

    def _run_records_pipeline(self, records: List[TransferRecord]) -> None:
        """按顺序执行 Phase 2 上传。"""
        if not self._cloud_sync:
            logger.warning("【云盘上传】Phase 2 取消：云同步未初始化")
            return
        with self._sync_lock:
            logger.info("【云盘上传】========== 开始同步（事件触发）==========")
            media_items, skipped = self._media_items_from_records(records)
            logger.info(f"【云盘上传】Phase 2 准备完成：事件记录 {len(records)} 条，媒体文件 {len(media_items)} 个，跳过 {skipped}")
            self._upload_media_items(media_items, incremental=True, label="事件增量上传")
            logger.info("【云盘上传】========== 同步结束（事件触发）==========")

    # ============================================================
    # 全量同步
    # ============================================================
    def run_once(self) -> None:
        if not self._enabled or not self._cloud_sync:
            logger.warning("【云盘上传】未启用或云同步未就绪，跳过全量同步")
            return
        with self._sync_lock:
            self._run_once_locked()

    def _run_once_locked(self) -> None:
        logger.info("【云盘上传】========== 开始全量同步 ==========")
        scan_started = time.time()
        media_items, _ = self._scan_full_media_files()
        self._upload_media_items(media_items, incremental=True, label="全量扫描增量上传")
        upload_batch = getattr(self, "_last_upload_batch", {}) or {}
        if int(upload_batch.get("failed") or 0) == 0:
            self._mark_full_scan_baseline("upload", scan_started)
        else:
            logger.warning("【云盘上传】全量同步上传存在失败，不更新上传增量扫描基准")
        logger.info("【云盘上传】========== 全量同步结束 ==========")

    def run_upload_full_once(self) -> None:
        self._run_upload_once(incremental=False, label="全量上传云端")

    def run_upload_incremental_once(self) -> None:
        self._run_upload_once(incremental=True, label="增量上传云端")

    def _run_upload_once(self, incremental: bool, label: str) -> None:
        if not self._enabled or not self._cloud_sync:
            logger.warning(f"【云盘上传】未启用或云同步未就绪，跳过{label}")
            return
        with self._sync_lock:
            logger.info(f"【云盘上传】========== 开始{label} ==========")
            scan_started = time.time()
            modified_after = self._incremental_baseline_epoch("upload") if incremental else None
            if incremental and not modified_after:
                logger.info("【云盘上传】未找到上传增量扫描基准，本次退回扫描全部候选")
            media_items, _ = self._scan_full_media_files(modified_after=modified_after)
            self._upload_media_items(media_items, incremental=incremental, label=label)
            if not incremental:
                upload_batch = getattr(self, "_last_upload_batch", {}) or {}
                if int(upload_batch.get("failed") or 0) == 0:
                    self._mark_full_scan_baseline("upload", scan_started)
                else:
                    logger.warning("【云盘上传】全量上传存在失败，不更新上传增量扫描基准")
            logger.info(f"【云盘上传】========== {label}结束 ==========")

    # ============================================================
    # Phase 2 上传
    # ============================================================
    def _media_items_from_records(self, records: List[TransferRecord]) -> Tuple[List[Tuple[str, str, Any, Any]], int]:
        media_items: List[Tuple[str, str, Any, Any]] = []
        skipped = 0
        for record in records or []:
            files = self._expand_record_media_files(record)
            if not files:
                if not os.path.exists(record.local_path):
                    logger.warning(f"【云盘上传】Phase 2 跳过：本地路径不存在 {record.local_path}")
                skipped += 1
                continue
            media_items.extend(files)
        return media_items, skipped

    def _expand_record_media_files(self, record: TransferRecord) -> List[Tuple[str, str, Any, Any]]:
        """展开 Phase 1 记录，SSE 目录消息展开为具体媒体文件。"""
        local_path = record.local_path
        result: List[Tuple[str, str, Any, Any]] = []

        if os.path.isdir(local_path):
            for root, dirs, files in os.walk(local_path):
                dirs[:] = [
                    dirname for dirname in dirs
                    if not self._is_excluded_path(os.path.join(root, dirname))
                ]
                for name in files:
                    candidate = os.path.join(root, name)
                    if not self._is_media_file(candidate):
                        continue
                    if self._is_excluded_path(candidate):
                        continue
                    remote_path = self._build_remote_path(candidate)
                    if not remote_path:
                        continue
                    result.append((candidate, remote_path, record.mediainfo, record.meta))
            return result

        if not self._is_media_file(local_path):
            return result
        if os.path.exists(local_path) and self._is_excluded_path(local_path):
            return result
        remote_path = self._build_remote_path(local_path) or record.remote_path
        if remote_path:
            result.append((local_path, remote_path, record.mediainfo, record.meta))
        return result

    def _is_media_file(self, path: str) -> bool:
        ext = Path(path).suffix.lower().lstrip(".")
        return bool(ext and ext in set(self._rmt_mediaext))

    def _is_excluded_path(self, local_path: str) -> bool:
        spec = self._exclude_spec
        if not spec:
            return False
        for root in self._local_media_roots or self._parse_path_lines(self._local_media_path):
            try:
                rel = str(Path(local_path).relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if spec.match_file(rel):
                return True
        return spec.match_file(str(Path(local_path)).replace("\\", "/"))

    def _scan_full_media_files(
        self,
        modified_after: Optional[float] = None,
    ) -> Tuple[List[Tuple[str, str, Any, Any]], int]:
        local_roots = self._local_media_roots or self._parse_path_lines(self._local_media_path)
        if not local_roots:
            logger.warning("【云盘上传】未配置本地媒体路径，跳过全量扫描")
            return [], 0

        media_items: List[Tuple[str, str, Any, Any]] = []
        media_exts = set(self._rmt_mediaext)
        exclude_spec = self._exclude_spec
        skipped = 0
        skipped_by_mtime = 0
        mtime_threshold = self._scan_mtime_threshold(modified_after)
        scan_checked = 0
        last_scan_log = time.time()

        logger.info(f"【云盘上传】扫描根目录: {local_roots}")
        logger.info(f"【云盘上传】上传映射: {self._upload_mappings}")
        if mtime_threshold is not None:
            logger.info("【云盘上传】增量扫描基准: %s", self._format_epoch(mtime_threshold))

        for local_root in local_roots:
            root_files = 0
            for root, _dirs, files in os.walk(local_root):
                for name in files:
                    scan_checked += 1
                    local_path = os.path.join(root, name)
                    ext = Path(name).suffix.lower().lstrip(".")
                    if ext not in media_exts:
                        skipped += 1
                        continue
                    if mtime_threshold is not None:
                        try:
                            if os.path.getmtime(local_path) <= mtime_threshold:
                                skipped_by_mtime += 1
                                continue
                        except OSError as e:
                            logger.debug(f"【云盘上传】读取文件 mtime 失败，跳过: {local_path} ({e})")
                            skipped += 1
                            continue
                    if exclude_spec and self._is_excluded_path(local_path):
                        skipped += 1
                        continue
                    remote_path = self._build_remote_path(local_path)
                    if not remote_path:
                        skipped += 1
                        continue
                    media_items.append((local_path, remote_path, None, None))
                    root_files += 1
                    now = time.time()
                    if now - last_scan_log >= BULK_PROGRESS_LOG_INTERVAL:
                        logger.info(
                            "【云盘上传】扫描中：根=%s，已检查 %d，候选 %d，mtime过滤 %d，其他跳过 %d，当前目录=%s",
                            local_root, scan_checked, len(media_items),
                            skipped_by_mtime, skipped, root,
                        )
                        last_scan_log = now
            logger.info(f"【云盘上传】已扫描根目录: {local_root}（候选媒体 {root_files}）")

        if mtime_threshold is not None:
            logger.info(f"【云盘上传】增量扫描完成: 候选 {len(media_items)}，mtime 过滤 {skipped_by_mtime}，其他跳过 {skipped}")
        else:
            logger.info(f"【云盘上传】全量扫描完成: 候选 {len(media_items)}，跳过 {skipped}")
        return media_items, skipped

    def _upload_media_items(self, media_items: List[Tuple[str, str, Any, Any]],
                            incremental: bool, label: str) -> None:
        if not self._cloud_sync:
            logger.warning(f"【云盘上传】{label} 取消：云同步未初始化")
            return

        queued = 0
        skipped = 0
        checked = 0
        failed_samples: List[str] = []
        detail_logging = len(media_items) <= BULK_DETAIL_LOG_THRESHOLD or "事件" in label
        last_decision_log = time.time()
        logger.info(f"【云盘上传】Phase 2 开始：{label}，候选 {len(media_items)} 个")

        if not detail_logging:
            logger.info(
                "【云盘上传】Phase 2 批量日志模式：候选超过 %d，单文件入队/跳过日志降级为 debug",
                BULK_DETAIL_LOG_THRESHOLD,
            )
        self._cloud_sync.prepare_batch(label=label)

        # 小批量（事件触发）不做全量预加载，单文件按需查询远端目录
        # 大批量（全量同步）预加载所有远端目录，避免逐文件 list_dir 网络开销
        is_small_batch = len(media_items) <= 20
        if is_small_batch:
            logger.info(f"【云盘上传】小批量模式（{len(media_items)} 个），跳过云端目录预加载，按文件单查远端")
            remote_cache = {}
        elif media_items:
            remote_roots = [cloud for _, cloud in self._upload_mappings if cloud]
            logger.info(f"【云盘上传】预加载云端目录列表: {remote_roots}")
            remote_cache = self._cloud_sync.preload_remote_dirs(remote_roots)
            logger.info(f"【云盘上传】预加载完成：缓存 {len(remote_cache)} 个远端目录")
        else:
            remote_cache = {}

        for local_path, remote_path, mediainfo, meta in media_items:
            checked += 1
            try:
                need = (self._cloud_sync.need_upload(remote_path)
                        if is_small_batch
                        else self._cloud_sync.need_upload_cached(remote_path, remote_cache))
                if not need:
                    message = f"【云盘上传】Phase 2 跳过：云端已存在同名文件 {remote_path}"
                    if detail_logging:
                        logger.info(message)
                    else:
                        logger.debug(message)
                    skipped += 1
                    continue
            except Exception as e:
                logger.warning(f"【云盘上传】Phase 2 云端同名判定异常，按需上传: {e}")
            self._cloud_sync.enqueue_file(
                local_path, remote_path, mediainfo, meta,
                log_detail=detail_logging,
            )
            queued += 1
            now = time.time()
            if not detail_logging and (
                now - last_decision_log >= BULK_PROGRESS_LOG_INTERVAL
                or checked == len(media_items)
            ):
                logger.info(
                    "【云盘上传】Phase 2 判定进度：%s，已处理 %d/%d，入队 %d，跳过 %d",
                    label, checked, len(media_items), queued, skipped,
                )
                last_decision_log = now

        self._cloud_sync.mark_scan_finish()
        logger.info(f"【云盘上传】Phase 2 扫描完成：入队 {queued}，跳过 {skipped}")
        logger.info("【云盘上传】等待上传完成...")
        finished = self._cloud_sync.wait_for_batch(
            progress_label=label,
            progress_interval=BULK_PROGRESS_LOG_INTERVAL if not detail_logging else 30.0,
        )

        upload_ok = 0
        upload_skip = 0
        upload_fail = 0
        for item in finished:
            if item.status == TASK_SUCCEEDED:
                upload_ok += 1
                self._record_upload_stat(item.local_path, item.remote_path, item.file_size or 0, status="uploaded")
            elif item.status == TASK_SKIPPED:
                upload_skip += 1
            else:
                upload_fail += 1
                if len(failed_samples) < 10:
                    failed_samples.append(
                        f"{item.remote_path}: {item.err_msg or item.status or '未知错误'}"
                    )

        self._last_upload_batch = {
            "success": upload_ok,
            "skipped": upload_skip + skipped,
            "failed": upload_fail,
            "queued": queued,
            "candidates": len(media_items),
        }
        logger.info(
            f"【云盘上传】上传阶段结束：成功 {upload_ok}，跳过 {upload_skip + skipped}，失败 {upload_fail}"
        )
        if failed_samples:
            logger.warning("【云盘上传】上传失败样本（最多10条）：%s", "；".join(failed_samples))

    @staticmethod
    def _format_epoch(epoch: Optional[float]) -> str:
        if not epoch:
            return ""
        try:
            from datetime import datetime
            return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    @staticmethod
    def _scan_mtime_threshold(modified_after: Optional[float]) -> Optional[float]:
        if not modified_after:
            return None
        try:
            value = float(modified_after)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return max(0.0, value - 1.0)

    def _incremental_baseline_epoch(self, kind: str) -> Optional[float]:
        stats = getattr(self, "_stats", None) or self._load_stats()
        key = f"last_full_{kind}_scan_epoch"
        try:
            value = float(stats.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else None

    def _mark_full_scan_baseline(self, kind: str, epoch: float) -> None:
        if not self._stats:
            self._stats = self._load_stats()
        epoch_key = f"last_full_{kind}_scan_epoch"
        time_key = f"last_full_{kind}_scan_time"
        self._stats[epoch_key] = float(epoch)
        self._stats[time_key] = self._format_epoch(epoch)
        self._save_stats()
        logger.info(f"【云盘上传】已更新上传增量扫描基准: {self._stats[time_key]}")

    # ============================================================
    # 路径映射
    # ============================================================
    @staticmethod
    def _parse_path_lines(raw: str) -> List[str]:
        return [line.strip().rstrip("/") for line in (raw or "").splitlines() if line.strip()]

    @classmethod
    def _normalize_upload_path_mappings(cls, config: Dict[str, Any]) -> str:
        raw = config.get("upload_path_mappings")
        if raw:
            return str(raw)
        return DEFAULT_UPLOAD_PATH_MAPPINGS

    @classmethod
    def _parse_path_mappings(cls, raw: str) -> List[Tuple[str, str]]:
        mappings: List[Tuple[str, str]] = []
        seen = set()
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or "#" not in line:
                continue
            local, cloud = line.split("#", 1)
            local = local.strip().rstrip("/")
            cloud = cloud.strip().rstrip("/")
            if not local or not cloud:
                continue
            key = (local, cloud)
            if key in seen:
                continue
            seen.add(key)
            mappings.append(key)
        return mappings

    def _build_remote_path(self, local_path: str) -> Optional[str]:
        matched = self._match_upload_mapping(local_path)
        if matched is None:
            return None
        _, cloud_root, rel = matched
        rel_str = str(rel).replace("\\", "/").lstrip("/")
        return f"{cloud_root.rstrip('/')}/{rel_str}".rstrip("/")

    def _match_upload_mapping(self, local_path: str) -> Optional[Tuple[str, str, Path]]:
        mappings = (
            getattr(self, "_upload_mappings", None)
            or self._parse_path_mappings(getattr(self, "_upload_path_mappings", ""))
        )
        return self._match_path_mapping(local_path, mappings)

    @classmethod
    def _match_path_mapping(cls, path: str, mappings: List[Tuple[str, str]]) -> Optional[Tuple[str, str, Path]]:
        value = Path(path)
        for source_root, target_root in mappings or []:
            root = Path(source_root)
            try:
                return source_root, target_root, value.relative_to(root)
            except ValueError:
                if cls._has_path_prefix(value, root):
                    return source_root, target_root, Path(*value.parts[len(root.parts):])
        return None

    @staticmethod
    def _normalize_sync_mode(value: str) -> str:
        normalized = (value or "copy").strip().lower()
        if normalized in ("move", "移动"):
            return "move"
        return "copy"

    @staticmethod
    def _build_exclude_spec(raw: str) -> Optional[pathspec.PathSpec]:
        lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
        if not lines:
            return None
        try:
            return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, lines)
        except Exception as e:
            logger.warning(f"【云盘上传】排除规则解析失败，忽略: {e}")
            return None

    # ============================================================
    # 统计
    # ============================================================
    def _load_stats(self) -> Dict[str, Any]:
        try:
            data = self.get_data("stats")
            if isinstance(data, dict):
                data.setdefault("upload_count", 0)
                data.setdefault("last_upload_time", "")
                data.setdefault("recent_uploads", [])
                data.setdefault("last_full_upload_scan_epoch", 0)
                data.setdefault("last_full_upload_scan_time", "")
                return data
        except Exception as e:
            logger.debug(f"【云盘上传】读取统计失败: {e}")
        return {
            "upload_count": 0, "last_upload_time": "", "recent_uploads": [],
            "last_full_upload_scan_epoch": 0, "last_full_upload_scan_time": "",
        }

    def _save_stats(self) -> None:
        try:
            self.save_data("stats", self._stats)
        except Exception as e:
            logger.debug(f"【云盘上传】保存统计失败: {e}")

    def _record_upload_stat(self, local_path: str, remote_path: str,
                            size: int = 0, status: str = "uploaded") -> None:
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self._stats:
            self._stats = self._load_stats()
        if status == "uploaded":
            self._stats["upload_count"] = int(self._stats.get("upload_count") or 0) + 1
        self._stats["last_upload_time"] = now
        recent = list(self._stats.get("recent_uploads") or [])
        display_name = Path(local_path).name if local_path else Path(remote_path).name

        if status in ("remote_deleted", "local_deleted"):
            updated = [i for i, item in enumerate(recent) if item.get("remote") == remote_path]
            if updated:
                idx = updated[0]
                recent[idx]["status"] = status
                recent[idx]["time"] = now
                if local_path:
                    recent[idx]["local"] = local_path
                self._stats["recent_uploads"] = recent[:20]
                self._save_stats()
                return
            recent.insert(0, {
                "name": display_name, "local": local_path,
                "remote": remote_path, "size": size,
                "time": now, "status": status,
            })
            self._stats["recent_uploads"] = recent[:20]
        else:
            should_insert = status not in ("uploaded", "skipped") or not any(
                item.get("remote") == remote_path for item in recent
            )
            if should_insert:
                recent.insert(0, {
                    "name": display_name, "local": local_path,
                    "remote": remote_path, "size": size,
                    "time": now, "status": status,
                })
                self._stats["recent_uploads"] = recent[:20]
        self._save_stats()

    # ============================================================
    # 通知
    # ============================================================
    def _notify(self, title: str, text: str) -> None:
        try:
            from app.schemas import NotificationType
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as e:
            logger.debug(f"【云盘上传】通知发送失败: {e}")
        try:
            self.systemmessage.put(message=text, role="plugin", title=title)
        except Exception as e:
            logger.debug(f"【云盘上传】系统消息写入失败: {e}")

    # ============================================================
    # 配置持久化与表单
    # ============================================================
    def _update_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "moviepilot_address": self._moviepilot_address,
            "cloud_storage_type": self._cloud_storage_type,
            "alist_url": self._alist_url,
            "alist_token": self._alist_token,
            "upload_path_mappings": self._upload_path_mappings,
            "sync_mode": self._sync_mode,
            "exclude_patterns": self._exclude_patterns,
            "event_filters": self._event_filters,
            "notify_enabled": self._notify_enabled,
            "rmt_mediaext": ",".join(self._rmt_mediaext) if self._rmt_mediaext else DEFAULT_MEDIA_EXTS,
            "upload_concurrency": self._upload_concurrency,
            "once_sync": self._once_sync,
            "once_upload_full": self._once_upload_full,
            "once_upload_incremental": self._once_upload_incremental,
            "sse_enabled": self._sse_enabled,
            "manual_upload_action": self._manual_upload_action,
            "manual_upload_target": self._manual_upload_target,
            "manual_confirm": self._manual_confirm,
            "manual_execute": self._manual_execute,
        })

    def _diagnostic_snapshot(self, probe: bool = False) -> Dict[str, Any]:
        local_roots = self._local_media_roots or self._parse_path_lines(self._local_media_path)
        stats = getattr(self, "_stats", None) or {}
        if not stats and not isinstance(self, type):
            stats = self._load_stats()
        sample_local = self._sample_local_path(local_roots)
        sample_remote = self._build_remote_path(sample_local) if sample_local else ""
        data: Dict[str, Any] = {
            "enabled": self._enabled,
            "phase_order": ["listen", "sync"],
            "config": {
                "moviepilot_address": self._moviepilot_address,
                "cloud_storage_type": self._cloud_storage_type,
                "alist_url": self._alist_url,
                "alist_token": self._mask_secret(self._alist_token),
                "alist_token_configured": bool(self._alist_token),
                "upload_path_mappings": self._upload_path_mappings,
                "upload_mappings": [
                    {"local": local, "cloud": cloud}
                    for local, cloud in self._upload_mappings
                ],
                "local_media_roots": local_roots,
                "sync_mode": self._sync_mode,
                "upload_concurrency": self._upload_concurrency,
                "media_exts": self._rmt_mediaext,
                "event_filters": self._event_filter_prefixes,
                "sse_enabled": self._sse_enabled,
            },
            "modules": {
                "sse_listener": bool(self._sse_listener),
                "alist_client": bool(self._alist_client),
                "cloud_sync": bool(self._cloud_sync),
            },
            "local_roots": [
                {"path": root, "exists": os.path.exists(root), "is_dir": os.path.isdir(root)}
                for root in local_roots
            ],
            "mapping_sample": {
                "local": sample_local,
                "remote": sample_remote,
            },
            "stats": stats,
        }
        if self._cloud_sync:
            data["sync"] = self._cloud_sync.get_status()
        if probe:
            data["alist_probe"] = self._probe_alist()
        return data

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}...{value[-4:]}"

    @staticmethod
    def _sample_local_path(local_roots: List[str]) -> str:
        if not local_roots:
            return ""
        return str(Path(local_roots[0]) / "example.mkv")

    def _probe_alist(self) -> Dict[str, Any]:
        if not self._alist_client:
            return {"ok": False, "message": "AList 客户端未初始化"}
        result: Dict[str, Any] = {"ok": False}
        try:
            user = self._alist_client.verify()
            result["ok"] = True
            result["user"] = user
        except Exception as e:
            result["message"] = f"Token 校验失败: {e}"
            return result

        sample_remote = ""
        try:
            local_roots = self._local_media_roots or self._parse_path_lines(self._local_media_path)
            sample_local = self._sample_local_path(local_roots)
            sample_remote = self._build_remote_path(sample_local) if sample_local else ""
        except Exception:
            sample_remote = ""
        if sample_remote:
            try:
                info = self._alist_client.fs_get(sample_remote) or {}
                result["fs_get"] = {
                    "path": sample_remote,
                    "name": info.get("name") or "",
                    "is_dir": bool(info.get("is_dir")),
                    "size": info.get("size"),
                    "has_raw_url": bool(info.get("raw_url")),
                    "has_sign": bool(info.get("sign")),
                }
            except Exception as e:
                result["fs_get"] = {"path": sample_remote, "error": str(e)[:200]}
        return result

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页面（3 Tab：基础 / 云端设置 / 同步与过滤）。"""
        return [
                    {
                        "component": "VTabs",
                        "props": {
                            "model": "_tabs", "color": "primary", "class": "mb-4",
                        },
                        "content": [
                            {"component": "VTab", "props": {"value": "base"}, "text": "基础设置"},
                            {"component": "VTab", "props": {"value": "cloud"}, "text": "云端设置"},
                            {"component": "VTab", "props": {"value": "sync"}, "text": "同步与过滤"},
                        ],
                    },
                    {
                        "component": "VWindow",
                        "props": {"model": "_tabs"},
                        "content": [
                            # ---- Tab: 基础 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "base"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "基础设置"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "enabled", "label": "启用插件",
                                                         "hint": "监听 MP 整理入库事件，自动上传到云盘",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "notify_enabled", "label": "任务完成通知",
                                                         "hint": "通过 MP 通知同步结果",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "upload_concurrency", "label": "上传并发数",
                                                         "placeholder": "3", "type": "number",
                                                         "hint": "并发上传文件数",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "sse_enabled", "label": "SSE 事件监听",
                                                         "hint": "监听 MP 消息流补全事件",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                         {"component": "div", "props": {"class": "text-h6 mb-4 mt-6"}, "text": "立即同步"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "once_upload_full", "label": "全量上传云端",
                                                         "hint": "扫描全部候选，云端同名文件直接跳过",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "once_upload_incremental", "label": "增量上传云端",
                                                         "hint": "有全量基准时只扫描新增/修改文件",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                     ]},
                                ],
                            },
                            # ---- Tab: 云端设置 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "cloud"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "AList / OpenList 连接"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 6},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "alist_url", "label": "AList/OpenList 地址",
                                                         "placeholder": DEFAULT_ALIST_URL,
                                                         "hint": "http://ip:端口",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 6},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "alist_token", "label": "AList/OpenList Token",
                                                         "hint": "管理后台的 Token",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                     ]},
                                ],
                            },
                            # ---- Tab: 同步与过滤 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "sync"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "路径映射"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-3"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12},
                                                     "content": [{"component": "VTextarea", "props": {
                                                         "model": "upload_path_mappings",
                                                         "label": "上传映射（本地路径#云端路径）",
                                                         "rows": 3,
                                                         "placeholder": DEFAULT_UPLOAD_PATH_MAPPINGS,
                                                         "hint": "每行一个：本地媒体目录#AList 云端目录",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                         {"component": "div", "props": {"class": "text-h6 mb-4 mt-6"}, "text": "同步模式"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSelect", "props": {
                                                         "model": "sync_mode", "label": "同步模式",
                                                         "items": [
                                                             {"title": "复制（上传后保留本地源文件）", "value": "copy"},
                                                             {"title": "移动（上传成功后删除本地源文件）", "value": "move"},
                                                         ],
                                                         "hint": "不自动删除云端文件",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 8},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "rmt_mediaext", "label": "可处理媒体扩展名",
                                                         "placeholder": "mp4,mkv,ts,iso,...",
                                                         "hint": "逗号分隔",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-3"},
                                             "content": [{
                                                 "component": "VCol", "props": {"cols": 12},
                                                 "content": [{
                                                     "component": "VTextarea", "props": {
                                                         "model": "exclude_patterns",
                                                         "label": "排除规则（gitignore 语法，一行一条）",
                                                         "rows": 3, "placeholder": DEFAULT_EXCLUDE_PATTERNS,
                                                         "hint": "命中规则的文件不上传",
                                                         "persistent-hint": False,
                                                     },
                                                 }],
                                             }],
                                         },
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-3"},
                                             "content": [{
                                                 "component": "VCol", "props": {"cols": 12},
                                                 "content": [{
                                                     "component": "VTextarea", "props": {
                                                         "model": "event_filters",
                                                         "label": "事件路径过滤（留空处理全部）",
                                                         "rows": 2, "placeholder": DEFAULT_EVENT_FILTERS,
                                                         "hint": "只处理这些本地路径前缀下的整理事件",
                                                         "persistent-hint": False,
                                                     },
                                                 }],
                                             }],
                                         },
                                     ]},
                                ],
                            },
                        ],
                    },
        ], {
            "_tabs": "base",
            "enabled": False,
            "once_sync": False,
            "once_upload_full": False,
            "once_upload_incremental": False,
            "notify_enabled": True,
            "upload_concurrency": 3,
            "moviepilot_address": DEFAULT_MOVIEPILOT_ADDRESS,
            "sse_enabled": False,
            "manual_upload_action": "none",
            "manual_upload_target": "",
            "manual_confirm": False,
            "manual_execute": False,
            "cloud_storage_type": "alist",
            "alist_url": DEFAULT_ALIST_URL,
            "alist_token": "",
            "upload_path_mappings": DEFAULT_UPLOAD_PATH_MAPPINGS,
            "sync_mode": "copy",
            "rmt_mediaext": DEFAULT_MEDIA_EXTS,
            "exclude_patterns": DEFAULT_EXCLUDE_PATTERNS,
            "event_filters": DEFAULT_EVENT_FILTERS,
        }
