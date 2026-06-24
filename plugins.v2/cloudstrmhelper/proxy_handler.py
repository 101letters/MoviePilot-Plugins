"""proxy_handler.py — /redirect 302 端点的解析逻辑

实现要点（参考 MediaWarp 的 AlistStrm 解析 + MediaRelay 的请求合并思想 + p123strmhelper 的自托管 302）：
- 收到播放请求（.strm 内容 URL）→ 解析 path 参数 → AList/OpenList FsGet 取真实直链。
- direct_link_mode 控制来源：优先 raw_url、严格 raw_url、或 AList/OpenList /d 兼容。
- resolve_final_url=True 时用 HEAD 跟随重定向取最终 URL（≤10 跳，循环检测），HEAD 失败回退 GET Range bytes=0-0，最终失败回退原始 URL 不中断播放。
- URL query 中可识别过期时间时携带 expires_at，端点缓存会提前失效，避免返回过期直链。
- head_probe_mode 由端点层使用（ok/redirect/resolve），本模块只负责解析。
- 日志脱敏：_safe_url_for_log 只保留 scheme://host/path，去掉 query value。
- 失败 raise，端点层捕获后写负缓存并返回 502 JSON。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from time import time
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit
from typing import Optional

import requests

from app.log import logger

# 跟随重定向的最大跳数（与 MediaWarp MaxRedirectAttempts 一致）
MAX_REDIRECT_ATTEMPTS = 10

# 最终 URL 解析的重试策略：(connect_timeout, read_timeout)
_RESOLVE_TIMEOUTS = ((3.0, 10.0), (3.0, 15.0), (5.0, 20.0))
_DIRECT_LINK_MODES = {"prefer_raw_url", "raw_url_only", "alist_download"}
_ABSOLUTE_EXPIRY_QUERY_KEYS = {
    "expires",
    "expire",
    "expires_at",
    "expire_at",
    "expiration",
    "expiry",
    "deadline",
    "e",
    "x-oss-expires",
    "x-cos-expires",
}


@dataclass(frozen=True)
class DirectLink:
    """一次 302 解析结果。

    source 用于诊断和响应头，不包含任何敏感 URL 值。
    """

    url: str
    source: str
    resolved_final: bool = False
    expires_at: Optional[float] = None

    def expires_soon(self, safety_seconds: int = 5, now: Optional[float] = None) -> bool:
        if not self.expires_at:
            return False
        current = time() if now is None else now
        return self.expires_at <= current + safety_seconds


def _safe_url_for_log(url: str) -> str:
    """日志脱敏：保留 scheme://host/path，清空 query value（只留 key）。

    避免 sign/token 等敏感信息进入日志。
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        # 只保留 query 的 key，不保留 value
        keys = [pair.split("=", 1)[0] for pair in parts.query.split("&") if pair]
        safe_query = "&".join(f"{k}=" for k in keys)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, ""))
    except Exception:
        # 解析失败：退而求其次，截断可能含敏感信息的 query
        return url.split("?", 1)[0] if "?" in url else url


def _normalize_direct_link_mode(value: str) -> str:
    """直链来源策略。

    prefer_raw_url：优先 raw_url，无 raw_url 回退 /d；
    raw_url_only：严格只允许 raw_url；
    alist_download：始终返回 /d 下载端点。
    """
    normalized = (value or "prefer_raw_url").strip().lower()
    aliases = {
        "prefer": "prefer_raw_url",
        "prefer_raw": "prefer_raw_url",
        "raw": "raw_url_only",
        "raw_url": "raw_url_only",
        "strict_raw": "raw_url_only",
        "strict": "raw_url_only",
        "alist": "alist_download",
        "alist_direct": "alist_download",
        "download": "alist_download",
        "d": "alist_download",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in _DIRECT_LINK_MODES:
        return normalized
    return "prefer_raw_url"


def _parse_epoch_seconds(value: str) -> Optional[float]:
    """解析 URL query 中常见的 epoch 秒/毫秒。"""
    try:
        ts = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    # 小数字常是 TTL 而不是 epoch；没有配套 date 时不要误判。
    if ts < 1_000_000_000:
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000
    return ts


def _parse_utc_compact_date(value: str) -> Optional[float]:
    """解析 S3/GCS 风格的 20260624T120000Z。"""
    text = str(value or "").strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S.%fZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _extract_url_expiry(url: str) -> Optional[float]:
    """从常见云盘/CDN签名 URL 中提取过期时间。

    只用于缓存提前失效；解析不到时返回 None，不影响播放。
    """
    try:
        params = {
            key.lower(): value
            for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        }
    except Exception:
        return None

    for key in _ABSOLUTE_EXPIRY_QUERY_KEYS:
        if key in params:
            ts = _parse_epoch_seconds(params[key])
            if ts:
                return ts

    for date_key, duration_key in (
        ("x-amz-date", "x-amz-expires"),
        ("x-goog-date", "x-goog-expires"),
    ):
        if date_key not in params or duration_key not in params:
            continue
        started_at = _parse_utc_compact_date(params[date_key])
        if not started_at:
            continue
        try:
            ttl = float(params[duration_key])
        except (TypeError, ValueError):
            continue
        if ttl > 0:
            return started_at + ttl

    return None


class ProxyHandler:
    """302 解析器：把 AList/OpenList 云端路径解析为可直链访问的 URL。"""

    def __init__(self, alist_client, follow_redirects: bool = True,
                 resolve_final_url: bool = True,
                 direct_link_mode: str = "prefer_raw_url"):
        # follow_redirects 保留兼容；实际由 resolve_final_url 控制
        self.alist = alist_client
        self.follow_redirects = follow_redirects
        self.resolve_final_url = resolve_final_url
        self.direct_link_mode = _normalize_direct_link_mode(direct_link_mode)

    def resolve(self, remote_path: str, ua: str = "",
                resolve_final_url: Optional[bool] = None) -> str:
        """把 AList/OpenList 云端路径解析为最终直链 URL。

        :param remote_path: AList 虚拟路径，如 /媒体库/电影/Foo.mkv
        :param ua: 客户端 User-Agent（解析最终 URL 时透传，部分 CDN 网关按 UA 限流）
        :param resolve_final_url: 是否跟随重定向取最终 URL；None 用实例默认
        :return: 可 302 跳转的最终 URL
        :raises Exception: 解析失败（端点层捕获写负缓存）
        """
        return self.resolve_link(remote_path, ua, resolve_final_url).url

    def resolve_link(self, remote_path: str, ua: str = "",
                     resolve_final_url: Optional[bool] = None) -> DirectLink:
        """把 AList/OpenList 云端路径解析为带来源信息的直链结果。"""
        if not self.alist:
            raise Exception("AList/OpenList 客户端未初始化")

        # 1. FsGet 取文件信息（含 raw_url / sign / size）
        try:
            info = self.alist.fs_get(remote_path)
        except Exception as e:
            if self.direct_link_mode == "raw_url_only":
                raise
            # AList/OpenList 的 /d 路由有时能直接命中文件，但 /api/fs/get 会因路径编码、
            # 缓存或驱动差异失败。兼容模式下回退无 sign /d，避免 /redirect 直接 502。
            logger.warning(f"【302跳转】FsGet 失败，回退 /d 下载地址: {remote_path} ({e})")
            link = DirectLink(
                url=self._build_alist_download_url({}, remote_path),
                source="alist_download_fallback",
                expires_at=None,
            )
            return self._resolve_or_return(link, ua, resolve_final_url)
        if not info:
            raise Exception(f"AList/OpenList FsGet 无响应: {remote_path}")
        if info.get("is_dir"):
            raise Exception(f"目标路径是目录而非文件: {remote_path}")

        link = self._build_direct_link(info, remote_path)
        logger.info(
            f"【302跳转】解析直链: {remote_path} -> {_safe_url_for_log(link.url)} "
            f"(source={link.source})"
        )

        return self._resolve_or_return(link, ua, resolve_final_url)

    def _resolve_or_return(self, link: DirectLink, ua: str,
                           resolve_final_url: Optional[bool] = None) -> DirectLink:
        """可选预解析最终 URL，并保留 DirectLink 元数据。"""
        do_resolve = self.resolve_final_url if resolve_final_url is None else resolve_final_url
        if do_resolve:
            final = self._resolve_final_url(link.url, ua)
            if final:
                return DirectLink(
                    url=final,
                    source=link.source,
                    resolved_final=final != link.url,
                    expires_at=_extract_url_expiry(final) or link.expires_at,
                )
        return link

    def _build_url(self, info: dict, remote_path: str) -> str:
        """从 FsGet 响应构建直链 URL。

        优先 raw_url（AList 上游真实直链，已含 sign/expiry）；
        无 raw_url 则用 AList 自身的 /d/<path>?sign=<sign> 下载端点。
        """
        return self._build_direct_link(info, remote_path).url

    def _build_direct_link(self, info: dict, remote_path: str) -> DirectLink:
        """从 FsGet 响应构建带来源信息的直链结果。"""
        raw_url = info.get("raw_url")
        if raw_url and self.direct_link_mode != "alist_download":
            return DirectLink(
                url=raw_url,
                source="raw_url",
                expires_at=_extract_url_expiry(raw_url),
            )
        if self.direct_link_mode == "raw_url_only":
            raise Exception("AList/OpenList 未返回 raw_url，严格云盘直链模式拒绝回退 /d")

        url = self._build_alist_download_url(info, remote_path)
        return DirectLink(
            url=url,
            source="alist_download",
            expires_at=_extract_url_expiry(url),
        )

    def _build_alist_download_url(self, info: dict, remote_path: str) -> str:
        sign = info.get("sign") or ""
        base = (self.alist.url or "").rstrip("/")
        # AList /d/*path 路由：直链下载，sign 校验
        download = f"{base}/d{quote(remote_path, safe='/')}"
        if sign:
            download += f"?sign={sign}"
        return download

    def _resolve_final_url(self, url: str, ua: str) -> str:
        """跟随重定向链取最终 URL，失败回退空串（端点层用原始 URL 兜底）。

        策略：HEAD → 失败回退 GET Range bytes=0-0（stream=True，只取响应头）→ 仍失败回退原始 URL。
        最多 MAX_REDIRECT_ATTEMPTS 跳，循环检测。多策略带 (connect, read) 超时重试。
        """
        for attempt, (connect_t, read_t) in enumerate(_RESOLVE_TIMEOUTS, 1):
            try:
                final = self._follow_chain(url, ua, connect_t, read_t, method="HEAD")
                if final is not None:
                    return final
                # HEAD 不支持（405 等）→ 回退 GET Range
                logger.debug(f"【302跳转】HEAD 未取到最终 URL，回退 GET Range: attempt={attempt}")
                final = self._follow_chain(url, ua, connect_t, read_t, method="GET",
                                           extra_headers={"Range": "bytes=0-0"})
                if final is not None:
                    return final
                # 本轮超时策略失败，下一轮加长超时
            except Exception as e:
                if attempt >= len(_RESOLVE_TIMEOUTS):
                    logger.warning(f"【302跳转】预解析最终 URL 全部失败，回退原始 URL: {_safe_url_for_log(url)} ({e})")
                    return ""
                logger.debug(f"【302跳转】预解析重试 {attempt}/{len(_RESOLVE_TIMEOUTS)}: {e}")
        logger.warning(f"【302跳转】预解析最终 URL 失败，回退原始 URL: {_safe_url_for_log(url)}")
        return ""

    def _follow_chain(self, url: str, ua: str, connect_t: float, read_t: float,
                      method: str = "HEAD", extra_headers: dict = None) -> Optional[str]:
        """跟随一条重定向链。返回最终 URL 或 None（应回退/重试）。

        None 表示「该 method 未取到最终 URL」（如 405/不允许），由调用方决定回退或重试。
        遇到非 3xx 的 2xx 成功响应，返回 resp.url（HEAD 时即最终地址）。
        """
        timeout = (connect_t, read_t)
        headers = {"User-Agent": ua} if ua else {}
        if extra_headers:
            headers.update(extra_headers)
        try:
            with requests.Session() as session:
                current = url
                visited = set()
                for _ in range(MAX_REDIRECT_ATTEMPTS + 1):
                    if current in visited:
                        logger.warning(f"【302跳转】检测到循环重定向，回退原始 URL: {_safe_url_for_log(url)}")
                        return ""
                    visited.add(current)
                    # GET Range 只用于拿响应头和最终 URL；stream=True 避免服务端忽略 Range 时下载完整文件。
                    resp = session.request(
                        method,
                        current,
                        headers=headers,
                        allow_redirects=False,
                        timeout=timeout,
                        stream=(method == "GET"),
                    )
                    code = resp.status_code
                    try:
                        if 300 <= code < 400:
                            location = resp.headers.get("location")
                            if not location:
                                return ""
                            current = urljoin(current, location)
                            continue
                        # 405 Method Not Allowed → HEAD 不被支持，回退调用方做 GET
                        if code == 405:
                            return None
                        # 4xx/5xx 不是可播放最终地址；HEAD 失败后回退 GET Range，GET 失败则重试/兜底原始 URL。
                        if code >= 400:
                            return None
                        # 非重定向成功：取最终请求 URL
                        return str(resp.url)
                    finally:
                        resp.close()
        except Exception as e:
            raise e
        return None
