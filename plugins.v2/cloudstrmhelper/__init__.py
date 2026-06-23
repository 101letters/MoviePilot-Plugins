"""CloudStrmHelper — 云端STRM整理助手 MoviePilot V2 插件

链路：整理完成事件 → 复制本地文件到 AList 云端 → 生成 STRM（自托管 302）→ Emby 302 直链播放。

核心设计（详见 README「规格 vs 实现偏差」）：
- 触发：进程内 EventType.TransferComplete 事件（非 SSE）。
- STRM：.strm 内容指向插件自带 /redirect 端点，播放时 FsGet 取 raw_url 再 302。
- 上传：AList PUT /api/fs/put + As-Task 轮询。
- Emby 刷新：MediaServerHelper + RefreshMediaItem。
"""
import threading
from typing import Any, Dict, List, Optional, Tuple

import pathspec
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from cachetools import TTLCache, cached
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .cloud_sync import AlistClient, CloudSync, LocalCloudClient
from .proxy_handler import ProxyHandler
from .strm_generator import StrmGenerator
from .transfer_listener import TransferListener


# 默认可处理媒体扩展名（与 p123strmhelper 一致）
DEFAULT_MEDIA_EXTS = (
    "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v"
)


class CloudStrmHelper(_PluginBase):
    """云端STRM整理助手。"""

    # ---- 插件元数据（类属性，MoviePilot V2 无 package.v2.json）----
    plugin_name = "云端STRM整理助手"
    plugin_desc = "整理入库自动复制到AList并生成STRM，Emby 302直链播放"
    plugin_icon = ""  # 图标可后续补充 URL
    plugin_version = "1.0.0"
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
    _sync_lock = threading.Lock()

    # 302 解析缓存：按 (path, ua-hash) 缓存最终 URL，2 分钟 TTL（与 p123strmhelper 一致）
    _redirect_cache = TTLCache(maxsize=512, ttl=120)

    # ============================================================
    # 生命周期
    # ============================================================
    def init_plugin(self, config: Dict[str, Any] = None) -> None:
        """初始化：解析配置 → 持久化 → 先停 → 重建各模块。"""
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._moviepilot_address = (config.get("moviepilot_address") or "").strip()
            self._cloud_storage_type = (config.get("cloud_storage_type") or "alist").strip()
            self._alist_url = (config.get("alist_url") or "").strip()
            self._alist_token = (config.get("alist_token") or "").strip()
            self._alist_target_path = (config.get("alist_target_path") or "").strip()
            self._local_media_path = (config.get("local_media_path") or "").strip()
            self._strm_output_path = (config.get("strm_output_path") or "").strip()
            self._sync_mode = (config.get("sync_mode") or "new").strip()
            self._overwrite_mode = (config.get("overwrite_mode") or "never").strip()
            self._exclude_patterns = config.get("exclude_patterns") or ""
            self._event_filters = config.get("event_filters") or ""
            self._refresh_enabled = bool(config.get("transfer_monitor_media_server_refresh_enabled", True))
            self._mediaservers = config.get("transfer_monitor_mediaservers") or []
            self._transfer_mp_mediaserver_paths = config.get("transfer_mp_mediaserver_paths") or ""
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
        self._event_filter_prefixes = [
            p.strip() for p in (self._event_filters or "").splitlines() if p.strip()
        ]

        # 先停旧资源
        self.stop_service()

        if not self._enabled:
            return

        # 校验必填
        if not self._local_media_path or not self._strm_output_path:
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

        logger.info("【云端STRM】插件已启动：storage=%s, sync=%s, 并发=%d",
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
        elif st == "local":
            if not self._alist_target_path:
                raise Exception("local 类型需配置 alist_target_path（本地挂载目录）")
            return LocalCloudClient(self._alist_target_path)
        elif st == "webdav":
            from .cloud_sync import WebdavClient
            return WebdavClient()
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
        """本插件无数据仪表盘。"""
        return []

    # ============================================================
    # API 端点
    # ============================================================
    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：/redirect（302）、/status（状态）、/sync_now（手动触发）。"""
        return [
            {
                "path": "/redirect",
                "endpoint": self.redirect,
                "methods": ["GET", "HEAD"],
                "summary": "302跳转",
                "description": "解析云端路径为直链并 302 重定向（STRM 播放）",
            },
            {
                "path": "/status",
                "endpoint": self.status,
                "methods": ["GET"],
                "summary": "同步状态",
                "description": "查询当前云同步任务进度",
            },
            {
                "path": "/sync_now",
                "endpoint": self.sync_now,
                "methods": ["GET", "POST"],
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
        """整理完成事件 → 过滤分发 → 入队云同步。"""
        if not self._enabled:
            return
        if self._listener is None:
            return
        self._listener.handle(event)

    # ============================================================
    # 同步后回调（由 cloud_sync 调用）
    # ============================================================
    def _on_file_synced(self, local_path: str, remote_path: str,
                        mediainfo: Any = None, meta: Any = None) -> None:
        """单文件上传成功后生成 STRM。"""
        if self._strm_gen is None:
            return
        self._strm_gen.generate(local_path, remote_path, mediainfo, meta)

    # ============================================================
    # 全量同步
    # ============================================================
    def run_once(self) -> None:
        """全量同步：遍历 local_media_path 下所有媒体文件，增量上传 + 生成 STRM。"""
        if not self._enabled or not self._cloud_sync:
            logger.warning("【云端STRM】未启用或云同步未就绪，跳过全量同步")
            return
        local_root = self._local_media_path
        if not local_root:
            logger.warning("【云端STRM】未配置本地媒体路径，跳过全量同步")
            return

        import os
        from pathlib import Path

        cloud_root = self._alist_target_path.rstrip("/")
        media_exts = set(self._rmt_mediaext)
        exclude_spec = self._exclude_spec
        queued = 0
        skipped = 0

        logger.info(f"【云端STRM】开始全量同步: {local_root} -> {cloud_root}")
        self._cloud_sync.create_time = __import__("time").time()
        self._cloud_sync.scan_finish = False
        self._cloud_sync.finish = []

        for root, dirs, files in os.walk(local_root):
            for name in files:
                local_path = os.path.join(root, name)
                ext = Path(name).suffix.lower().lstrip(".")
                if ext not in media_exts:
                    skipped += 1
                    continue
                # 排除规则
                if exclude_spec:
                    try:
                        rel = str(Path(local_path).relative_to(local_root)).replace("\\", "/")
                    except ValueError:
                        rel = local_path
                    if exclude_spec.match_file(rel):
                        skipped += 1
                        continue
                # 云端路径
                try:
                    rel = Path(local_path).relative_to(local_root)
                except ValueError:
                    continue
                remote_path = cloud_root + "/" + str(rel).replace("\\", "/")
                # 增量判定
                try:
                    if not self._cloud_sync.need_upload(local_path, remote_path):
                        # 跳过上传但补 STRM
                        self._on_file_synced(local_path, remote_path, None, None)
                        skipped += 1
                        continue
                except Exception as e:
                    logger.warning(f"【云端STRM】增量判定异常，按需上传: {e}")
                self._cloud_sync.enqueue_file(local_path, remote_path, None, None)
                queued += 1

        self._cloud_sync.mark_scan_finish()
        logger.info(f"【云端STRM】全量扫描完成: 入队 {queued}，跳过 {skipped}")

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
                                            "hint": "开启后监听整理完成事件", "persistent-hint": True,
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "cloud_storage_type", "label": "云端存储类型",
                                            "items": [
                                                {"title": "AList", "value": "alist"},
                                                {"title": "本地挂载目录", "value": "local"},
                                                {"title": "WebDAV（未实现）", "value": "webdav"},
                                            ],
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "moviepilot_address", "label": "MoviePilot 内网地址",
                                            "placeholder": "http://192.168.1.10:3000",
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
                                            "placeholder": "http://192.168.31.5:5244",
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
                                            "placeholder": "/媒体库",
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "local_media_path", "label": "本地媒体库路径",
                                            "placeholder": "/media/movies",
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 4},
                                        "content": [{"component": "VTextField", "props": {
                                            "model": "strm_output_path", "label": "STRM 输出目录",
                                            "placeholder": "/strm/movies",
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
                                                {"title": "全同步（补传，预留删除）", "value": "full"},
                                            ],
                                        }}],
                                    },
                                    {
                                        "component": "VCol", "props": {"cols": 12, "md": 3},
                                        "content": [{"component": "VSelect", "props": {
                                            "model": "overwrite_mode", "label": "STRM 覆盖模式",
                                            "items": [
                                                {"title": "从不（跳过已存在）", "value": "never"},
                                                {"title": "总是（覆盖）", "value": "always"},
                                            ],
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
                                            "rows": 3, "placeholder": "*.tmp\n**/.DS_Store\n/sample/**",
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
                                            "rows": 2, "placeholder": "/media/movies\n/media/tv",
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
                            {"component": "span", "text": "媒体服务器与通知"},
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
                                            "rows": 2, "placeholder": "/media#/data",
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
            "moviepilot_address": "",
            "cloud_storage_type": "alist",
            "alist_url": "",
            "alist_token": "",
            "alist_target_path": "",
            "local_media_path": "",
            "strm_output_path": "",
            "sync_mode": "new",
            "overwrite_mode": "never",
            "exclude_patterns": "",
            "event_filters": "",
            "transfer_monitor_media_server_refresh_enabled": True,
            "transfer_monitor_mediaservers": [],
            "transfer_mp_mediaserver_paths": "",
            "notify_enabled": True,
            "rmt_mediaext": DEFAULT_MEDIA_EXTS,
            "upload_concurrency": 3,
            "once_sync": False,
        }
