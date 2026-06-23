"""proxy_handler.py — /redirect 302 端点的解析逻辑

实现要点（参考 MediaWarp 的 AlistStrm 解析 + p123strmhelper 的自托管 302）：
- 收到播放请求（.strm 内容 URL）→ 解析 path 参数 → AList FsGet 取真实直链。
- 优先 raw_url（AList 上游真实直链）；无则构建 {alist_url}/d{path}?sign={sign}。
- 可选：跟随重定向取最终 URL（HEAD，传客户端 UA，≤10 跳，循环检测）——减少客户端重定向次数。
- HEAD 请求由端点层放行（兼容 Infuse/Fileball 探测）。
- 缓存：在 __init__.py 的 @cached(TTLCache) 上做（按 path+ua），本模块只负责解析。
- 失败 raise，端点层捕获返回 500 JSON。
"""
from urllib.parse import quote

from app.log import logger

# 跟随重定向的最大跳数（与 MediaWarp MaxRedirectAttempts 一致）
MAX_REDIRECT_ATTEMPTS = 10


class ProxyHandler:
    """302 解析器：把 AList 云端路径解析为可直链访问的 URL。"""

    def __init__(self, alist_client, follow_redirects: bool = True):
        self.alist = alist_client
        self.follow_redirects = follow_redirects

    def resolve(self, remote_path: str, ua: str = "") -> str:
        """把 AList 云端路径解析为最终直链 URL。

        :param remote_path: AList 虚拟路径，如 /媒体库/电影/Foo.mkv
        :param ua: 客户端 User-Agent（解析最终 URL 时透传，部分 CDN 网关按 UA 限流）
        :return: 可 302 跳转的最终 URL
        :raises Exception: 解析失败
        """
        if not self.alist:
            raise Exception("AList 客户端未初始化")

        # 1. FsGet 取文件信息（含 raw_url / sign / size）
        info = self.alist.fs_get(remote_path)
        if not info:
            raise Exception(f"AList FsGet 无响应: {remote_path}")
        # AList FsGet 响应 data 为文件对象（非 list），含 raw_url/sign/name/is_dir
        if info.get("is_dir"):
            raise Exception(f"目标路径是目录而非文件: {remote_path}")

        url = self._build_url(info, remote_path)
        logger.info(f"【302跳转】解析直链: {remote_path} -> {url}")

        # 2. 可选：跟随重定向取最终 URL
        if self.follow_redirects:
            final = self._resolve_final_url(url, ua)
            if final:
                return final
        return url

    def _build_url(self, info: dict, remote_path: str) -> str:
        """从 FsGet 响应构建直链 URL。

        优先 raw_url（AList 上游真实直链，已含 sign/expiry）；
        无 raw_url 则用 AList 自身的 /d/<path>?sign=<sign> 下载端点。
        """
        raw_url = info.get("raw_url")
        if raw_url:
            return raw_url

        sign = info.get("sign") or ""
        base = (self.alist.url or "").rstrip("/")
        # AList /d/*path 路由：直链下载，sign 校验
        download = f"{base}/d{quote(remote_path, safe='/')}"
        if sign:
            download += f"?sign={sign}"
        return download

    def _resolve_final_url(self, url: str, ua: str) -> str:
        """跟随重定向链取最终 URL（HEAD，≤10 跳，循环检测）。

        失败时返回空串，端点层回退到原始 URL（仍 302，只是未预解析）。
        """
        try:
            import httpx
        except ImportError:
            # httpx 是 MoviePilot 自带依赖；若意外缺失则跳过预解析
            logger.debug("【302跳转】httpx 不可用，跳过最终 URL 预解析")
            return ""

        try:
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                current = url
                visited = set()
                chain = []
                method = "HEAD"
                for _ in range(MAX_REDIRECT_ATTEMPTS + 1):
                    if current in visited:
                        logger.warning(f"【302跳转】检测到循环重定向，回退原始 URL: {url}")
                        return ""
                    visited.add(current)
                    chain.append(current)
                    headers = {"User-Agent": ua} if ua else {}
                    resp = client.request(method, current, headers=headers)
                    if 300 <= resp.status_code < 400:
                        location = resp.headers.get("location")
                        if not location:
                            return ""
                        # 处理相对 Location
                        if location.startswith("http"):
                            current = location
                        else:
                            import httpx as _hx
                            current = str(_hx.URL(current).join(location))
                        continue
                    # 非重定向：取最终请求 URL
                    return str(resp.url)
        except Exception as e:
            logger.debug(f"【302跳转】预解析最终 URL 失败，回退原始: {url} ({e})")
            return ""
        return ""
