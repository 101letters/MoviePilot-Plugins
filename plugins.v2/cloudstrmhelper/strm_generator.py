"""strm_generator.py — STRM 文件生成 + Emby/Jellyfin 媒体库刷新

实现要点（参考 p123strmhelper）：
- `.strm` 内容默认写入 AList/OpenList `/d/...` 下载地址，保留插件 `/redirect` 端点兼容旧 STRM。
- 文件名：原媒体文件 stem + `.strm`；`open(w, utf-8).write(url)` 不加换行。
- 路径重定向：按插件的 `本地媒体库路径#STRM输出目录` 映射生成。
- overwrite_mode：never=跳过已存在，always=覆盖。
- Emby 刷新：MediaServerHelper + RefreshMediaItem，支持路径映射替换（媒体服务器路径#MP路径）。
"""
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas import RefreshMediaItem


class StrmGenerator:
    """STRM 生成器。"""

    # redirect 端点路径前缀；插件 ID（类名）在运行时从 plugin 类取，避免写死字符串
    REDIRECT_PREFIX = "/api/v1/plugin"

    def __init__(self, plugin: Any):
        self.plugin = plugin

    @property
    def _plugin_id(self) -> str:
        """插件 ID = 主类名（规范：不要把插件 ID 写死到字符串里）。"""
        return type(self.plugin).__name__

    @property
    def _redirect_path(self) -> str:
        return f"{self.REDIRECT_PREFIX}/{self._plugin_id}/redirect"

    # ---- 路径计算 ----
    def _strm_output_path(self, local_path: str, remote_path: str = "") -> Optional[Path]:
        """计算 STRM 输出路径：优先按云端路径映射，兼容旧测试的本地路径映射。"""
        resolver = getattr(self.plugin, "_strm_output_path_for", None)
        if remote_path and callable(resolver):
            path = resolver(local_path, remote_path)
            if isinstance(path, Path):
                return path
            if isinstance(path, str) and path:
                return Path(path)

        local = Path(local_path)
        roots = getattr(self.plugin, "_local_media_roots", None) or [self.plugin._local_media_path]
        for root_value in roots:
            root = Path(root_value)
            try:
                rel = local.relative_to(root)
            except ValueError:
                if not self._has_prefix(local, root):
                    continue
                rel = self._relative_to(local, root)
            out_root = Path(self.plugin._strm_output_path)
            return out_root / rel.parent / (local.stem + ".strm")
        logger.debug(f"【STRM生成】文件不在本地媒体根下，跳过: {local_path}")
        return None

    @staticmethod
    def _has_prefix(full: Path, prefix: Path) -> bool:
        f, p = full.parts, prefix.parts
        return len(p) <= len(f) and f[:len(p)] == p

    @staticmethod
    def _relative_to(full: Path, prefix: Path) -> Path:
        return Path(*full.parts[len(prefix.parts):])

    # ---- STRM URL 构建 ----
    def _build_strm_url(self, remote_path: str) -> str:
        """构建 .strm 文件内容 URL，按插件 STRM URL 模式分流。

        - alist_direct（默认/推荐）：直接写入 AList/OpenList /d/... 下载地址，保留媒体后缀便于 Emby 识别。
        - cloud_raw_url（实验）：直接写入 AList/OpenList fs_get.raw_url/最终 CDN URL，绕过 MP/OpenList 数据流量。
        - moviepilot_redirect（兼容旧配置）：指向插件自带 /redirect 端点。
        """
        mode = getattr(self.plugin, "_strm_url_mode", "alist_direct")
        if mode == "cloud_raw_url":
            return self._build_cloud_raw_url(remote_path)
        if mode == "alist_direct":
            return self._build_alist_direct_url(remote_path)
        return self._build_moviepilot_redirect_url(remote_path)

    def _build_moviepilot_redirect_url(self, remote_path: str) -> str:
        """兼容旧模式：STRM 内容 = 指向插件 /redirect 端点的 URL。

        http://<mp_address>/api/v1/plugin/<PluginID>/redirect?apikey=<token>&path=<urlenc 远端路径>
        """
        mp_address = self._get_mp_address()
        from app.core.config import settings
        token = settings.API_TOKEN or ""
        return (
            f"{mp_address.rstrip('/')}{self._redirect_path}"
            f"?apikey={token}&path={quote(remote_path, safe='/')}"
        )

    def _build_alist_direct_url(self, remote_path: str) -> str:
        """实验模式：STRM 内容 = AList/OpenList /d/... 下载地址。

        构造 <alist_url>/d<quote(path)>，并尝试用 fs_get 取 sign 追加 ?sign=<sign>。
        注意：不写 raw_url（可能过期）；fs_get 失败仅 warning，生成无 sign 的 /d/ 地址，不中断 STRM 生成。
        """
        base = self._build_alist_download_url(remote_path)
        sign = ""
        alist_client = getattr(self.plugin, "_alist_client", None)
        if alist_client:
            try:
                info = alist_client.fs_get(remote_path) or {}
                sign = info.get("sign") or ""
            except Exception as e:
                logger.warning(f"【STRM生成】alist_direct 取 sign 失败，生成无 sign 地址: {remote_path} ({e})")
        if sign:
            return f"{base}?sign={sign}"
        return base

    def _build_cloud_raw_url(self, remote_path: str) -> str:
        """实验模式：STRM 内容 = AList/OpenList 返回的云盘 raw_url 或最终 CDN URL。

        这是真正绕过 MoviePilot/OpenList 数据流量的模式，但 raw_url 可能过期；适合只想要
        云盘厂商直链、且接受后续需要重新生成 STRM 的场景。
        """
        strict_raw = getattr(self.plugin, "_direct_link_mode", "prefer_raw_url") == "raw_url_only"
        alist_client = getattr(self.plugin, "_alist_client", None)
        if not alist_client:
            if strict_raw:
                raise Exception("cloud_raw_url 严格模式需要 AList/OpenList 客户端")
            logger.warning(f"【STRM生成】cloud_raw_url 需要 AList/OpenList 客户端，回退 /d 地址: {remote_path}")
            return self._build_alist_download_url(remote_path)

        try:
            info = alist_client.fs_get(remote_path) or {}
        except Exception as e:
            if strict_raw:
                raise Exception(f"cloud_raw_url 严格模式取 raw_url 失败: {e}")
            logger.warning(f"【STRM生成】cloud_raw_url 取 raw_url 失败，回退 /d 地址: {remote_path} ({e})")
            return self._build_alist_download_url(remote_path)

        raw_url = info.get("raw_url") or ""
        if raw_url:
            if getattr(self.plugin, "_resolve_final_url", False):
                try:
                    from .proxy_handler import ProxyHandler
                    final = ProxyHandler(alist_client)._resolve_final_url(raw_url, "")
                    if final:
                        return final
                except Exception as e:
                    logger.debug(f"【STRM生成】cloud_raw_url 预解析最终 URL 失败，使用 raw_url: {e}")
            return raw_url

        sign = info.get("sign") or ""
        if strict_raw:
            raise Exception("cloud_raw_url 严格模式未拿到 raw_url")
        logger.warning(f"【STRM生成】cloud_raw_url 未拿到 raw_url，回退 AList/OpenList /d 地址: {remote_path}")
        url = self._build_alist_download_url(remote_path)
        return f"{url}?sign={sign}" if sign else url

    def _build_alist_download_url(self, remote_path: str) -> str:
        alist_url = (getattr(self.plugin, "_alist_url", "") or "").rstrip("/")
        return f"{alist_url}/d{quote(remote_path, safe='/')}"

    def _get_mp_address(self) -> str:
        """获取 MP 内网访问地址：优先用户配置，回退 settings.MP_DOMAIN。"""
        addr = (self.plugin._moviepilot_address or "").strip()
        if addr:
            if not addr.startswith(("http://", "https://")):
                addr = "http://" + addr
            return addr
        from app.core.config import settings
        # MP_DOMAIN(url) 组合 APP_DOMAIN；无 domain 配置返回 None，回退到 localhost
        domain = settings.MP_DOMAIN("") if hasattr(settings, "MP_DOMAIN") else None
        if domain:
            return domain.rstrip("/")
        logger.warning("【STRM生成】未配置 MP 内网地址且 MP_DOMAIN 为空，回退 http://localhost:3000；"
                       "请在插件配置填写 MoviePilot 内网访问地址")
        return "http://localhost:3000"

    # ---- 防御性过滤（上传与 STRM 共用）----
    def _should_skip(self, local_path: str) -> bool:
        """扩展名 + 排除规则过滤，命中则跳过 STRM 生成。

        与上传阶段（transfer_listener/run_once）共用同一套规则，这里做防御性二次校验，
        避免外部调用绕过过滤。返回 True 表示应跳过。
        """
        # 扩展名白名单
        media_exts = getattr(self.plugin, "_rmt_mediaext", None) or []
        if media_exts:
            ext = Path(local_path).suffix.lower().lstrip(".")
            if ext not in media_exts:
                logger.debug(f"【STRM生成】扩展名不在白名单，跳过: {local_path}")
                return True
        # 排除规则（gitignore，路径相对本地媒体根）
        spec = getattr(self.plugin, "_exclude_spec", None)
        if spec:
            try:
                roots = getattr(self.plugin, "_local_media_roots", None) or [self.plugin._local_media_path]
                local = Path(local_path)
                for root_value in roots:
                    try:
                        rel = str(local.relative_to(root_value)).replace("\\", "/")
                    except ValueError:
                        if self._has_prefix(local, Path(root_value)):
                            rel = str(self._relative_to(local, Path(root_value))).replace("\\", "/")
                        else:
                            continue
                    if spec.match_file(rel):
                        logger.debug(f"【STRM生成】命中排除规则，跳过: {local_path}")
                        return True
            except Exception as e:
                logger.debug(f"【STRM生成】排除规则校验异常，放行: {e}")
        return False

    # ---- 生成 ----
    def generate(self, local_path: str, remote_path: str,
                 mediainfo: Any = None, meta: Any = None,
                 refresh: bool = True) -> Tuple[bool, Optional[Path], bool]:
        """生成单个 STRM 文件。返回 (是否成功, strm 路径, 是否新建)。

        local_path  : 本地媒体文件绝对路径（用于推导 STRM 输出位置）
        remote_path : AList 云端路径（写入 .strm 内容，播放时由 redirect 端点解析）
        mediainfo   : MediaInfo（用于 Emby 刷新 RefreshMediaItem）
        """
        try:
            # 防御性过滤：扩展名 + 排除规则（与上传阶段共用）
            if self._should_skip(local_path):
                return False, None, False

            strm_path = self._strm_output_path(local_path, remote_path)
            if strm_path is None:
                return False, None, False

            existed = strm_path.exists()
            if existed and self.plugin._overwrite_mode == "never":
                logger.debug(f"【STRM生成】已存在且 STRM 模式 never，跳过: {strm_path}")
                return True, strm_path, False

            strm_path.parent.mkdir(parents=True, exist_ok=True)
            strm_url = self._build_strm_url(remote_path)

            with open(strm_path, "w", encoding="utf-8") as f:
                f.write(strm_url)  # 不加换行（与 p123strmhelper 一致）

            logger.info(f"【STRM生成】生成成功: {strm_path} -> {remote_path}")

            # Emby/Jellyfin 刷新
            if refresh and self.plugin._refresh_enabled:
                try:
                    self.refresh_emby(strm_path, mediainfo)
                except Exception as e:
                    logger.error(f"【STRM生成】媒体服务器刷新失败: {e}", exc_info=True)

            return True, strm_path, not existed
        except Exception as e:
            logger.error(f"【STRM生成】生成失败: {local_path}: {e}", exc_info=True)
            return False, None, False

    # ---- Emby/Jellyfin 刷新 ----
    def refresh_emby(self, strm_path: Path, mediainfo: Any) -> None:
        """通知媒体服务器刷新指定条目（媒体服务器无关，支持 Emby/Jellyfin/Plex）。

        路径映射：transfer_mp_mediaserver_paths "媒体服务器路径#MP路径" 一行一条，
        将 MP 看到的 STRM 路径替换为媒体服务器看到的路径后再刷新。
        """
        self.refresh_emby_batch([(strm_path, mediainfo)])

    def refresh_emby_batch(self, refresh_targets: List[Tuple[Path, Any]]) -> None:
        """批量通知媒体服务器刷新，避免批量生成 STRM 时逐文件刷新媒体库。"""
        items: List[RefreshMediaItem] = []
        target_paths: List[str] = []
        for strm_path, mediainfo in refresh_targets or []:
            target_path_str = str(strm_path)
            target_path_str = self._map_media_server_path(target_path_str)
            target_paths.append(target_path_str)

            title = getattr(mediainfo, "title", None) or "未知"
            year = getattr(mediainfo, "year", None)
            mtype = getattr(mediainfo, "type", None)
            category = getattr(mediainfo, "category", None)
            items.append(RefreshMediaItem(
                title=title,
                year=year,
                type=mtype,
                category=category,
                target_path=Path(target_path_str),
            ))

        if not items:
            return

        # 选择媒体服务器并刷新
        helper = MediaServerHelper()
        name_filters = self.plugin._mediaservers or None
        services = helper.get_services(name_filters=name_filters)
        if not services:
            logger.warning("【STRM生成】未获取到已连接的媒体服务器，跳过刷新")
            return

        refreshed = False
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None:
                continue
            if hasattr(instance, "is_inactive") and instance.is_inactive():
                logger.warning(f"【STRM生成】媒体服务器 {name} 未连接，跳过")
                continue
            if hasattr(instance, "refresh_library_by_items"):
                instance.refresh_library_by_items(items)
                if len(items) == 1:
                    logger.info(f"【STRM生成】已通知 {name} 刷新: {target_paths[0]}")
                else:
                    logger.info(f"【STRM生成】已通知 {name} 批量刷新: {len(items)} 个 STRM")
                refreshed = True
            elif hasattr(instance, "refresh_root_library"):
                instance.refresh_root_library()
                logger.info(f"【STRM生成】已通知 {name} 全量刷新")
                refreshed = True
            else:
                logger.warning(f"【STRM生成】媒体服务器 {name} 不支持刷新")

        if not refreshed:
            logger.warning("【STRM生成】没有可用的媒体服务器刷新方法")

    def _map_media_server_path(self, target_path_str: str) -> str:
        """按配置把 MoviePilot 可见路径转换为媒体服务器可见路径。"""
        # 路径映射替换
        mapping = self._parse_path_mapping(self.plugin._transfer_mp_mediaserver_paths)
        for mp_path, ms_path in mapping:
            if target_path_str.startswith(mp_path):
                target_path_str = target_path_str.replace(mp_path, ms_path, 1)
                logger.debug(f"【STRM生成】媒体服务器路径替换: {mp_path} -> {ms_path}")
                break
        return target_path_str

    @staticmethod
    def _parse_path_mapping(raw: str) -> List[Tuple[str, str]]:
        """解析路径映射：每行 "媒体服务器路径#MP路径"。"""
        result: List[Tuple[str, str]] = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or "#" not in line:
                continue
            ms_path, mp_path = line.split("#", 1)
            ms_path, mp_path = ms_path.strip(), mp_path.strip()
            if ms_path and mp_path:
                result.append((mp_path, ms_path))  # (MP路径, 媒体服务器路径)，便于 startswith 匹配
        return result
