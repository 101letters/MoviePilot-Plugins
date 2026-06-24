"""CloudStrmHelper — 云端STRM整理助手 MoviePilot V2 插件

链路：Phase 1 监听整理完成事件 → Phase 2 上传 AList → Phase 3 生成 STRM → Phase 4 刷新 Emby。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

import pathspec
from apscheduler.schedulers.background import BackgroundScheduler
from cachetools import TTLCache
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .cloud_sync import TASK_SKIPPED, TASK_SUCCEEDED, AlistClient, CloudSync
from .proxy_handler import DirectLink, ProxyHandler
from .sse_listener import MoviePilotSseListener
from .strm_generator import StrmGenerator
from .transfer_listener import TransferListener, TransferRecord


# 默认可处理媒体扩展名（与 p123strmhelper 一致）
DEFAULT_MEDIA_EXTS = (
    "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v"
)
DEFAULT_MOVIEPILOT_ADDRESS = "http://192.168.31.6:3000"
DEFAULT_ALIST_URL = "http://192.168.31.6:5244/"
DEFAULT_ALIST_TARGET_PATH = "/123云盘/影视/华语电影"
DEFAULT_LOCAL_MEDIA_PATH = "/media/movies\n/media/tv"
DEFAULT_STRM_OUTPUT_PATH = "/strm/test/华语电影"
DEFAULT_LOCAL_STRM_PATHS = "/media/movies#/strm/test/华语电影\n/media/tv#/strm/test/电视剧"
DEFAULT_UPLOAD_PATH_MAPPINGS = (
    "/media/movies#/123云盘/影视/华语电影\n"
    "/media/tv#/123云盘/影视/电视剧"
)
DEFAULT_STRM_PATH_MAPPINGS = (
    "/123云盘/影视/华语电影#/strm/test/华语电影\n"
    "/123云盘/影视/电视剧#/strm/test/电视剧"
)
DEFAULT_EXCLUDE_PATTERNS = "*.tmp\n**/.DS_Store\n/sample/**"
DEFAULT_EVENT_FILTERS = "/media/movies\n/media/tv"
DEFAULT_PATH_MAPPING = "/media#/data"
DEFAULT_EMBY_SERVER_URL = "http://192.168.31.6:8096"
DEFAULT_EMBY_PROXY_HOST = "0.0.0.0"
DEFAULT_EMBY_PROXY_PORT = 8095
DEFAULT_STRM_URL_MODE = "alist_direct"


class ManualActionParams(BaseModel):
    """首页列表内单条操作的 POST body 参数。

    前端 PageRender 对 POST 请求会把 events.click.params 作为请求 body 发送
    (api.post(api_path, params))，端点必须用 Pydantic model 接收；
    散落的 query 参数收不到 body —— 这是 v1.5.3 操作按钮“能点不执行”的根因。
    """
    action: str = Field("", description="reupload/delete_remote/delete_remote_and_local/regenerate_strm/delete_strm")
    local: str = Field("", description="本地源文件路径（reupload/delete_remote_and_local 需要）")
    remote: str = Field("", description="云端路径（所有动作都需要）")
    strm: str = Field("", description="STRM 文件路径（regenerate_strm/delete_strm 需要）")

    def to_payload(self) -> Dict[str, Any]:
        """转换为 _manual_action_worker 期望的 action dict。"""
        action = (self.action or "").strip().lower()
        if action in ("regenerate_strm", "delete_strm"):
            return {"kind": "strm", "action": action,
                    "target": CloudStrmHelper._manual_entry_value(strm=self.strm or "", remote=self.remote or "")}
        return {"kind": "upload", "action": action,
                "target": CloudStrmHelper._manual_entry_value(local=self.local or "", remote=self.remote or "")}

    def validate_action(self) -> Optional[str]:
        """返回错误信息（None 表示通过）。"""
        action = (self.action or "").strip().lower()
        if action not in {"reupload", "delete_remote", "delete_remote_and_local", "regenerate_strm", "delete_strm"}:
            return f"未知动作: {action}"
        if not (self.remote or "").strip():
            return "缺少 remote 参数"
        if action in ("regenerate_strm", "delete_strm"):
            if not (self.strm or "").strip():
                return "缺少 strm 参数"
        elif action in ("reupload", "delete_remote_and_local"):
            if not (self.local or "").strip():
                return "缺少 local 参数"
        return None


class CloudStrmHelper(_PluginBase):
    """云端STRM整理助手。"""

    # ---- 插件元数据（类属性；V2 索引同时写入仓库根 package.v2.json，version 须一致）----
    plugin_name = "云端STRM整理助手"
    plugin_desc = "整理入库自动复制到AList并生成STRM，支持轻量/Emby前置302直链播放"
    # 图标：引用仓库 icons 目录下的图标文件（URL 形式，与官方插件一致）
    plugin_icon = "https://raw.githubusercontent.com/101letters/MoviePilot-Plugins/main/icons/cloudstrmhelper.png"
    plugin_version = "1.5.5"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "cloudstrmhelper_"
    plugin_order = 99
    auth_level = 1

    # ---- 私有状态（类属性，stop 时复位）----
    _enabled = False
    _moviepilot_address = ""
    _cloud_storage_type = "alist"
    _alist_url = ""
    _alist_token = ""
    _alist_target_path = ""
    _upload_path_mappings = ""
    _upload_mappings: List[Tuple[str, str]] = []
    _strm_path_mappings = ""
    _strm_mappings: List[Tuple[str, str]] = []
    _strm_path_mappings_explicit = False
    _local_media_path = ""
    _local_media_roots: List[str] = []
    _local_strm_paths = ""
    _local_strm_mappings: List[Tuple[str, str]] = []
    _strm_output_path = ""
    _sync_mode = "copy"
    _overwrite_mode = "never"
    _exclude_patterns = ""
    _exclude_spec: Optional[pathspec.PathSpec] = None
    _event_filters = ""
    _event_filter_prefixes: List[str] = []
    _refresh_enabled = True
    _mediaservers: List[str] = []
    _transfer_mp_mediaserver_paths = ""
    _notify_enabled = True
    _rmt_mediaext: List[str] = []
    _upload_concurrency = 3
    _once_sync = False
    # 播放入口 / 302 可靠性配置
    _strm_url_mode = DEFAULT_STRM_URL_MODE
    _resolve_final_url = True
    _direct_link_mode = "prefer_raw_url"
    _redirect_cache_ttl = 120
    _head_probe_mode = "ok"
    _sse_enabled = False
    _emby_proxy_enabled = False
    _emby_server_url = ""
    _emby_proxy_host = DEFAULT_EMBY_PROXY_HOST
    _emby_proxy_port = DEFAULT_EMBY_PROXY_PORT
    _manual_upload_action = "none"
    _manual_upload_target = ""
    _manual_strm_target = ""
    _manual_confirm = False
    _manual_execute = False
    _pending_manual_action: Optional[Dict[str, Any]] = None

    _scheduler: Optional[BackgroundScheduler] = None
    _alist_client = None
    _cloud_sync: Optional[CloudSync] = None
    _strm_gen: Optional[StrmGenerator] = None
    _proxy: Optional[ProxyHandler] = None
    _listener: Optional[TransferListener] = None
    _sse_listener: Optional[MoviePilotSseListener] = None
    _emby_proxy_server = None
    _emby_proxy_thread: Optional[threading.Thread] = None
    _sync_lock = threading.Lock()
    _stats: Dict[str, Any] = {}

    # 302 解析缓存：按 (path, ua-hash, mode) 缓存最终 URL；TTL 可配置，配置变更时重建
    _redirect_cache: TTLCache = TTLCache(maxsize=512, ttl=120)
    # 失败负缓存：坏路径短 TTL，避免疯狂请求
    _redirect_error_cache: TTLCache = TTLCache(maxsize=256, ttl=30)
    # in-flight 请求合并：同一 cache key 解析时加锁，避免并发重复请求 AList
    _resolve_locks: Dict[str, threading.Lock] = {}
    _resolve_locks_guard = threading.Lock()

    # ============================================================
    # 生命周期
    # ============================================================
    def init_plugin(self, config: Dict[str, Any] = None) -> None:
        """初始化：解析配置 → 持久化 → 先停 → 重建各模块。"""
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._moviepilot_address = (config.get("moviepilot_address") or DEFAULT_MOVIEPILOT_ADDRESS).strip()
            self._cloud_storage_type = "alist"
            self._alist_url = (config.get("alist_url") or DEFAULT_ALIST_URL).strip()
            self._alist_token = (config.get("alist_token") or "").strip()
            self._alist_target_path = (config.get("alist_target_path") or DEFAULT_ALIST_TARGET_PATH).strip()
            self._upload_path_mappings = self._normalize_upload_path_mappings(config).strip()
            self._strm_path_mappings = self._normalize_strm_path_mappings(config).strip()
            self._strm_path_mappings_explicit = bool((config.get("strm_path_mappings") or "").strip())
            self._local_strm_paths = self._normalize_local_strm_paths(config).strip()
            self._sync_mode = self._normalize_sync_mode(config.get("sync_mode") or "copy")
            self._overwrite_mode = self._normalize_overwrite_mode(config.get("overwrite_mode") or "never")
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
            self._refresh_enabled = bool(config.get("transfer_monitor_media_server_refresh_enabled", True))
            self._mediaservers = config.get("transfer_monitor_mediaservers") or []
            self._transfer_mp_mediaserver_paths = config.get("transfer_mp_mediaserver_paths") or DEFAULT_PATH_MAPPING
            self._notify_enabled = bool(config.get("notify_enabled", True))
            # 扩展名
            ext_str = (config.get("rmt_mediaext") or DEFAULT_MEDIA_EXTS)
            self._rmt_mediaext = [e.strip().lower().lstrip(".") for e in ext_str.split(",") if e.strip()]
            try:
                self._upload_concurrency = max(1, int(config.get("upload_concurrency") or 3))
            except (TypeError, ValueError):
                self._upload_concurrency = 3
            self._once_sync = bool(config.get("once_sync", False))
            # 播放入口 / 302 可靠性
            self._strm_url_mode = self._normalize_strm_url_mode(
                config.get("strm_url_mode") or DEFAULT_STRM_URL_MODE)
            self._resolve_final_url = bool(config.get("resolve_final_url", True))
            self._direct_link_mode = self._normalize_direct_link_mode(
                config.get("direct_link_mode") or "prefer_raw_url")
            try:
                self._redirect_cache_ttl = max(0, int(config.get("redirect_cache_ttl") or 120))
            except (TypeError, ValueError):
                self._redirect_cache_ttl = 120
            self._head_probe_mode = self._normalize_head_probe_mode(
                config.get("head_probe_mode") or "ok")
            self._sse_enabled = bool(config.get("sse_enabled", False))
            self._emby_proxy_enabled = bool(config.get("emby_proxy_enabled", False))
            self._emby_server_url = (config.get("emby_server_url") or "").strip().rstrip("/")
            self._emby_proxy_host = (
                config.get("emby_proxy_host") or DEFAULT_EMBY_PROXY_HOST
            ).strip()
            try:
                self._emby_proxy_port = max(1, int(config.get("emby_proxy_port") or DEFAULT_EMBY_PROXY_PORT))
            except (TypeError, ValueError):
                self._emby_proxy_port = DEFAULT_EMBY_PROXY_PORT
            # 手动处理字段：UI 已改为首页列表内单条操作（/manual_action API），
            # 这里仍读取旧配置字段以兼容历史持久化，但不再从配置保存派发任何动作。
            self._manual_upload_action = self._normalize_manual_upload_action(
                config.get("manual_upload_action") or "none")
            self._manual_upload_target = config.get("manual_upload_target") or ""
            self._manual_strm_target = config.get("manual_strm_target") or ""
            self._manual_confirm = bool(config.get("manual_confirm", False))
            self._manual_execute = bool(config.get("manual_execute", False))
            self._pending_manual_action = None
            self._update_config()

        # 配置变更后重建 302 缓存（TTL 可配置）
        self._redirect_cache = TTLCache(maxsize=512, ttl=self._redirect_cache_ttl)
        self._redirect_error_cache = TTLCache(maxsize=256, ttl=30)
        self._resolve_locks = {}
        # 派生：排除规则 PathSpec、事件前缀
        self._exclude_spec = self._build_exclude_spec(self._exclude_patterns)
        self._upload_mappings = self._parse_path_mappings(self._upload_path_mappings)
        self._strm_mappings = self._parse_path_mappings(self._strm_path_mappings)
        self._local_strm_mappings = self._parse_local_strm_mappings(self._local_strm_paths)
        self._local_media_roots = (
            [local for local, _ in self._upload_mappings]
            or [local for local, _ in self._local_strm_mappings]
        )
        self._local_media_path = "\n".join(self._local_media_roots)
        strm_roots = self._unique_paths(
            [strm for _, strm in self._strm_mappings]
            or [strm for _, strm in self._local_strm_mappings]
        )
        self._strm_output_path = "\n".join(strm_roots)
        self._event_filter_prefixes = [
            p.strip() for p in (self._event_filters or "").splitlines() if p.strip()
        ]
        self._stats = self._load_stats()

        # 先停旧资源
        self.stop_service()

        if not self._enabled:
            return

        # 校验必填
        proxy_only = bool(self._emby_proxy_enabled and self._emby_server_url)
        if not self._upload_mappings and not proxy_only:
            logger.warning("【云端STRM】未配置上传路径映射，插件不启动")
            return
        if not self._strm_mappings and not self._local_strm_mappings and not proxy_only:
            logger.warning("【云端STRM】未配置 STRM 路径映射，插件不启动")
            return

        # 构建云端客户端
        try:
            self._alist_client = self._build_cloud_client()
        except Exception as e:
            logger.error(f"【云端STRM】云端客户端初始化失败: {e}", exc_info=True)
            self._alist_client = None

        # 构建各模块
        self._proxy = ProxyHandler(
            self._alist_client,
            resolve_final_url=self._resolve_final_url,
            direct_link_mode=self._direct_link_mode,
        ) if self._alist_client else None
        self._strm_gen = StrmGenerator(self)
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

        logger.info("【云端STRM】插件已启动：storage=%s, sync=%s, 并发=%d, SSE=%s",
                    self._cloud_storage_type, self._sync_mode, self._upload_concurrency,
                    "on" if self._sse_enabled else "off")

        self._start_emby_proxy_service()

        # 一次性全量同步
        if self._once_sync:
            self._once_sync = False
            self._update_config()
            self._schedule_once_sync()

    def _build_cloud_client(self):
        """根据 cloud_storage_type 构建云端客户端。"""
        st = self._cloud_storage_type
        if st == "alist":
            if not self._alist_url or not self._alist_token:
                raise Exception("AList 类型需配置 alist_url 和 alist_token")
            return AlistClient(self._alist_url, self._alist_token)
        else:
            raise Exception(f"未知云端存储类型: {st}")

    def _start_emby_proxy_service(self) -> None:
        """启动 qmediasync 风格的 Emby 302 前置代理。"""
        if not self._emby_proxy_enabled:
            return
        if not self._emby_server_url:
            logger.warning("【Emby302代理】未配置 Emby 原始地址，代理不启动")
            return
        try:
            import uvicorn
            from .emby_proxy import create_emby_proxy_app

            app = create_emby_proxy_app(self, self._emby_server_url)
            config = uvicorn.Config(
                app=app,
                host=self._emby_proxy_host,
                port=self._emby_proxy_port,
                log_config=None,
                access_log=False,
            )
            self._emby_proxy_server = uvicorn.Server(config)
            self._emby_proxy_thread = threading.Thread(
                target=self._emby_proxy_server.run,
                daemon=True,
                name="CloudStrmEmbyProxy",
            )
            self._emby_proxy_thread.start()
            logger.info(
                "【Emby302代理】已启动: listen=%s:%s, origin=%s",
                self._emby_proxy_host,
                self._emby_proxy_port,
                self._emby_server_url,
            )
        except Exception as e:
            self._emby_proxy_server = None
            self._emby_proxy_thread = None
            logger.error(f"【Emby302代理】启动失败: {e}", exc_info=True)

    def _stop_emby_proxy_service(self) -> None:
        """停止 Emby 302 前置代理。"""
        server = self._emby_proxy_server
        thread = self._emby_proxy_thread
        if server:
            try:
                server.should_exit = True
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=5)
            except Exception:
                pass
        self._emby_proxy_server = None
        self._emby_proxy_thread = None

    def _schedule_once_sync(self) -> None:
        """立刻全量同步：用 date 触发器 3s 后跑一次。"""
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = BackgroundScheduler()
            # 3 秒后执行一次性任务
            run_time = self._now_plus_seconds(3)
            self._scheduler.add_job(
                self.run_once, "date", run_date=run_time, id="cloudstrm_once_sync",
            )
            self._scheduler.start()
            logger.info("【云端STRM】已排定 3s 后执行一次全量同步")
        except Exception as e:
            logger.error(f"【云端STRM】排定一次性同步失败: {e}", exc_info=True)

    @staticmethod
    def _now_plus_seconds(seconds: int):
        """当前时间 + seconds（避免在插件中使用 Date.now 类调用）。"""
        from datetime import datetime, timedelta
        return datetime.now() + timedelta(seconds=seconds)

    def stop_service(self) -> None:
        """停止所有服务：scheduler + cloud_sync 队列。"""
        try:
            self._stop_emby_proxy_service()
        except Exception as e:
            logger.error(f"【云端STRM】停止 Emby 302 代理失败: {e}")
        try:
            if self._sse_listener:
                self._sse_listener.stop()
                self._sse_listener = None
        except Exception as e:
            logger.error(f"【云端STRM】停止 SSE 监听失败: {e}")
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
            logger.error(f"【云端STRM】停止 scheduler 失败: {e}")
        try:
            if self._cloud_sync:
                self._cloud_sync.stop()
        except Exception as e:
            logger.error(f"【云端STRM】停止云同步失败: {e}")
        # 不复位 _cloud_sync 等，便于 status 查询；init_plugin 会重建

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件无远程命令。"""
        return []

    def get_page(self) -> List[dict]:
        """插件首页统计面板：4 统计卡片 + 最近上传表 + 最近 STRM 表。"""
        stats = getattr(self, "_stats", None) or {}
        if not stats and not isinstance(self, type):
            stats = self._load_stats()
        recent_uploads = stats.get("recent_uploads") or []
        recent_strms = stats.get("recent_strms") or []
        upload_count = stats.get("upload_count") or 0
        last_upload_time = stats.get("last_upload_time") or ""
        strm_count = stats.get("strm_count") or 0
        last_strm_time = stats.get("last_strm_time") or ""

        def _status_text(s):
            return {
                "uploaded": "已上传",
                "skipped": "远端已存在",
                "remote_deleted": "已删云端",
                "local_deleted": "已删本地",
            }.get(s, s or "-")

        def _strm_status_text(created):
            return "新生成" if created else "已存在/已跳过"

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
        clear_strm_api = f"plugin/{self.__class__.__name__}/clear_strm_history?apikey={settings.API_TOKEN}"

        def _menu_item(text, icon, params, color="primary"):
            """操作菜单项：VListItem，点击 POST /manual_action。"""
            return {
                "component": "VListItem",
                "props": {
                    "base-color": color,
                    "prepend-icon": icon,
                    "title": text,
                },
                "events": {"click": {"api": manual_api, "method": "post", "params": params}},
            }

        def _action_menu_cell(items):
            """一个“操作”按钮，点击展开 VMenu 下拉显示 items。"""
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
            """最近上传行：按记录字段渲染可用动作菜单。"""
            local = it.get("local") or ""
            remote = it.get("remote") or ""
            status = it.get("status") or ""
            items = []
            # 重新上传：仅上传/跳过且有本地源文件路径
            if local and remote and status in ("uploaded", "skipped"):
                items.append(_menu_item(
                    "重新上传", "mdi-upload-refresh",
                    {"action": "reupload", "local": local, "remote": remote}))
            # 删除云端：有云端路径即可
            if remote:
                items.append(_menu_item(
                    "删除云端", "mdi-cloud-remove",
                    {"action": "delete_remote", "local": "", "remote": remote},
                    color="warning"))
            # 删除云端+本地：需要本地路径（后端再校验是否在上传映射内）
            if local and remote:
                items.append(_menu_item(
                    "删云端和本地", "mdi-delete-forever",
                    {"action": "delete_remote_and_local", "local": local, "remote": remote},
                    color="error"))
            return _action_menu_cell(items)

        def _strm_action_cell(it):
            """最近 STRM 行：重新生成 STRM / 删除 STRM 文件菜单。"""
            strm = it.get("path") or ""
            remote = it.get("remote") or ""
            items = []
            if strm and remote:
                items.append(_menu_item(
                    "重新生成 STRM", "mdi-file-refresh",
                    {"action": "regenerate_strm", "local": "", "remote": remote, "strm": strm}))
            if strm:
                items.append(_menu_item(
                    "删除 STRM 文件", "mdi-file-remove",
                    {"action": "delete_strm", "local": "", "remote": remote, "strm": strm},
                    color="warning"))
            return _action_menu_cell(items)

        # 最近上传表行
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

        # 最近 STRM 表行
        strm_rows = [
            {"component": "tr", "content": [
                {"component": "td", "text": it.get("name") or "-"},
                {"component": "td", "text": _strm_status_text(it.get("created"))},
                {"component": "td", "text": it.get("time") or "-"},
                {"component": "td", "props": {"class": "text-caption text-grey"}, "text": it.get("path") or "-"},
                {"component": "td", "props": {"class": "text-caption text-grey"}, "text": it.get("remote") or "-"},
                _strm_action_cell(it),
            ]}
            for it in recent_strms[:5]
        ]
        if not strm_rows:
            strm_rows = [{"component": "tr", "content": [
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
                    {"component": "VCol", "props": {"cols": 12, "md": 3},
                     "content": [_stat_card("累计上传数量", upload_count)]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3},
                     "content": [_stat_card("最近上传时间", last_upload_time or "-")]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3},
                     "content": [_stat_card("累计生成 STRM 数量", strm_count)]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3},
                     "content": [_stat_card("最近生成 STRM 时间", last_strm_time or "-")]},
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
                    {"component": "VDivider", "props": {"class": "mx-4"}},
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
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center py-3 px-4"},
                     "content": [
                         {"component": "span", "text": "最近生成 STRM 列表"},
                         {"component": "VSpacer"},
                         {"component": "VBtn", "props": {
                             "prepend-icon": "mdi-delete-sweep", "variant": "tonal",
                             "color": "info", "size": "small", "density": "compact",
                         }, "text": "清除 STRM 历史",
                          "events": {"click": {"api": clear_strm_api, "method": "post"}}},
                     ]},
                    {"component": "VDivider", "props": {"class": "mx-4"}},
                    {"component": "VTable", "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "文件名"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "STRM 路径"},
                            {"component": "th", "text": "云端路径"},
                            {"component": "th", "text": "操作"},
                        ]}]},
                        {"component": "tbody", "content": strm_rows},
                    ]},
                ],
            },
        ]

    # ============================================================
    # API 端点
    # ============================================================
    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：/redirect（302）、/status（状态）、/sync_now（手动触发）。

        auth=apikey：面向外部系统/客户端调用（STRM 播放器、脚本）。
        规范：如无特殊原因不要默认匿名开放，故显式声明鉴权方式。
        """
        return [
            {
                "path": "/redirect",
                "endpoint": self.redirect,
                "methods": ["GET", "HEAD"],
                "auth": "apikey",
                "summary": "302跳转",
                "description": "解析云端路径为直链并 302 重定向（STRM 播放）",
            },
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
                "description": "手动触发一次全量同步",
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
                "path": "/clear_strm_history",
                "endpoint": self.clear_strm_history,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "清除 STRM 历史",
                "description": "仅清除最近 STRM 生成记录，不删除 .strm 文件",
            },
            {
                "path": "/manual_action",
                "endpoint": self.manual_action,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "单条手动处理",
                "description": "对最近上传/STRM 记录执行单条操作：reupload/delete_remote/delete_remote_and_local/regenerate_strm/delete_strm",
            },
        ]

    def redirect(self, request: Request, apikey: str = "", path: str = ""):
        """302 跳转端点：校验 apikey → 解析 path 为直链 → 302。

        可靠性增强（v1.3.0）：
        - cache key 含 UA 哈希与解析模式，避免缓存串/爆炸；
        - in-flight 请求合并：同 key 并发只解析一次；
        - 失败负缓存：坏路径短 TTL 内直接 502，避免疯狂请求；
        - HEAD 策略（head_probe_mode）：ok=返回200(兼容)、redirect=同GET返回302、resolve=200+X-Resolved-Url；
        - 日志脱敏：不打印带 sign/token 的完整 URL。
        """
        # 鉴权
        if not apikey or apikey != (settings.API_TOKEN or ""):
            return JSONResponse({"state": False, "message": "鉴权失败"}, status_code=401)
        if not path:
            return JSONResponse({"state": False, "message": "缺少 path 参数"}, status_code=400)
        path = self._normalize_remote_path_arg(path)

        ua = request.headers.get("User-Agent", "")

        # 失败负缓存：最近解析失败的 path 直接 502
        err = self._redirect_error_cache.get(path)
        if err:
            return JSONResponse(
                {"state": False, "message": f"最近解析失败，稍后重试: {err}"},
                status_code=502,
            )

        # HEAD 请求策略
        if request.method == "HEAD":
            return self._handle_head(ua, path)

        if self._proxy is None:
            return JSONResponse({"state": False, "message": "代理未初始化（AList 未连接）"}, status_code=503)

        cache_key = self._redirect_cache_key(path, ua)
        try:
            link = self._cached_resolve(cache_key, path, ua)
        except Exception as e:
            # 写负缓存并返回 502
            self._redirect_error_cache[path] = str(e)[:200]
            logger.error(f"【302跳转】解析失败: path={path}, err={e}", exc_info=True)
            return JSONResponse({"state": False, "message": f"解析直链失败: {e}"}, status_code=502)

        resp = RedirectResponse(url=link.url, status_code=302)
        self._set_redirect_headers(resp, link)
        return resp

    @staticmethod
    def _normalize_remote_path_arg(path: str) -> str:
        """规范化 URL query 传入的云端路径，兼容插件网关未解码的情况。"""
        value = str(path or "").strip()
        for _ in range(2):
            decoded = unquote_plus(value)
            if decoded == value:
                break
            value = decoded
        if value and not value.startswith("/") and not value.startswith(("http://", "https://")):
            value = "/" + value
        return value

    def _handle_head(self, ua: str, path: str):
        """HEAD 请求按 head_probe_mode 处理。

        - ok（默认兼容）：返回 200，不跳转。兼容 Infuse/Fileball 先 HEAD 探测。
        - redirect：同 GET，解析后返回 302（严格模式）。
        - resolve：解析目标 URL 但返回 200，header 附带脱敏 X-Resolved-Url（仅 host）。
        """
        mode = self._head_probe_mode
        if mode == "redirect":
            if self._proxy is None:
                return JSONResponse({"state": False, "message": "代理未初始化"}, status_code=503)
            try:
                link = self._cached_resolve(self._redirect_cache_key(path, ua), path, ua)
            except Exception as e:
                self._redirect_error_cache[path] = str(e)[:200]
                return JSONResponse({"state": False, "message": f"解析直链失败: {e}"}, status_code=502)
            resp = RedirectResponse(url=link.url, status_code=302)
            self._set_redirect_headers(resp, link)
            return resp
        if mode == "resolve" and self._proxy is not None:
            try:
                link = self._cached_resolve(self._redirect_cache_key(path, ua), path, ua)
                from .proxy_handler import _safe_url_for_log
                resp = JSONResponse({"state": True}, status_code=200)
                resp.headers["X-Resolved-Url"] = _safe_url_for_log(link.url)
                self._set_redirect_headers(resp, link)
                return resp
            except Exception as e:
                self._redirect_error_cache[path] = str(e)[:200]
                return JSONResponse({"state": False, "message": f"解析直链失败: {e}"}, status_code=502)
        # ok 模式
        resp = Response(status_code=200)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-CloudStrm-Mode"] = self._strm_url_mode
        resp.headers["X-CloudStrm-Head-Mode"] = "ok"
        return resp

    def _set_redirect_headers(self, resp, link: DirectLink) -> None:
        """302/诊断响应头只放来源状态，不泄露真实 URL。"""
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-CloudStrm-Mode"] = self._strm_url_mode
        resp.headers["X-CloudStrm-Link-Mode"] = self._direct_link_mode
        resp.headers["X-CloudStrm-Link-Source"] = link.source
        resp.headers["X-CloudStrm-Final-Resolved"] = "1" if link.resolved_final else "0"
        direct = link.source == "raw_url" or bool(link.resolved_final)
        resp.headers["X-CloudStrm-Direct-Link"] = "1" if direct else "0"

    def _redirect_cache_key(self, path: str, ua: str) -> Tuple[str, str, str, str]:
        """cache key = (path, ua-hash[:16], 解析模式, 直链策略)，避免完整 UA 进缓存。"""
        import hashlib
        ua_key = hashlib.sha256((ua or "").encode()).hexdigest()[:16]
        mode_key = "final" if self._resolve_final_url else "origin"
        return (path, ua_key, mode_key, self._direct_link_mode)

    def _get_resolve_lock(self, cache_key) -> threading.Lock:
        """获取 cache key 对应的 in-flight 锁（请求合并）。"""
        key_str = str(cache_key)
        with self._resolve_locks_guard:
            lock = self._resolve_locks.get(key_str)
            if lock is None:
                lock = threading.Lock()
                self._resolve_locks[key_str] = lock
            return lock

    @staticmethod
    def _coerce_direct_link(value) -> Optional[DirectLink]:
        if isinstance(value, DirectLink):
            return value
        if isinstance(value, str) and value:
            return DirectLink(url=value, source="legacy_cache")
        return None

    def _cached_resolve(self, cache_key, path: str, ua: str) -> DirectLink:
        """带 TTL 缓存 + in-flight 合并的解析。

        1. 查缓存命中直接返回；
        2. 获取该 key 锁，进入后再查一次缓存（double-check）；
        3. 未命中才真正调用 AList/OpenList；
        4. 写缓存后释放锁。
        """
        cached_link = self._coerce_direct_link(self._redirect_cache.get(cache_key))
        if cached_link and not cached_link.expires_soon():
            return cached_link
        if cached_link:
            self._redirect_cache.pop(cache_key, None)
        lock = self._get_resolve_lock(cache_key)
        with lock:
            # double-check
            cached_link = self._coerce_direct_link(self._redirect_cache.get(cache_key))
            if cached_link and not cached_link.expires_soon():
                return cached_link
            if cached_link:
                self._redirect_cache.pop(cache_key, None)
            if hasattr(self._proxy, "resolve_link"):
                link = self._proxy.resolve_link(path, ua)
            else:
                link = DirectLink(url=self._proxy.resolve(path, ua), source="legacy_proxy")
            if self._redirect_cache_ttl > 0 and not link.expires_soon():
                self._redirect_cache[cache_key] = link
            return link

    def status(self, request: Request = None):
        """返回云同步进度快照。"""
        if self._cloud_sync is None:
            return JSONResponse({"state": False, "message": "云同步未初始化"}, status_code=503)
        return JSONResponse({"state": True, "data": self._cloud_sync.get_status()})

    def diagnose(self, request: Request = None, probe: bool = False):
        """返回运行诊断信息；probe=true 时做 AList 只读连通性探测。"""
        return JSONResponse({"state": True, "data": self._diagnostic_snapshot(probe=probe)})

    def sync_now(self, request: Request = None):
        """手动触发一次全量同步（异步，立即返回）。"""
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)
        if not self._local_media_path:
            return JSONResponse({"state": False, "message": "未配置本地媒体路径"}, status_code=400)
        threading.Thread(target=self._safe_run_once, daemon=True).start()
        return JSONResponse({"state": True, "message": "已触发全量同步"})

    def clear_upload_history(self, request: Request = None):
        """清除最近上传历史（仅记录，不删除云端/本地文件）。"""
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)
        try:
            stats = getattr(self, "_stats", None) or self._load_stats()
            stats["recent_uploads"] = []
            stats["upload_count"] = 0
            stats["last_upload_time"] = ""
            self._stats = stats
            self._save_stats()
            logger.info("【云端STRM】上传历史已清除（云端/本地文件未删除）")
            return JSONResponse({"state": True, "message": "上传历史已清除（云端/本地文件未删除）"})
        except Exception as e:
            logger.error(f"【云端STRM】清除上传历史失败: {e}", exc_info=True)
            return JSONResponse({"state": False, "message": f"清除失败: {e}"}, status_code=500)

    def clear_strm_history(self, request: Request = None):
        """清除最近 STRM 生成历史（仅记录，不删除 .strm 文件）。"""
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)
        try:
            stats = getattr(self, "_stats", None) or self._load_stats()
            stats["recent_strms"] = []
            stats["strm_count"] = 0
            stats["last_strm_time"] = ""
            self._stats = stats
            self._save_stats()
            logger.info("【云端STRM】STRM 生成历史已清除（.strm 文件未删除）")
            return JSONResponse({"state": True, "message": "STRM 生成历史已清除（.strm 文件未删除）"})
        except Exception as e:
            logger.error(f"【云端STRM】清除 STRM 历史失败: {e}", exc_info=True)
            return JSONResponse({"state": False, "message": f"清除失败: {e}"}, status_code=500)

    def _safe_run_once(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            logger.error(f"【云端STRM】手动同步异常: {e}", exc_info=True)

    def manual_action(self, params: ManualActionParams):
        """单条手动处理端点（首页列表内操作调用，POST body = ManualActionParams）。

        action:
          - reupload              : 先删云端再上传，并重新生成 STRM（需 local+remote）
          - delete_remote         : 只删云端文件（需 remote）
          - delete_remote_and_local: 删云端 + 删本地（需 local+remote，local 须在上传映射内）
          - regenerate_strm       : 重新生成 STRM（需 strm+remote）
          - delete_strm           : 删除本地 STRM 文件（需 strm）

        校验通过后启动后台线程执行真实任务（_manual_action_worker → AList/OpenList 操作）。
        """
        if not self._enabled:
            return JSONResponse({"state": False, "message": "插件未启用"}, status_code=400)

        # 1. 动作合法性 + 必填参数
        err = params.validate_action()
        if err:
            return JSONResponse({"state": False, "message": err}, status_code=400)

        # 2. 路径范围校验（同步，失败立即 400，不启动线程）
        payload = params.to_payload()
        action = (params.action or "").strip().lower()
        try:
            if payload["kind"] == "strm":
                self._decode_manual_strm_target(payload["target"])
            elif action == "reupload":
                self._decode_manual_upload_target(payload["target"])
            else:  # delete_remote / delete_remote_and_local
                self._decode_manual_delete_target(
                    payload["target"], require_local=(action == "delete_remote_and_local"))
        except Exception as e:
            logger.warning(f"【手动处理】单条操作校验失败: action={action}, err={e}")
            return JSONResponse({"state": False, "message": str(e)}, status_code=400)

        # 3. 启动后台线程执行真实任务
        threading.Thread(
            target=self._manual_action_worker,
            args=(payload,), daemon=True,
            name="CloudStrmManualAction",
        ).start()
        logger.info(f"【手动处理】已开始执行单条操作: action={action}, remote={params.remote}")
        return JSONResponse({"state": True, "message": f"已开始执行: {action}"})

    @staticmethod
    def _normalize_manual_upload_action(value: str) -> str:
        normalized = (value or "none").strip().lower()
        if normalized in {"reupload", "delete_remote", "delete_remote_and_local"}:
            return normalized
        return "none"

    def _manual_action_worker(self, action: Dict[str, Any]) -> None:
        try:
            if action.get("kind") == "upload":
                upload_action = action.get("action")
                if upload_action == "reupload":
                    local_path, remote_path = self._decode_manual_upload_target(action.get("target") or "")
                    self._manual_reupload_worker(local_path, remote_path)
                elif upload_action == "delete_remote":
                    _, remote_path = self._decode_manual_delete_target(action.get("target") or "")
                    self._manual_delete_remote_worker(remote_path)
                elif upload_action == "delete_remote_and_local":
                    local_path, remote_path = self._decode_manual_delete_target(
                        action.get("target") or "",
                        require_local=True,
                    )
                    self._manual_delete_remote_worker(remote_path)
                    self._manual_delete_local_file(local_path)
                else:
                    raise Exception(f"未知上传手动动作: {upload_action}")
                return
            if action.get("kind") == "strm":
                strm_path, remote_path = self._decode_manual_strm_target(action.get("target") or "")
                strm_action = action.get("action")
                if strm_action == "delete_strm":
                    self._manual_delete_strm_file(strm_path)
                else:
                    ok, _ = self._regenerate_strm_file(strm_path, remote_path)
                    if not ok:
                        raise Exception("STRM 重新生成失败")
                return
            raise Exception(f"未知手动动作: {action}")
        except Exception as e:
            logger.error(f"【手动处理】执行失败: {e}", exc_info=True)
            self._notify("云端STRM手动处理失败", str(e))

    def _manual_reupload_worker(self, local_path: str, remote_path: str) -> None:
        try:
            if not self._alist_client:
                raise Exception("AList/OpenList 客户端未初始化")
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
            ok, _ = self._regenerate_strm_for_pair(local_path, remote_path)
            if not ok:
                raise Exception("上传完成，但 STRM 重新生成失败")
            logger.info(f"【手动处理】重新上传完成: {local_path} -> {remote_path}")
            self._notify("云端STRM手动处理完成", f"重新上传完成：{Path(local_path).name}")
        except Exception as e:
            logger.error(f"【手动处理】重新上传失败: {local_path} -> {remote_path}: {e}", exc_info=True)
            raise

    def _manual_delete_remote_worker(self, remote_path: str) -> None:
        if not self._alist_client:
            raise Exception("AList/OpenList 客户端未初始化")
        remote_path = self._validate_remote_path(remote_path)
        self._alist_client.remove_file(remote_path)
        self._record_upload_stat("", remote_path, 0, status="remote_deleted")
        logger.info(f"【手动处理】云端文件已删除: {remote_path}")
        self._notify("云端STRM手动处理完成", f"云端文件已删除：{Path(remote_path).name}")

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
        self._notify("云端STRM手动处理完成", f"本地文件已删除：{Path(local_path).name}")

    def _manual_delete_strm_file(self, strm_path: str) -> None:
        """删除指定的本地 .strm 文件，并从 STRM 列表中移除记录。"""
        strm_path = unquote_plus(str(strm_path or "").strip())
        if not strm_path:
            raise Exception("缺少 strm 参数")
        if not self._is_known_strm_path(strm_path):
            raise Exception("strm_path 不在已配置的 STRM 输出路径范围内")
        path = Path(strm_path)
        if not path.exists():
            logger.warning(f"【手动处理】STRM 文件不存在，从列表移除: {strm_path}")
            self._remove_strm_stat(strm_path)
            self._notify("云端STRM手动处理完成", f"STRM 文件已不存在：{path.name}")
            return
        path.unlink()
        self._remove_strm_stat(strm_path)
        logger.info(f"【手动处理】STRM 文件已删除: {strm_path}")
        self._notify("云端STRM手动处理完成", f"STRM 文件已删除：{path.name}")

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

    def _validate_remote_path(self, remote_path: str) -> str:
        value = self._normalize_remote_path_arg(remote_path)
        if not value:
            raise Exception("缺少 remote 参数")
        if self._is_known_remote_path(value):
            return value
        raise Exception("remote 不在已配置的云端路径范围内")

    def _validate_strm_path(self, strm_path: str) -> str:
        value = unquote_plus(str(strm_path or "").strip())
        if not value:
            raise Exception("缺少 strm 参数")
        if self._is_known_strm_path(value):
            return value
        raise Exception("strm 不在已配置的 STRM 输出路径范围内")

    @staticmethod
    def _manual_entry_value(**kwargs) -> str:
        return json.dumps(kwargs, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _manual_entry_data(value: str) -> Dict[str, str]:
        try:
            data = json.loads(value or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _decode_manual_upload_target(self, value: str) -> Tuple[str, str]:
        data = self._manual_entry_data(value)
        return self._validate_reupload_paths(data.get("local") or "", data.get("remote") or "")

    def _decode_manual_delete_target(self, value: str, require_local: bool = False) -> Tuple[str, str]:
        data = self._manual_entry_data(value)
        remote_path = self._validate_remote_path(data.get("remote") or "")
        local_path = unquote_plus(str(data.get("local") or "").strip())
        if require_local and not local_path:
            raise Exception("缺少 local 参数")
        if local_path:
            expected_remote = self._build_remote_path(local_path)
            if require_local and not expected_remote:
                raise Exception("local 不在上传映射范围内")
            if expected_remote and remote_path != expected_remote:
                raise Exception("remote 与上传映射计算结果不一致，已拒绝")
        return local_path, remote_path

    def _decode_manual_strm_target(self, value: str) -> Tuple[str, str]:
        data = self._manual_entry_data(value)
        strm_path = self._validate_strm_path(data.get("strm") or data.get("path") or "")
        remote_path = self._validate_remote_path(data.get("remote") or "")
        return strm_path, remote_path

    def _is_known_remote_path(self, remote_path: str) -> bool:
        roots = [cloud for _, cloud in self._upload_mappings or []]
        roots.extend([cloud for cloud, _ in self._strm_mappings or []])
        if self._alist_target_path:
            roots.append(self._alist_target_path)
        return any(self._has_path_prefix(Path(remote_path), Path(root)) for root in roots if root)

    def _is_known_strm_path(self, strm_path: str) -> bool:
        roots = [strm for _, strm in self._strm_mappings or []]
        roots.extend([strm for _, strm in self._local_strm_mappings or []])
        for root in roots:
            if root and self._has_path_prefix(Path(strm_path), Path(root)):
                return True
        return False

    def _regenerate_strm_for_pair(self, local_path: str, remote_path: str) -> Tuple[bool, Optional[Path]]:
        if not self._strm_gen:
            raise Exception("STRM 生成器未初始化")
        old_mode = self._overwrite_mode
        try:
            self._overwrite_mode = "always"
            ok, strm_path, _ = self._strm_gen.generate(local_path, remote_path)
            if ok:
                self._record_strm_stat(strm_path, created=False, remote_path=remote_path, local_path=local_path)
            return ok, strm_path
        finally:
            self._overwrite_mode = old_mode

    def _regenerate_strm_file(self, strm_path: str, remote_path: str) -> Tuple[bool, Optional[Path]]:
        if not self._strm_gen:
            raise Exception("STRM 生成器未初始化")
        path = Path(strm_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        strm_url = self._strm_gen._build_strm_url(remote_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(strm_url)
        self._record_strm_stat(path, created=False, remote_path=remote_path)
        logger.info(f"【手动处理】STRM 已重新生成: {path} -> {remote_path}")
        return True, path

    # ============================================================
    # 事件处理（核心触发）
    # ============================================================
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """MoviePilot 内部整理完成事件兜底入口：仅记录事件，再进入 Phase 2。"""
        if not self._enabled:
            return
        if self._listener is None:
            return
        records = self._listener.handle(event)
        self._accept_phase1_records(records)

    def _accept_phase1_records(self, records: List[TransferRecord]) -> None:
        """Phase 1 输出入口：记录事件后异步进入 Phase 2。"""
        records = records or []
        if not records:
            return
        logger.info(f"【云端STRM】Phase 1 完成：记录有效事件 {len(records)} 条")
        threading.Thread(
            target=self._run_records_pipeline,
            args=(records,),
            daemon=True,
            name="CloudStrmPipeline",
        ).start()

    def _run_records_pipeline(self, records: List[TransferRecord]) -> None:
        """按顺序执行 Phase 2 → Phase 3/4。"""
        if not self._cloud_sync:
            logger.warning("【云端STRM】Phase 2 取消：云同步未初始化")
            return
        with self._sync_lock:
            queued = 0
            skipped = 0
            ready_for_strm: List[Tuple[str, str, Any, Any]] = []
            logger.info(f"【云端STRM】Phase 2 开始：AList 仅新增同步 {len(records)} 条")
            self._cloud_sync.prepare_batch()
            for record in records:
                files = self._expand_record_media_files(record)
                if not files:
                    if not os.path.exists(record.local_path):
                        logger.warning(f"【云端STRM】Phase 2 跳过：本地路径不存在 {record.local_path}")
                    skipped += 1
                    continue
                for local_path, remote_path, mediainfo, meta in files:
                    try:
                        if not self._cloud_sync.need_upload(local_path, remote_path):
                            logger.info(f"【云端STRM】Phase 2 跳过：远端已存在 {remote_path}")
                            self._record_upload_stat(local_path, remote_path, 0, status="skipped")
                            ready_for_strm.append((local_path, remote_path, mediainfo, meta))
                            skipped += 1
                            continue
                    except Exception as e:
                        logger.warning(f"【云端STRM】Phase 2 增量判定异常，按需上传: {e}")
                    self._cloud_sync.enqueue_file(local_path, remote_path, mediainfo, meta)
                    queued += 1
            self._cloud_sync.mark_scan_finish()
            logger.info(f"【云端STRM】Phase 2 扫描完成：入队 {queued}，跳过 {skipped}")
            finished = self._cloud_sync.wait_for_batch()
            for item in finished:
                if item.status in (TASK_SUCCEEDED, TASK_SKIPPED):
                    status = "skipped" if item.status == TASK_SKIPPED else "uploaded"
                    self._record_upload_stat(item.local_path, item.remote_path, item.file_size or 0, status=status)
                    ready_for_strm.append((item.local_path, item.remote_path, item.mediainfo, item.meta))
            logger.info(f"【云端STRM】Phase 2 完成：可生成 STRM {len(ready_for_strm)} 条")

            logger.info("【云端STRM】Phase 3 开始：基于云端路径生成 STRM")
            for local_path, remote_path, mediainfo, meta in ready_for_strm:
                self._on_file_synced(local_path, remote_path, mediainfo, meta)
            logger.info("【云端STRM】Phase 3/4 完成：STRM 生成与媒体库刷新结束")

    # ============================================================
    # Phase 3/4
    # ============================================================
    def _on_file_synced(self, local_path: str, remote_path: str,
                        mediainfo: Any = None, meta: Any = None) -> bool:
        """Phase 2 单文件完成后执行 Phase 3/4。"""
        if self._strm_gen is None:
            return False
        ok, strm_path, created = self._strm_gen.generate(local_path, remote_path, mediainfo, meta)
        if ok:
            self._record_strm_stat(strm_path, created, remote_path, local_path=local_path)
            self._cleanup_local_after_move(local_path)
        return ok

    def _cleanup_local_after_move(self, local_path: str) -> None:
        """移动模式：远端同步和 STRM 成功后删除本地源文件。"""
        if self._sync_mode != "move":
            return
        try:
            if os.path.isfile(local_path):
                os.remove(local_path)
                logger.info(f"【云端STRM】移动模式：已删除本地源文件 {local_path}")
        except Exception as e:
            logger.error(f"【云端STRM】移动模式删除本地源文件失败: {local_path}: {e}", exc_info=True)

    # ============================================================
    # 全量同步
    # ============================================================
    def run_once(self) -> None:
        """全量同步：遍历 local_media_path 下所有媒体文件，增量上传 + 生成 STRM。"""
        if not self._enabled or not self._cloud_sync:
            logger.warning("【云端STRM】未启用或云同步未就绪，跳过全量同步")
            return
        with self._sync_lock:
            self._run_once_locked()

    def _run_once_locked(self) -> None:
        """已持有同步锁的全量同步实现。"""
        local_roots = self._local_media_roots or self._parse_path_lines(self._local_media_path)
        if not local_roots:
            logger.warning("【云端STRM】未配置本地媒体路径，跳过全量同步")
            return

        media_exts = set(self._rmt_mediaext)
        exclude_spec = self._exclude_spec
        queued = 0
        skipped = 0
        ready_for_strm: List[Tuple[str, str, Any, Any]] = []

        logger.info(f"【云端STRM】开始全量同步: roots={local_roots}, upload_mappings={self._upload_mappings}")
        self._cloud_sync.prepare_batch()

        for local_root in local_roots:
            for root, dirs, files in os.walk(local_root):
                for name in files:
                    local_path = os.path.join(root, name)
                    ext = Path(name).suffix.lower().lstrip(".")
                    if ext not in media_exts:
                        skipped += 1
                        continue
                    if exclude_spec and self._is_excluded_path(local_path):
                        skipped += 1
                        continue
                    remote_path = self._build_remote_path(local_path)
                    if not remote_path:
                        skipped += 1
                        continue
                    try:
                        if not self._cloud_sync.need_upload(local_path, remote_path):
                            # 远端已存在不加入上传列表，但仍需生成 STRM
                            ready_for_strm.append((local_path, remote_path, None, None))
                            skipped += 1
                            continue
                    except Exception as e:
                        logger.warning(f"【云端STRM】增量判定异常，按需上传: {e}")
                    self._cloud_sync.enqueue_file(local_path, remote_path, None, None)
                    queued += 1

        self._cloud_sync.mark_scan_finish()
        logger.info(f"【云端STRM】全量扫描完成: 入队 {queued}，跳过 {skipped}")
        finished = self._cloud_sync.wait_for_batch()
        for item in finished:
            if item.status == TASK_SUCCEEDED:
                logger.info(f"【云端STRM】上传完成: {item.local_path} -> {item.remote_path} ({(item.file_size or 0)/1024/1024:.1f} MB)")
                self._record_upload_stat(item.local_path, item.remote_path, item.file_size or 0, status="uploaded")
                ready_for_strm.append((item.local_path, item.remote_path, item.mediainfo, item.meta))
            elif item.status == TASK_SKIPPED:
                logger.info(f"【云端STRM】远端已存在，跳过上传: {item.remote_path}")
                # 不记入上传列表，但仍需生成 STRM
                ready_for_strm.append((item.local_path, item.remote_path, item.mediainfo, item.meta))
        logger.info(f"【云端STRM】全量 Phase 2 完成：可生成 STRM {len(ready_for_strm)} 条")
        strm_ok = 0
        strm_fail = 0
        for local_path, remote_path, mediainfo, meta in ready_for_strm:
            ok = self._on_file_synced(local_path, remote_path, mediainfo, meta)
            if ok:
                strm_ok += 1
                logger.info(f"【云端STRM】STRM 生成完成: {Path(remote_path).name}")
            else:
                strm_fail += 1
                logger.warning(f"【云端STRM】STRM 生成失败: {remote_path}")
        logger.info(f"【云端STRM】全量 Phase 3/4 完成：STRM 成功 {strm_ok}，失败 {strm_fail}")

    # ============================================================
    # 路径映射与统计
    # ============================================================
    @staticmethod
    def _parse_path_lines(raw: str) -> List[str]:
        """解析多行路径配置。"""
        return [line.strip().rstrip("/") for line in (raw or "").splitlines() if line.strip()]

    @classmethod
    def _normalize_local_strm_paths(cls, config: Dict[str, Any]) -> str:
        """读取新映射配置；缺失时从旧 local_media_path/strm_output_path 迁移。"""
        raw = config.get("local_strm_paths")
        if raw:
            return str(raw)
        local_roots = cls._parse_path_lines(config.get("local_media_path") or DEFAULT_LOCAL_MEDIA_PATH)
        strm_root = (config.get("strm_output_path") or DEFAULT_STRM_OUTPUT_PATH).strip().rstrip("/")
        return "\n".join(f"{local}#{strm_root}" for local in local_roots if local)

    @classmethod
    def _normalize_upload_path_mappings(cls, config: Dict[str, Any]) -> str:
        """上传映射：`本地路径#AList/OpenList 云端路径`。

        兼容旧配置：如果没有 upload_path_mappings，则由旧 local_strm_paths/local_media_path
        加 alist_target_path 推导，保持旧的“全局云端根目录 + 本地相对路径”行为。
        """
        raw = config.get("upload_path_mappings")
        if raw:
            return str(raw)
        has_legacy = any(
            config.get(key)
            for key in ("local_strm_paths", "local_media_path", "strm_output_path", "alist_target_path")
        )
        if not has_legacy:
            return DEFAULT_UPLOAD_PATH_MAPPINGS
        cloud_root = (config.get("alist_target_path") or DEFAULT_ALIST_TARGET_PATH).strip().rstrip("/")
        legacy = cls._parse_local_strm_mappings(config.get("local_strm_paths") or "")
        if legacy:
            return "\n".join(f"{local}#{cloud_root}" for local, _ in legacy)
        local_roots = cls._parse_path_lines(config.get("local_media_path") or DEFAULT_LOCAL_MEDIA_PATH)
        return "\n".join(f"{local}#{cloud_root}" for local in local_roots if local)

    @classmethod
    def _normalize_strm_path_mappings(cls, config: Dict[str, Any]) -> str:
        """STRM 映射：`AList/OpenList 云端路径#本地 STRM 输出目录`。"""
        raw = config.get("strm_path_mappings")
        if raw:
            return str(raw)
        has_legacy = any(
            config.get(key)
            for key in ("local_strm_paths", "local_media_path", "strm_output_path", "alist_target_path")
        )
        if not has_legacy:
            return DEFAULT_STRM_PATH_MAPPINGS
        cloud_root = (config.get("alist_target_path") or DEFAULT_ALIST_TARGET_PATH).strip().rstrip("/")
        legacy = cls._parse_local_strm_mappings(config.get("local_strm_paths") or "")
        if legacy:
            return "\n".join(f"{cloud_root}#{strm}" for _, strm in legacy)
        strm_root = (config.get("strm_output_path") or DEFAULT_STRM_OUTPUT_PATH).strip().rstrip("/")
        return f"{cloud_root}#{strm_root}"

    @staticmethod
    def _normalize_sync_mode(value: str) -> str:
        """同步模式：copy=复制，move=移动；兼容旧 new。"""
        normalized = (value or "copy").strip().lower()
        if normalized in ("move", "移动"):
            return "move"
        return "copy"

    @staticmethod
    def _normalize_overwrite_mode(value: str) -> str:
        """STRM 覆盖模式：never=从不，always=总是。"""
        normalized = (value or "never").strip().lower()
        if normalized in ("always", "总是"):
            return "always"
        return "never"

    @staticmethod
    def _normalize_strm_url_mode(value: str) -> str:
        """STRM URL 模式：alist_direct（默认）/ cloud_raw_url（实验）。

        moviepilot_redirect 作为历史配置保留兼容，但会迁移为 alist_direct。
        """
        normalized = (value or DEFAULT_STRM_URL_MODE).strip().lower()
        if normalized in ("cloud_raw_url", "cloud_raw", "raw_url", "raw", "cloud_direct"):
            return "cloud_raw_url"
        return DEFAULT_STRM_URL_MODE

    @staticmethod
    def _normalize_direct_link_mode(value: str) -> str:
        """302 直链来源策略：优先 raw_url / 严格 raw_url / AList 下载端点。"""
        normalized = (value or "prefer_raw_url").strip().lower()
        if normalized in ("raw_url_only", "raw_url", "raw", "strict", "strict_raw"):
            return "raw_url_only"
        if normalized in ("alist_download", "alist_direct", "alist", "download", "d"):
            return "alist_download"
        return "prefer_raw_url"

    @staticmethod
    def _normalize_head_probe_mode(value: str) -> str:
        """HEAD 探测策略：ok（兼容200）/ redirect（严格302）/ resolve（诊断200+header）。"""
        normalized = (value or "ok").strip().lower()
        if normalized in ("redirect", "302"):
            return "redirect"
        if normalized in ("resolve", "diagnose"):
            return "resolve"
        return "ok"

    @classmethod
    def _parse_local_strm_mappings(cls, raw: str) -> List[Tuple[str, str]]:
        """解析路径映射：每行 `本地媒体库路径#STRM输出目录`。"""
        return cls._parse_path_mappings(raw)

    @classmethod
    def _parse_path_mappings(cls, raw: str) -> List[Tuple[str, str]]:
        """解析通用路径映射：每行 `源路径#目标路径`。"""
        mappings: List[Tuple[str, str]] = []
        seen = set()
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or "#" not in line:
                continue
            local, strm = line.split("#", 1)
            local = local.strip().rstrip("/")
            strm = strm.strip().rstrip("/")
            if not local or not strm:
                continue
            key = (local, strm)
            if key in seen:
                continue
            seen.add(key)
            mappings.append(key)
        return mappings

    @staticmethod
    def _unique_paths(paths: List[str]) -> List[str]:
        result: List[str] = []
        seen = set()
        for path in paths:
            if path and path not in seen:
                seen.add(path)
                result.append(path)
        return result

    def _build_remote_path(self, local_path: str) -> Optional[str]:
        """本地媒体路径 → AList 云端路径。"""
        matched = self._match_upload_mapping(local_path)
        if matched is None:
            return None
        _, cloud_root, rel = matched
        rel_str = str(rel).replace("\\", "/").lstrip("/")
        return f"{cloud_root.rstrip('/')}/{rel_str}".rstrip("/")

    def _relative_to_local_roots(self, local_path: str) -> Optional[Path]:
        matched = self._match_upload_mapping(local_path)
        return matched[2] if matched else None

    def _match_upload_mapping(self, local_path: str) -> Optional[Tuple[str, str, Path]]:
        """匹配上传映射，返回 (本地根, 云端根, 相对路径)。"""
        mappings = (
            getattr(self, "_upload_mappings", None)
            or self._parse_path_mappings(getattr(self, "_upload_path_mappings", ""))
        )
        if not mappings:
            cloud_root = getattr(self, "_alist_target_path", DEFAULT_ALIST_TARGET_PATH)
            legacy = (
                getattr(self, "_local_strm_mappings", None)
                or self._parse_local_strm_mappings(getattr(self, "_local_strm_paths", ""))
            )
            mappings = [(local, cloud_root) for local, _ in legacy]
        return self._match_path_mapping(local_path, mappings)

    def _match_local_strm_mapping(self, local_path: str) -> Optional[Tuple[str, str, Path]]:
        """匹配本地路径所属映射，返回 (本地根, STRM 根, 相对路径)。"""
        mappings = (
            getattr(self, "_local_strm_mappings", None)
            or self._parse_local_strm_mappings(getattr(self, "_local_strm_paths", ""))
        )
        if not mappings:
            strm_root = getattr(self, "_strm_output_path", DEFAULT_STRM_OUTPUT_PATH)
            mappings = [(root, strm_root) for root in getattr(self, "_local_media_roots", [])]
        return self._match_path_mapping(local_path, mappings)

    def _match_strm_mapping(self, remote_path: str) -> Optional[Tuple[str, str, Path]]:
        """匹配 STRM 映射，返回 (云端根, STRM 根, 相对路径)。"""
        mappings = (
            getattr(self, "_strm_mappings", None)
            or self._parse_path_mappings(getattr(self, "_strm_path_mappings", ""))
        )
        return self._match_path_mapping(remote_path, mappings)

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

    def _remote_relative_path(self, remote_path: str) -> Optional[Path]:
        """AList 云端路径 → 相对云端目标根的路径。"""
        matched = self._match_strm_mapping(remote_path)
        if matched:
            return matched[2]
        remote = Path(remote_path)
        root = Path(self._alist_target_path)
        try:
            return remote.relative_to(root)
        except ValueError:
            if self._has_path_prefix(remote, root):
                return Path(*remote.parts[len(root.parts):])
            logger.debug(f"【云端STRM】云端路径不在目标根下: {remote_path}")
            return None

    @staticmethod
    def _has_path_prefix(full: Path, prefix: Path) -> bool:
        full_parts, prefix_parts = full.parts, prefix.parts
        return len(prefix_parts) <= len(full_parts) and full_parts[:len(prefix_parts)] == prefix_parts

    def _strm_output_path_for(self, local_path: str, remote_path: str) -> Optional[Path]:
        """本地路径 + AList 云端路径 → 对应 STRM 输出路径。"""
        # 旧配置未显式设置云端->STRM 映射时，继续按本地路径选择 STRM 根，避免旧多根配置改变行为。
        if not getattr(self, "_strm_path_mappings_explicit", False):
            matched = self._match_local_strm_mapping(local_path)
            if matched:
                _, strm_root, rel = matched
                return Path(strm_root) / rel.parent / (rel.stem + ".strm")
        matched = self._match_strm_mapping(remote_path)
        if matched:
            _, strm_root, rel = matched
            return Path(strm_root) / rel.parent / (rel.stem + ".strm")
        return self._strm_output_path_from_remote(remote_path)

    def _strm_output_path_from_remote(self, remote_path: str) -> Optional[Path]:
        """AList 云端路径 → STRM 输出路径；无法定位本地映射时使用第一条 STRM 根兜底。"""
        matched = self._match_strm_mapping(remote_path)
        if matched:
            _, strm_root, rel = matched
            return Path(strm_root) / rel.parent / (rel.stem + ".strm")
        rel = self._remote_relative_path(remote_path)
        if rel is None:
            return None
        mappings = (
            getattr(self, "_strm_mappings", None)
            or getattr(self, "_local_strm_mappings", None)
            or []
        )
        strm_root = mappings[0][1] if mappings else getattr(self, "_strm_output_path", DEFAULT_STRM_OUTPUT_PATH)
        return Path(strm_root) / rel.parent / (rel.stem + ".strm")

    def _is_media_file(self, path: str) -> bool:
        ext = Path(path).suffix.lower().lstrip(".")
        return bool(ext and ext in set(self._rmt_mediaext))

    def _expand_record_media_files(self, record: TransferRecord) -> List[Tuple[str, str, Any, Any]]:
        """Phase 2 展开 Phase 1 记录。

        SSE 消息可能只包含整理后的目录路径；目录扫描属于 Phase 2 文件操作，
        因此在这里展开为具体媒体文件。
        """
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

    def _diagnostic_snapshot(self, probe: bool = False) -> Dict[str, Any]:
        """构建脱敏运行诊断快照。"""
        local_roots = self._local_media_roots or self._parse_path_lines(self._local_media_path)
        stats = getattr(self, "_stats", None) or {}
        if not stats and not isinstance(self, type):
            stats = self._load_stats()
        sample_local = self._sample_local_path(local_roots)
        sample_remote = self._build_remote_path(sample_local) if sample_local else ""
        sample_strm = str(self._strm_output_path_from_remote(sample_remote)) if sample_remote else ""
        data: Dict[str, Any] = {
            "enabled": self._enabled,
            "phase_order": ["listen", "sync", "strm", "refresh"],
            "config": {
                "moviepilot_address": self._moviepilot_address,
                "cloud_storage_type": self._cloud_storage_type,
                "alist_url": self._alist_url,
                "alist_token": self._mask_secret(self._alist_token),
                "alist_token_configured": bool(self._alist_token),
                "alist_target_path": self._alist_target_path,
                "upload_path_mappings": self._upload_path_mappings,
                "upload_mappings": [
                    {"local": local, "cloud": cloud}
                    for local, cloud in self._upload_mappings
                ],
                "strm_path_mappings": self._strm_path_mappings,
                "strm_mappings": [
                    {"cloud": cloud, "strm": strm}
                    for cloud, strm in self._strm_mappings
                ],
                "local_strm_paths": self._local_strm_paths,
                "local_strm_mappings": [
                    {"local": local, "strm": strm}
                    for local, strm in self._local_strm_mappings
                ],
                "local_media_roots": local_roots,
                "sync_mode": self._sync_mode,
                "overwrite_mode": self._overwrite_mode,
                "upload_concurrency": self._upload_concurrency,
                "media_exts": self._rmt_mediaext,
                "event_filters": self._event_filter_prefixes,
                "refresh_enabled": self._refresh_enabled,
                "mediaservers": self._mediaservers,
                "path_mapping": self._transfer_mp_mediaserver_paths,
                "strm_url_mode": self._strm_url_mode,
                "resolve_final_url": self._resolve_final_url,
                "direct_link_mode": self._direct_link_mode,
                "redirect_cache_ttl": self._redirect_cache_ttl,
                "head_probe_mode": self._head_probe_mode,
                "sse_enabled": self._sse_enabled,
                "emby_proxy_enabled": self._emby_proxy_enabled,
                "emby_server_url": self._emby_server_url,
                "emby_proxy_host": self._emby_proxy_host,
                "emby_proxy_port": self._emby_proxy_port,
            },
            "modules": {
                "sse_listener": bool(self._sse_listener),
                "alist_client": bool(self._alist_client),
                "cloud_sync": bool(self._cloud_sync),
                "strm_generator": bool(self._strm_gen),
                "proxy": bool(self._proxy),
                "emby_proxy": bool(self._emby_proxy_server),
            },
            "redirect": {
                "strm_url_mode": self._strm_url_mode,
                "resolve_final_url": self._resolve_final_url,
                "direct_link_mode": self._direct_link_mode,
                "redirect_cache_ttl": self._redirect_cache_ttl,
                "head_probe_mode": self._head_probe_mode,
                "redirect_cache_size": len(self._redirect_cache),
                "redirect_error_cache_size": len(self._redirect_error_cache),
                "emby_proxy_enabled": self._emby_proxy_enabled,
                "emby_proxy_running": bool(self._emby_proxy_server),
                "emby_proxy_listen": (
                    f"{self._emby_proxy_host}:{self._emby_proxy_port}"
                    if self._emby_proxy_enabled else ""
                ),
            },
            "local_roots": [
                {"path": root, "exists": os.path.exists(root), "is_dir": os.path.isdir(root)}
                for root in local_roots
            ],
            "mapping_sample": {
                "local": sample_local,
                "remote": sample_remote,
                "strm": sample_strm,
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
        """脱敏展示 Token，不输出原文。"""
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
        """AList 只读连通性探测：校验 token，并尝试对样本远端路径 fs_get（脱敏输出）。

        probe 输出不含 raw_url/sign 完整内容，只输出是否存在、is_dir、size、name。
        """
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

        # 尝试对样本远端路径做只读 fs_get（路径通常不存在，仅测连通与字段形态）
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
                # 路径不存在属正常，记录但不影响 ok
                result["fs_get"] = {"path": sample_remote, "error": str(e)[:200]}
        return result

    def _load_stats(self) -> Dict[str, Any]:
        """加载统计并迁移旧结构。

        旧: {strm_count, last_strm_time, recent_files:[{name,time}]}
        新: {upload_count, last_upload_time, recent_uploads:[...],
             strm_count, last_strm_time, recent_strms:[...]}
        """
        try:
            data = self.get_data("stats")
            if isinstance(data, dict):
                # 迁移 recent_files -> recent_strms
                recent_strms = data.get("recent_strms")
                if recent_strms is None:
                    recent_strms = [
                        {"name": item.get("name", ""), "path": "",
                         "remote": "", "time": item.get("time", ""),
                         "created": True}
                        for item in (data.get("recent_files") or [])
                        if isinstance(item, dict)
                    ]
                data.setdefault("upload_count", 0)
                data.setdefault("last_upload_time", "")
                data.setdefault("recent_uploads", [])
                data.setdefault("strm_count", 0)
                data.setdefault("last_strm_time", "")
                data.setdefault("recent_strms", recent_strms)
                return data
        except Exception as e:
            logger.debug(f"【云端STRM】读取统计失败: {e}")
        return {
            "upload_count": 0, "last_upload_time": "", "recent_uploads": [],
            "strm_count": 0, "last_strm_time": "", "recent_strms": [],
        }

    def _save_stats(self) -> None:
        try:
            self.save_data("stats", self._stats)
        except Exception as e:
            logger.debug(f"【云端STRM】保存统计失败: {e}")

    def _record_upload_stat(self, local_path: str, remote_path: str,
                            size: int = 0, status: str = "uploaded") -> None:
        """记录上传统计。

        - status="uploaded"：真正上传成功，upload_count += 1
        - status="skipped"：远端已存在跳过，不增加 upload_count
        - remote_deleted/local_deleted：更新已有记录状态（不重复插入）
        - last_upload_time 在 uploaded/skipped 时都更新
        - recent_uploads 最多 20 条，单批次内按 remote 去重
        """
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
            # 删除操作：更新已有记录状态而非插入新记录
            updated = [i for i, item in enumerate(recent) if item.get("remote") == remote_path]
            if updated:
                idx = updated[0]
                recent[idx]["status"] = status
                recent[idx]["time"] = now
                if local_path:
                    recent[idx]["local"] = local_path
                self._stats["recent_uploads"] = recent[:20]
                self._save_stats()
                logger.debug(f"【云端STRM】已更新上传统计状态: {remote_path} -> {status}")
                return
            # 未找到已有记录则插入新记录（兜底）
            recent.insert(0, {
                "name": display_name,
                "local": local_path,
                "remote": remote_path,
                "size": size,
                "time": now,
                "status": status,
            })
            self._stats["recent_uploads"] = recent[:20]
        else:
            # 上传/跳过：有 remote 去重
            should_insert = status not in ("uploaded", "skipped") or not any(
                item.get("remote") == remote_path for item in recent
            )
            if should_insert:
                recent.insert(0, {
                    "name": display_name,
                    "local": local_path,
                    "remote": remote_path,
                    "size": size,
                    "time": now,
                    "status": status,
                })
                self._stats["recent_uploads"] = recent[:20]
        self._save_stats()

    def _record_strm_stat(self, strm_path: Optional[Path], created: bool,
                          remote_path: str = "", local_path: str = "") -> None:
        """记录 STRM 生成统计。

        - created=True 时 strm_count += 1
        - created=False 不增加累计数量
        - last_strm_time 总是更新
        - recent_strms 最多 20 条
        """
        if not strm_path:
            return
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self._stats:
            self._stats = self._load_stats()
        if created:
            self._stats["strm_count"] = int(self._stats.get("strm_count") or 0) + 1
        self._stats["last_strm_time"] = now
        recent = list(self._stats.get("recent_strms") or [])
        recent.insert(0, {
            "name": strm_path.name,
            "path": str(strm_path),
            "local": local_path,
            "remote": remote_path,
            "time": now,
            "created": bool(created),
        })
        self._stats["recent_strms"] = recent[:20]
        self._save_stats()

    def _remove_strm_stat(self, strm_path: str) -> None:
        """从最近 STRM 列表中移除指定记录（文件已删除）。"""
        if not self._stats:
            self._stats = self._load_stats()
        recent = list(self._stats.get("recent_strms") or [])
        before = len(recent)
        recent = [item for item in recent if item.get("path") != strm_path]
        if len(recent) < before:
            self._stats["recent_strms"] = recent[:20]
            self._save_stats()
            logger.info(f"【云端STRM】已从 STRM 列表移除记录: {strm_path}")

    # ============================================================
    # 通知
    # ============================================================
    def _notify(self, title: str, text: str) -> None:
        """发送通知（MP 内建消息渠道）+ 系统消息。"""
        try:
            from app.schemas import NotificationType
            self.post_message(
                mtype=NotificationType.Plugin, title=title, text=text,
            )
        except Exception as e:
            logger.debug(f"【云端STRM】通知发送失败: {e}")
        try:
            self.systemmessage.put(message=text, role="plugin", title=title)
        except Exception as e:
            logger.debug(f"【云端STRM】系统消息写入失败: {e}")

    # ============================================================
    # 配置持久化与表单
    # ============================================================
    def _update_config(self) -> None:
        """持久化当前配置。"""
        self.update_config({
            "enabled": self._enabled,
            "moviepilot_address": self._moviepilot_address,
            "cloud_storage_type": self._cloud_storage_type,
            "alist_url": self._alist_url,
            "alist_token": self._alist_token,
            "alist_target_path": self._alist_target_path,
            "upload_path_mappings": self._upload_path_mappings,
            "strm_path_mappings": self._strm_path_mappings,
            "local_strm_paths": self._local_strm_paths,
            "local_media_path": self._local_media_path,
            "strm_output_path": self._strm_output_path,
            "sync_mode": self._sync_mode,
            "overwrite_mode": self._overwrite_mode,
            "exclude_patterns": self._exclude_patterns,
            "event_filters": self._event_filters,
            "transfer_monitor_media_server_refresh_enabled": self._refresh_enabled,
            "transfer_monitor_mediaservers": self._mediaservers,
            "transfer_mp_mediaserver_paths": self._transfer_mp_mediaserver_paths,
            "notify_enabled": self._notify_enabled,
            "rmt_mediaext": ",".join(self._rmt_mediaext) if self._rmt_mediaext else DEFAULT_MEDIA_EXTS,
            "upload_concurrency": self._upload_concurrency,
            "once_sync": self._once_sync,
            "strm_url_mode": self._strm_url_mode,
            "resolve_final_url": self._resolve_final_url,
            "direct_link_mode": self._direct_link_mode,
            "redirect_cache_ttl": self._redirect_cache_ttl,
            "head_probe_mode": self._head_probe_mode,
            "sse_enabled": self._sse_enabled,
            "emby_proxy_enabled": self._emby_proxy_enabled,
            "emby_server_url": self._emby_server_url,
            "emby_proxy_host": self._emby_proxy_host,
            "emby_proxy_port": self._emby_proxy_port,
            "manual_upload_action": self._manual_upload_action,
            "manual_upload_target": self._manual_upload_target,
            "manual_strm_target": self._manual_strm_target,
            "manual_confirm": self._manual_confirm,
            "manual_execute": self._manual_execute,
        })

    @staticmethod
    def _build_exclude_spec(raw: str) -> Optional[pathspec.PathSpec]:
        """构建 gitignore 风格排除规则（一行一条）。"""
        lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
        if not lines:
            return None
        try:
            return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, lines)
        except Exception as e:
            logger.warning(f"【云端STRM】排除规则解析失败，忽略: {e}")
            return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页面（4 Tab：基础 / 播放·302 / 云存储·路径 / 同步·刷新）。"""
        # 媒体服务器选项
        try:
            mediaserver_items = [
                {"title": c.name, "value": c.name}
                for c in MediaServerHelper().get_configs().values()
                if c.name
            ]
        except Exception:
            mediaserver_items = []

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {
                            "model": "_tabs",
                            "color": "primary",
                            "class": "mb-4",
                        },
                        "content": [
                            {"component": "VTab", "props": {"value": "base"}, "text": "基础设置"},
                            {"component": "VTab", "props": {"value": "play"}, "text": "播放设置"},
                            {"component": "VTab", "props": {"value": "cloud"}, "text": "路径设置"},
                            {"component": "VTab", "props": {"value": "sync"}, "text": "同步设置"},
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
                                                         "hint": "监听 MP 整理入库事件", "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "once_sync", "label": "立即全量上传并生成 STRM",
                                                         "hint": "保存后触发一次全量扫描→上传→STRM",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "notify_enabled", "label": "任务完成通知",
                                                         "hint": "通过 MP 通知同步结果", "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "upload_concurrency", "label": "上传并发数",
                                                         "placeholder": "3", "type": "number",
                                                         "hint": "并发上传文件数", "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                     ]},
                                ],
                            },
                            # ---- Tab: 播放 / 302 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "play"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "播放设置"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "emby_proxy_enabled", "label": "启用 Emby 302 代理",
                                                         "hint": "代理 Emby 播放请求并拦截回源",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "emby_server_url", "label": "Emby 原始地址",
                                                         "placeholder": DEFAULT_EMBY_SERVER_URL,
                                                         "hint": "客户端改连代理端口", "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "emby_proxy_host", "label": "代理监听地址",
                                                         "placeholder": DEFAULT_EMBY_PROXY_HOST,
                                                         "hint": "通常保持 0.0.0.0",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 3},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "emby_proxy_port", "label": "代理监听端口",
                                                         "placeholder": str(DEFAULT_EMBY_PROXY_PORT), "type": "number",
                                                         "hint": "例如 8095", "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                     ]},
                                ],
                            },
                            # ---- Tab: 云存储 / 路径 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "cloud"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "云端存储"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSelect", "props": {
                                                         "model": "cloud_storage_type", "label": "云端存储类型",
                                                         "items": [{"title": "AList / OpenList", "value": "alist"}],
                                                         "disabled": True,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "alist_url", "label": "AList/OpenList 地址",
                                                         "placeholder": DEFAULT_ALIST_URL,
                                                         "hint": "服务地址", "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "alist_token", "label": "AList/OpenList Token",
                                                         "hint": "API Token", "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                         {"component": "div", "props": {"class": "text-h6 mb-4 mt-6"}, "text": "上传与 STRM 路径映射"},
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
                                                         "hint": "只处理这些本地根目录",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-3"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12},
                                                     "content": [{"component": "VTextarea", "props": {
                                                         "model": "strm_path_mappings",
                                                         "label": "STRM 映射（云端路径#本地 STRM 目录）",
                                                         "rows": 3,
                                                         "placeholder": DEFAULT_STRM_PATH_MAPPINGS,
                                                         "hint": "按云端路径匹配到 STRM 输出目录",
                                                         "persistent-hint": False,
                                                     }}],
                                                 },
                                             ],
                                         },
                                     ]},
                                ],
                            },
                            # ---- Tab: 同步 / 刷新 ----
                            {
                                "component": "VWindowItem",
                                "props": {"value": "sync"},
                                "content": [
                                    {"component": "div", "props": {"class": "pa-5"},
                                     "content": [
                                         {"component": "div", "props": {"class": "text-h6 mb-4"}, "text": "同步与过滤"},
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
                                                             {"title": "移动（上传+STRM 成功后删本地）", "value": "move"},
                                                         ],
                                                         "hint": "不删云端文件", "persistent-hint": False,
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSelect", "props": {
                                                         "model": "overwrite_mode", "label": "STRM 覆盖模式",
                                                         "items": [
                                                             {"title": "从不（跳过已存在）", "value": "never"},
                                                             {"title": "总是（覆盖已有 STRM）", "value": "always"},
                                                         ],
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VTextField", "props": {
                                                         "model": "rmt_mediaext", "label": "可处理媒体扩展名",
                                                         "placeholder": "mp4,mkv,ts,iso,...",
                                                         "hint": "逗号分隔", "persistent-hint": False,
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
                                                         "model": "exclude_patterns", "label": "排除规则（gitignore 语法，一行一条）",
                                                         "rows": 3, "placeholder": DEFAULT_EXCLUDE_PATTERNS,
                                                         "hint": "命中规则的文件不上传也不生成 STRM", "persistent-hint": False,
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
                                                         "model": "event_filters", "label": "事件路径过滤（留空处理全部）",
                                                         "rows": 2, "placeholder": DEFAULT_EVENT_FILTERS,
                                                         "hint": "只处理这些本地路径前缀下的整理事件", "persistent-hint": False,
                                                     },
                                                 }],
                                             }],
                                         },
                                         {"component": "div", "props": {"class": "text-h6 mb-4 mt-6"}, "text": "媒体服务器刷新"},
                                         {
                                             "component": "VRow",
                                             "props": {"class": "mb-4"},
                                             "content": [
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 4},
                                                     "content": [{"component": "VSwitch", "props": {
                                                         "model": "transfer_monitor_media_server_refresh_enabled",
                                                         "label": "生成 STRM 后刷新媒体服务器",
                                                     }}],
                                                 },
                                                 {
                                                     "component": "VCol", "props": {"cols": 12, "md": 8},
                                                     "content": [{"component": "VSelect", "props": {
                                                         "model": "transfer_monitor_mediaservers", "label": "媒体服务器",
                                                         "items": mediaserver_items,
                                                         "multiple": True, "chips": True, "clearable": True,
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
                                                         "model": "transfer_mp_mediaserver_paths",
                                                         "label": "路径映射（Emby 路径#MP 路径）",
                                                         "rows": 2, "placeholder": DEFAULT_PATH_MAPPING,
                                                         "hint": "用于刷新媒体库", "persistent-hint": False,
                                                     },
                                                 }],
                                             }],
                                         },
                                     ]},
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "_tabs": "base",
            "enabled": False,
            "once_sync": False,
            "notify_enabled": True,
            "upload_concurrency": 3,
            "moviepilot_address": DEFAULT_MOVIEPILOT_ADDRESS,
            "strm_url_mode": DEFAULT_STRM_URL_MODE,
            "resolve_final_url": True,
            "direct_link_mode": "prefer_raw_url",
            "redirect_cache_ttl": 120,
            "head_probe_mode": "ok",
            "sse_enabled": False,
            "emby_proxy_enabled": False,
            "emby_server_url": DEFAULT_EMBY_SERVER_URL,
            "emby_proxy_host": DEFAULT_EMBY_PROXY_HOST,
            "emby_proxy_port": DEFAULT_EMBY_PROXY_PORT,
            "manual_upload_action": "none",
            "manual_upload_target": "",
            "manual_strm_target": "",
            "manual_confirm": False,
            "manual_execute": False,
            "cloud_storage_type": "alist",
            "alist_url": DEFAULT_ALIST_URL,
            "alist_token": "",
            "alist_target_path": DEFAULT_ALIST_TARGET_PATH,
            "upload_path_mappings": DEFAULT_UPLOAD_PATH_MAPPINGS,
            "strm_path_mappings": DEFAULT_STRM_PATH_MAPPINGS,
            "local_strm_paths": DEFAULT_LOCAL_STRM_PATHS,
            "local_media_path": DEFAULT_LOCAL_MEDIA_PATH,
            "strm_output_path": DEFAULT_STRM_OUTPUT_PATH,
            "sync_mode": "copy",
            "overwrite_mode": "never",
            "rmt_mediaext": DEFAULT_MEDIA_EXTS,
            "exclude_patterns": DEFAULT_EXCLUDE_PATTERNS,
            "event_filters": DEFAULT_EVENT_FILTERS,
            "transfer_monitor_media_server_refresh_enabled": True,
            "transfer_monitor_mediaservers": [],
            "transfer_mp_mediaserver_paths": DEFAULT_PATH_MAPPING,
        }
