"""CloudStrmHelper — 云端STRM整理助手 MoviePilot V2 插件

链路：Phase 1 监听整理完成事件 → Phase 2 上传 AList → Phase 3 生成 STRM → Phase 4 刷新 Emby。
"""
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pathspec
from apscheduler.schedulers.background import BackgroundScheduler
from cachetools import TTLCache
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .cloud_sync import TASK_SKIPPED, TASK_SUCCEEDED, AlistClient, CloudSync
from .proxy_handler import ProxyHandler
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
DEFAULT_EXCLUDE_PATTERNS = "*.tmp\n**/.DS_Store\n/sample/**"
DEFAULT_EVENT_FILTERS = "/media/movies\n/media/tv"
DEFAULT_PATH_MAPPING = "/media#/data"


class CloudStrmHelper(_PluginBase):
    """云端STRM整理助手。"""

    # ---- 插件元数据（类属性；V2 索引同时写入仓库根 package.v2.json，version 须一致）----
    plugin_name = "云端STRM整理助手"
    plugin_desc = "整理入库自动复制到AList并生成STRM，Emby 302直链播放"
    # 图标：引用仓库 icons 目录下的图标文件（URL 形式，与官方插件一致）
    plugin_icon = "https://raw.githubusercontent.com/101letters/MoviePilot-Plugins/main/icons/cloudstrmhelper.png"
    plugin_version = "1.1.0"
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
    _local_media_path = ""
    _local_media_roots: List[str] = []
    _strm_output_path = ""
    _sync_mode = "new"
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

    _scheduler: Optional[BackgroundScheduler] = None
    _alist_client = None
    _cloud_sync: Optional[CloudSync] = None
    _strm_gen: Optional[StrmGenerator] = None
    _proxy: Optional[ProxyHandler] = None
    _listener: Optional[TransferListener] = None
    _sse_listener: Optional[MoviePilotSseListener] = None
    _sync_lock = threading.Lock()
    _stats: Dict[str, Any] = {}

    # 302 解析缓存：按 (path, ua-hash) 缓存最终 URL，2 分钟 TTL（与 p123strmhelper 一致）
    _redirect_cache = TTLCache(maxsize=512, ttl=120)

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
            self._local_media_path = (config.get("local_media_path") or DEFAULT_LOCAL_MEDIA_PATH).strip()
            self._strm_output_path = (config.get("strm_output_path") or DEFAULT_STRM_OUTPUT_PATH).strip()
            self._sync_mode = "new"
            self._overwrite_mode = "never"
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
            self._update_config()

        # 派生：排除规则 PathSpec、事件前缀
        self._exclude_spec = self._build_exclude_spec(self._exclude_patterns)
        self._local_media_roots = self._parse_path_lines(self._local_media_path)
        self._event_filter_prefixes = [
            p.strip() for p in (self._event_filters or "").splitlines() if p.strip()
        ]
        self._stats = self._load_stats()

        # 先停旧资源
        self.stop_service()

        if not self._enabled:
            return

        # 校验必填
        if not self._local_media_roots or not self._strm_output_path:
            logger.warning("【云端STRM】未配置本地媒体路径或 STRM 输出路径，插件不启动")
            return
        if not self._alist_target_path:
            logger.warning("【云端STRM】未配置云端目标路径，插件不启动")
            return

        # 构建云端客户端
        try:
            self._alist_client = self._build_cloud_client()
        except Exception as e:
            logger.error(f"【云端STRM】云端客户端初始化失败: {e}", exc_info=True)
            self._alist_client = None

        # 构建各模块
        self._proxy = ProxyHandler(self._alist_client) if self._alist_client else None
        self._strm_gen = StrmGenerator(self)
        self._cloud_sync = CloudSync(
            self, self._alist_client, sync_mode=self._sync_mode,
            concurrency=self._upload_concurrency,
        )
        self._cloud_sync.start()
        self._listener = TransferListener(self)
        self._sse_listener = MoviePilotSseListener(self)
        self._sse_listener.start()

        logger.info("【云端STRM】插件已启动：storage=%s, sync=%s, 并发=%d, SSE=on",
                    self._cloud_storage_type, self._sync_mode, self._upload_concurrency)

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
        """插件首页统计面板。"""
        stats = self._stats or self._load_stats()
        recent_files = stats.get("recent_files") or []
        rows = [
            {
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("name") or "-"},
                    {"component": "td", "text": item.get("time") or "-"},
                ],
            }
            for item in recent_files[:10]
        ]
        if not rows:
            rows = [{
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"colspan": 2, "class": "text-grey"}, "text": "暂无记录"},
                ],
            }]
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VCard", "props": {"variant": "outlined"},
                            "content": [
                                {"component": "VCardText", "content": [
                                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": "已生成 STRM 数量"},
                                    {"component": "div", "props": {"class": "text-h5"}, "text": str(stats.get("strm_count") or 0)},
                                ]},
                            ],
                        }],
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VCard", "props": {"variant": "outlined"},
                            "content": [
                                {"component": "VCardText", "content": [
                                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": "最近一次 STRM 生成时间"},
                                    {"component": "div", "props": {"class": "text-subtitle-1"}, "text": stats.get("last_strm_time") or "-"},
                                ]},
                            ],
                        }],
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VCard", "props": {"variant": "outlined"},
                            "content": [
                                {"component": "VCardText", "content": [
                                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": "最近入库文件"},
                                    {"component": "div", "props": {"class": "text-subtitle-1 text-truncate"}, "text": (recent_files[0].get("name") if recent_files else "-")},
                                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": (recent_files[0].get("time") if recent_files else "-")},
                                ]},
                            ],
                        }],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {"component": "VCardTitle", "text": "最近入库文件"},
                    {"component": "VDivider"},
                    {"component": "VTable", "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "文件名"},
                            {"component": "th", "text": "时间"},
                        ]}]},
                        {"component": "tbody", "content": rows},
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
        ]

    def redirect(self, request: Request, apikey: str = "", path: str = ""):
        """302 跳转端点：校验 apikey → 解析 path 为直链 → 302。

        HEAD 请求放行（兼容 Infuse/Fileball 探测）：返回 200 空 body 而非 302。
        """
        # 鉴权
        if not apikey or apikey != (settings.API_TOKEN or ""):
            return JSONResponse({"state": False, "message": "鉴权失败"}, status_code=401)
        if not path:
            return JSONResponse({"state": False, "message": "缺少 path 参数"}, status_code=400)

        # HEAD 放行（部分客户端先 HEAD 探测）
        if request.method == "HEAD":
            if self._proxy is None:
                return JSONResponse({"state": False, "message": "代理未初始化"}, status_code=503)
            return JSONResponse({"state": True}, status_code=200)

        if self._proxy is None:
            return JSONResponse({"state": False, "message": "代理未初始化（AList 未连接）"}, status_code=503)

        ua = request.headers.get("User-Agent", "")
        try:
            # 缓存按 (path, ua) —— ua 可能很长，用前 64 字符做 cache key 避免爆缓存
            cache_key = (path, ua[:64])
            url = self._cached_resolve(cache_key, path, ua)
        except Exception as e:
            logger.error(f"【302跳转】解析失败: path={path}, err={e}", exc_info=True)
            return JSONResponse({"state": False, "message": f"解析直链失败: {e}"}, status_code=500)

        return RedirectResponse(url=url, status_code=302)

    def _cached_resolve(self, cache_key, path: str, ua: str) -> str:
        """带 TTL 缓存的解析（缓存失效或异常时重新解析）。"""
        cached_url = self._redirect_cache.get(cache_key)
        if cached_url:
            return cached_url
        url = self._proxy.resolve(path, ua)
        self._redirect_cache[cache_key] = url
        return url

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

    def _safe_run_once(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            logger.error(f"【云端STRM】手动同步异常: {e}", exc_info=True)

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
                        mediainfo: Any = None, meta: Any = None) -> None:
        """Phase 2 单文件完成后执行 Phase 3/4。"""
        if self._strm_gen is None:
            return
        ok, strm_path, created = self._strm_gen.generate(local_path, remote_path, mediainfo, meta)
        if ok:
            self._record_strm_stat(strm_path, created)

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

        logger.info(f"【云端STRM】开始全量同步: {local_roots} -> {self._alist_target_path.rstrip('/')}")
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
            if item.status in (TASK_SUCCEEDED, TASK_SKIPPED):
                ready_for_strm.append((item.local_path, item.remote_path, item.mediainfo, item.meta))
        logger.info(f"【云端STRM】全量 Phase 2 完成：可生成 STRM {len(ready_for_strm)} 条")
        for local_path, remote_path, mediainfo, meta in ready_for_strm:
            self._on_file_synced(local_path, remote_path, mediainfo, meta)
        logger.info("【云端STRM】全量 Phase 3/4 完成")

    # ============================================================
    # 路径映射与统计
    # ============================================================
    @staticmethod
    def _parse_path_lines(raw: str) -> List[str]:
        """解析多行路径配置。"""
        return [line.strip().rstrip("/") for line in (raw or "").splitlines() if line.strip()]

    def _build_remote_path(self, local_path: str) -> Optional[str]:
        """本地媒体路径 → AList 云端路径。"""
        rel = self._relative_to_local_roots(local_path)
        if rel is None:
            return None
        rel_str = str(rel).replace("\\", "/").lstrip("/")
        return f"{self._alist_target_path.rstrip('/')}/{rel_str}".rstrip("/")

    def _relative_to_local_roots(self, local_path: str) -> Optional[Path]:
        local = Path(local_path)
        for root in self._local_media_roots or self._parse_path_lines(self._local_media_path):
            try:
                return local.relative_to(root)
            except ValueError:
                continue
        return None

    def _remote_relative_path(self, remote_path: str) -> Optional[Path]:
        """AList 云端路径 → 相对云端目标根的路径。"""
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

    def _strm_output_path_from_remote(self, remote_path: str) -> Optional[Path]:
        """AList 云端路径 → STRM 输出路径。"""
        rel = self._remote_relative_path(remote_path)
        if rel is None:
            return None
        return Path(self._strm_output_path) / rel.parent / (rel.stem + ".strm")

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
        stats = self._stats or self._load_stats()
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
                "local_media_roots": local_roots,
                "strm_output_path": self._strm_output_path,
                "sync_mode": self._sync_mode,
                "overwrite_mode": self._overwrite_mode,
                "upload_concurrency": self._upload_concurrency,
                "media_exts": self._rmt_mediaext,
                "event_filters": self._event_filter_prefixes,
                "refresh_enabled": self._refresh_enabled,
                "mediaservers": self._mediaservers,
                "path_mapping": self._transfer_mp_mediaserver_paths,
            },
            "modules": {
                "sse_listener": bool(self._sse_listener),
                "alist_client": bool(self._alist_client),
                "cloud_sync": bool(self._cloud_sync),
                "strm_generator": bool(self._strm_gen),
                "proxy": bool(self._proxy),
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
        """AList 只读连通性探测。"""
        if not self._alist_client:
            return {"ok": False, "message": "AList 客户端未初始化"}
        try:
            user = self._alist_client.verify()
            return {"ok": True, "user": user}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def _load_stats(self) -> Dict[str, Any]:
        try:
            data = self.get_data("stats")
            if isinstance(data, dict):
                data.setdefault("strm_count", 0)
                data.setdefault("last_strm_time", "")
                data.setdefault("recent_files", [])
                return data
        except Exception as e:
            logger.debug(f"【云端STRM】读取统计失败: {e}")
        return {"strm_count": 0, "last_strm_time": "", "recent_files": []}

    def _save_stats(self) -> None:
        try:
            self.save_data("stats", self._stats)
        except Exception as e:
            logger.debug(f"【云端STRM】保存统计失败: {e}")

    def _record_strm_stat(self, strm_path: Optional[Path], created: bool) -> None:
        """记录统计；跳过已存在 STRM 不重复增加累计数量。"""
        if not strm_path:
            return
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self._stats:
            self._stats = self._load_stats()
        if created:
            self._stats["strm_count"] = int(self._stats.get("strm_count") or 0) + 1
        self._stats["last_strm_time"] = now
        recent = list(self._stats.get("recent_files") or [])
        recent.insert(0, {"name": strm_path.name, "time": now})
        self._stats["recent_files"] = recent[:20]
        self._save_stats()

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
        """配置页面（Vuetify 组件树 + 默认值）。参照 p123strmhelper 表单范式。"""
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
            # 基础设置卡片
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-cog", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "基础设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSwitch", "props": {
                                            "model": "enabled", "label": "启用插件",
                                            "hint": "开启后监听 MoviePilot SSE 整理完成/入库完成事件", "persistent-hint": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "cloud_storage_type", "label": "云端存储类型",
                                            "items": [
                                                {"title": "AList / alist", "value": "alist"},
                                            ],
                                            "disabled": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "moviepilot_address", "label": "MoviePilot 内网地址",
                                            "placeholder": DEFAULT_MOVIEPILOT_ADDRESS,
                                            "hint": "用于构建 STRM 内的 302 跳转 URL，留空则用 MP_DOMAIN",
                                            "persistent-hint": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSwitch", "props": {
                                            "model": "once_sync", "label": "立刻全量同步",
                                            "hint": "保存后触发一次（用完自动关闭）", "persistent-hint": True,
                                        }}],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            # AList 设置卡片
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-cloud", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "云端（AList）设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "alist_url", "label": "AList 地址",
                                            "placeholder": DEFAULT_ALIST_URL,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "alist_token", "label": "AList Token",
                                            "hint": "AList 管理后台 → 设置 → 令牌", "persistent-hint": True,
                                        }}],
                                    },
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "alist_target_path", "label": "云端目标路径",
                                            "placeholder": DEFAULT_ALIST_TARGET_PATH,
                                        }}],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            # 本地与 STRM 路径卡片
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-folder-sync", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "本地与 STRM 路径"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextarea", "props": {
                                            "model": "local_media_path", "label": "本地媒体库路径",
                                            "rows": 2, "placeholder": DEFAULT_LOCAL_MEDIA_PATH,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 6},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "strm_output_path", "label": "STRM 输出目录",
                                            "placeholder": DEFAULT_STRM_OUTPUT_PATH,
                                        }}],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            # 同步设置卡片
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-sync", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "同步与过滤"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "sync_mode", "label": "同步模式",
                                            "items": [
                                                {"title": "仅新增（不删远端）", "value": "new"},
                                            ],
                                            "disabled": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "overwrite_mode", "label": "STRM 覆盖模式",
                                            "items": [
                                                {"title": "从不（跳过已存在）", "value": "never"},
                                            ],
                                            "disabled": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "upload_concurrency", "label": "并发上传数",
                                            "placeholder": "3", "type": "number",
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "rmt_mediaext", "label": "可处理媒体扩展名",
                                            "placeholder": "mp4,mkv,ts,iso,...",
                                        }}],
                                    },
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [{
                                    "component": "VCol", "props": {"cols": 12},
                                    "content": [{
                                        "component": "VTextarea", "props": {
                                            "model": "exclude_patterns", "label": "排除规则（gitignore 语法，一行一条）",
                                            "rows": 3, "placeholder": DEFAULT_EXCLUDE_PATTERNS,
                                        },
                                    }],
                                }],
                            },
                            {
                                "component": "VRow",
                                "content": [{
                                    "component": "VCol", "props": {"cols": 12},
                                    "content": [{
                                        "component": "VTextarea", "props": {
                                            "model": "event_filters", "label": "事件路径过滤（一行一个本地目录前缀，留空=全部处理）",
                                            "rows": 2, "placeholder": DEFAULT_EVENT_FILTERS,
                                        },
                                    }],
                                }],
                            },
                        ],
                    },
                ],
            },
            # 媒体服务器与通知卡片
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-server-network", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "媒体服务器设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VSwitch", "props": {
                                            "model": "transfer_monitor_media_server_refresh_enabled",
                                            "label": "生成 STRM 后刷新媒体服务器",
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "transfer_monitor_mediaservers", "label": "媒体服务器",
                                            "items": mediaserver_items,
                                            "multiple": True, "chips": True, "clearable": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VSwitch", "props": {
                                            "model": "notify_enabled", "label": "任务完成通知",
                                        }}],
                                    },
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [{
                                    "component": "VCol", "props": {"cols": 12},
                                    "content": [{
                                        "component": "VTextarea", "props": {
                                            "model": "transfer_mp_mediaserver_paths",
                                            "label": "路径映射（媒体服务器路径#MP路径，一行一条）",
                                            "rows": 2, "placeholder": DEFAULT_PATH_MAPPING,
                                            "hint": "媒体服务器与 MP 路径不同时用于刷新入库", "persistent-hint": True,
                                        },
                                    }],
                                }],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "moviepilot_address": DEFAULT_MOVIEPILOT_ADDRESS,
            "cloud_storage_type": "alist",
            "alist_url": DEFAULT_ALIST_URL,
            "alist_token": "",
            "alist_target_path": DEFAULT_ALIST_TARGET_PATH,
            "local_media_path": DEFAULT_LOCAL_MEDIA_PATH,
            "strm_output_path": DEFAULT_STRM_OUTPUT_PATH,
            "sync_mode": "new",
            "overwrite_mode": "never",
            "exclude_patterns": DEFAULT_EXCLUDE_PATTERNS,
            "event_filters": DEFAULT_EVENT_FILTERS,
            "transfer_monitor_media_server_refresh_enabled": True,
            "transfer_monitor_mediaservers": [],
            "transfer_mp_mediaserver_paths": DEFAULT_PATH_MAPPING,
            "notify_enabled": True,
            "rmt_mediaext": DEFAULT_MEDIA_EXTS,
            "upload_concurrency": 3,
            "once_sync": False,
        }
