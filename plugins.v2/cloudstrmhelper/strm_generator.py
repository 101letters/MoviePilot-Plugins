"""strm_generator.py — STRM 文件生成 + Emby/Jellyfin 媒体库刷新

实现要点（参考 p123strmhelper）：
- `.strm` 内容 = 指向插件自带 `/api/v1/plugin/<id>/redirect` 302 端点的 URL（自托管 302，链接不失效）。
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
        """构建 .strm 文件内容 URL：指向插件 redirect 端点。

        http://<mp_address>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<token>&path=<urlenc 远端路径>
        """
        mp_address = self._get_mp_address()
        from app.core.config import settings
        token = settings.API_TOKEN or ""
        return (
            f"{mp_address.rstrip('/')}{self._redirect_path}"
            f"?apikey={token}&path={quote(remote_path, safe='/')}"
        )

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

    # ---- 生成 ----
    def generate(self, local_path: str, remote_path: str,
                 mediainfo: Any = None, meta: Any = None) -> Tuple[bool, Optional[Path], bool]:
        """生成单个 STRM 文件。返回 (是否成功, strm 路径, 是否新建)。

        local_path  : 本地媒体文件绝对路径（用于推导 STRM 输出位置）
        remote_path : AList 云端路径（写入 .strm 内容，播放时由 redirect 端点解析）
        mediainfo   : MediaInfo（用于 Emby 刷新 RefreshMediaItem）
        """
        try:
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
            if self.plugin._refresh_enabled:
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
        target_path_str = str(strm_path)
        # 路径映射替换
        mapping = self._parse_path_mapping(self.plugin._transfer_mp_mediaserver_paths)
        for mp_path, ms_path in mapping:
            if target_path_str.startswith(mp_path):
                target_path_str = target_path_str.replace(mp_path, ms_path, 1)
                logger.debug(f"【STRM生成】媒体服务器路径替换: {mp_path} -> {ms_path}")
                break

        # 构建 RefreshMediaItem
        title = getattr(mediainfo, "title", None) or "未知"
        year = getattr(mediainfo, "year", None)
        mtype = getattr(mediainfo, "type", None)
        category = getattr(mediainfo, "category", None)
        items = [RefreshMediaItem(
            title=title,
            year=year,
            type=mtype,
            category=category,
            target_path=Path(target_path_str),
        )]

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
                logger.info(f"【STRM生成】已通知 {name} 刷新: {target_path_str}")
                refreshed = True
            elif hasattr(instance, "refresh_root_library"):
                instance.refresh_root_library()
                logger.info(f"【STRM生成】已通知 {name} 全量刷新")
                refreshed = True
            else:
                logger.warning(f"【STRM生成】媒体服务器 {name} 不支持刷新")

        if not refreshed:
            logger.warning("【STRM生成】没有可用的媒体服务器刷新方法")

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
