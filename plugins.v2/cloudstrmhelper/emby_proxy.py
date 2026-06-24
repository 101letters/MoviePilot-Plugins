"""Emby 302 前置代理。

参考 qmediasync 的处理方式：
- PlaybackInfo 进入代理后改写 MediaSources，诱导 Emby 客户端走 DirectStreamUrl。
- /videos|audio/{id}/stream 等播放请求进入代理后，先向 Emby 查询 PlaybackInfo.Path。
- Path 能映射到 AList/OpenList 云端路径或 STRM 内容时，解析云盘直链并 302。
- 解析不到时透明回源 Emby，保证兼容性优先。
"""
import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from app.log import logger

from .proxy_handler import DirectLink, _extract_url_expiry


MEDIA_SOURCE_ID_SEGMENT = "[[_]]"

REG_PLAYBACK_INFO = re.compile(r"(?i)^/.*items/([^/]+)/playbackinfo/?$")
REG_RESOURCE_STREAM = re.compile(r"(?i)^/.*(?:videos|audio)/([^/]+)/(?:stream|universal)(?:\.\w+)?/?$")
REG_RESOURCE_MASTER = re.compile(r"(?i)^/.*(?:videos|audio)/([^/]+)/(?:master)(?:\.\w+)?/?$")
REG_RESOURCE_MAIN = re.compile(r"(?i)^/.*(?:videos|audio)/([^/]+)/main\.m3u8/?$")
REG_RESOURCE_ORIGINAL = re.compile(r"(?i)^/.*(?:videos|audio)/([^/]+)/(?:original)(?:\.\w+)?/?$")
REG_ITEM_DOWNLOAD = re.compile(r"(?i)^/.*items/([^/]+)/download/?$")
REG_SUBTITLE = re.compile(r"(?i)/subtitles?/")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def create_emby_proxy_app(plugin, emby_server_url: str) -> FastAPI:
    """创建独立 Emby 302 前置代理 app。"""
    proxy = Emby302Proxy(plugin=plugin, emby_server_url=emby_server_url)
    app = FastAPI(title="CloudStrm Emby 302 Proxy", docs_url=None, redoc_url=None)

    @app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def root(request: Request):
        return await proxy.handle(request)

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def catch_all(full_path: str, request: Request):
        return await proxy.handle(request)

    return app


class Emby302Proxy:
    """qmediasync 风格的 Emby 前置 302 代理。"""

    def __init__(self, plugin, emby_server_url: str):
        self.plugin = plugin
        self.emby_server_url = (emby_server_url or "").rstrip("/")
        self.timeout = (5, 60)
        self.stream_timeout = (5, 300)

    async def handle(self, request: Request):
        """按 qmediasync 的路由顺序分发。"""
        if request.method == "OPTIONS":
            return self._options_response()

        path = request.url.path or "/"
        if self.is_playback_info_request(path):
            return await self.handle_playback_info(request)
        if self.is_openlist_redirect_request(path):
            return await self.handle_stream_redirect(request)
        return await self.proxy_origin(request)

    @staticmethod
    def is_playback_info_request(path: str) -> bool:
        return bool(REG_PLAYBACK_INFO.match(path or ""))

    @staticmethod
    def is_openlist_redirect_request(path: str) -> bool:
        if REG_SUBTITLE.search(path or ""):
            return False
        return bool(
            REG_RESOURCE_STREAM.match(path or "")
            or REG_RESOURCE_MASTER.match(path or "")
            or REG_RESOURCE_MAIN.match(path or "")
            or REG_ITEM_DOWNLOAD.match(path or "")
        )

    @staticmethod
    def is_original_resource_request(path: str) -> bool:
        return bool(REG_RESOURCE_ORIGINAL.match(path or ""))

    @staticmethod
    def item_id_from_path(path: str) -> str:
        for pattern in (
            REG_PLAYBACK_INFO,
            REG_RESOURCE_STREAM,
            REG_RESOURCE_MASTER,
            REG_RESOURCE_MAIN,
            REG_RESOURCE_ORIGINAL,
            REG_ITEM_DOWNLOAD,
        ):
            matched = pattern.match(path or "")
            if matched:
                return matched.group(1)
        return ""

    async def handle_playback_info(self, request: Request):
        """回源 Emby PlaybackInfo 后改写可直连的 MediaSources。"""
        body = await request.body()
        try:
            origin_resp = self.request_origin(request, body=body, stream=False)
        except Exception as e:
            logger.error(f"【Emby302代理】PlaybackInfo 回源失败: {e}", exc_info=True)
            return JSONResponse({"state": False, "message": "Emby 回源失败"}, status_code=502)

        if origin_resp.status_code >= 400:
            return self.response_from_origin(origin_resp)

        try:
            data = origin_resp.json()
        except Exception:
            return self.response_from_origin(origin_resp)

        path = request.url.path or ""
        item_id = self.item_id_from_path(path)
        api_key = self.api_key_from_request(request)
        patched, count = self.patch_playback_info(data, item_id=item_id, api_key=api_key)
        headers = {
            "Cache-Control": "no-store",
            "X-CloudStrm-Emby-Proxy": "playbackinfo",
            "X-CloudStrm-Patched-Sources": str(count),
        }
        return JSONResponse(patched, status_code=origin_resp.status_code, headers=headers)

    async def handle_stream_redirect(self, request: Request):
        """播放流请求：PlaybackInfo.Path -> 云盘直链 -> 302，失败回源。"""
        path = request.url.path or ""
        item_id = self.item_id_from_path(path)
        if not item_id:
            return await self.proxy_origin(request)

        media_source_id = self.media_source_id_from_request(request)
        api_key = self.api_key_from_request(request)
        ua = request.headers.get("User-Agent", "")

        try:
            playback = self.fetch_playback_info(
                item_id=item_id,
                api_key=api_key,
                media_source_id=media_source_id,
                request=request,
            )
            media_path, selected_source_id = self.select_media_path(playback, media_source_id)
            link = self.resolve_media_path(media_path, ua=ua)
        except Exception as e:
            logger.warning(f"【Emby302代理】解析直链失败，回源 Emby: item={item_id}, err={e}")
            link = None
            media_path = ""
            selected_source_id = ""

        if not link:
            return await self.proxy_origin(request)

        resp = RedirectResponse(url=link.url, status_code=302)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-CloudStrm-Emby-Proxy"] = "redirect"
        resp.headers["X-CloudStrm-Link-Source"] = link.source
        resp.headers["X-CloudStrm-Final-Resolved"] = "1" if link.resolved_final else "0"
        resp.headers["X-CloudStrm-Direct-Link"] = "1"
        if selected_source_id:
            resp.headers["X-CloudStrm-MediaSourceId"] = self.safe_header_value(selected_source_id)
        if media_path:
            resp.headers["X-CloudStrm-Media-Path-Mapped"] = "1"
        return resp

    async def proxy_origin(self, request: Request):
        """透明回源 Emby。"""
        body = await request.body()
        try:
            origin_resp = self.request_origin(request, body=body, stream=True)
        except Exception as e:
            logger.error(f"【Emby302代理】回源 Emby 失败: {e}", exc_info=True)
            return JSONResponse({"state": False, "message": "Emby 回源失败"}, status_code=502)
        return self.streaming_response_from_origin(origin_resp, request.method)

    def request_origin(self, request: Request, body: bytes = b"", stream: bool = False) -> requests.Response:
        url = self.origin_url(request)
        headers = self.forward_request_headers(request)
        timeout = self.stream_timeout if stream else self.timeout
        method = request.method.upper()
        data = body if method not in ("GET", "HEAD") and body else None
        return requests.request(
            method,
            url,
            headers=headers,
            data=data,
            stream=stream,
            allow_redirects=False,
            timeout=timeout,
        )

    def origin_url(self, request: Request) -> str:
        path = request.url.path or "/"
        query = request.url.query
        return f"{self.emby_server_url}{path}{'?' + query if query else ''}"

    def forward_request_headers(self, request: Request) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in ("host", "content-length"):
                continue
            headers[key] = value
        client_host = getattr(getattr(request, "client", None), "host", "")
        if client_host:
            headers.setdefault("X-Forwarded-For", client_host)
            headers.setdefault("X-Real-IP", client_host)
        return headers

    @staticmethod
    def response_headers(headers: Dict[str, str], keep_content_headers: bool = True) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            if not keep_content_headers and lower in ("content-encoding", "content-length"):
                continue
            result[key] = value
        return result

    def response_from_origin(self, origin_resp: requests.Response):
        headers = self.response_headers(origin_resp.headers, keep_content_headers=False)
        content = origin_resp.content
        origin_resp.close()
        return Response(content=content, status_code=origin_resp.status_code, headers=headers)

    def streaming_response_from_origin(self, origin_resp: requests.Response, method: str):
        headers = self.response_headers(origin_resp.headers, keep_content_headers=True)
        if method.upper() == "HEAD":
            origin_resp.close()
            return Response(status_code=origin_resp.status_code, headers=headers)

        def iter_content() -> Iterable[bytes]:
            try:
                for chunk in origin_resp.raw.stream(1024 * 1024, decode_content=False):
                    if chunk:
                        yield chunk
            finally:
                origin_resp.close()

        return StreamingResponse(
            iter_content(),
            status_code=origin_resp.status_code,
            headers=headers,
            media_type=origin_resp.headers.get("content-type"),
        )

    def fetch_playback_info(
        self,
        item_id: str,
        api_key: str,
        media_source_id: str = "",
        request: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """按 qmediasync 的 POST -> GET 顺序获取 PlaybackInfo。"""
        url = f"{self.emby_server_url}/Items/{quote(item_id)}/PlaybackInfo"
        params = {
            "reqformat": "json",
            "IsPlayback": "false",
            "AutoOpenLiveStream": "false",
        }
        origin_source_id = self.origin_media_source_id(media_source_id)
        if origin_source_id:
            params["MediaSourceId"] = origin_source_id
        if api_key:
            params["api_key"] = api_key

        headers = self.forward_request_headers(request) if request else {}
        for method in ("POST", "GET"):
            resp = requests.request(method, url, params=params, headers=headers, timeout=self.timeout)
            try:
                if resp.status_code < 400:
                    return resp.json()
            finally:
                resp.close()
        raise RuntimeError(f"PlaybackInfo 查询失败: item={item_id}")

    @staticmethod
    def select_media_path(playback: Dict[str, Any], media_source_id: str = "") -> Tuple[str, str]:
        sources = playback.get("MediaSources") or []
        if not isinstance(sources, list) or not sources:
            return "", ""
        origin_source_id = Emby302Proxy.origin_media_source_id(media_source_id)
        selected = sources[0]
        if origin_source_id:
            for source in sources:
                if str(source.get("Id") or "") == origin_source_id:
                    selected = source
                    break
        return str(selected.get("Path") or ""), str(selected.get("Id") or "")

    def patch_playback_info(self, data: Dict[str, Any], item_id: str, api_key: str = "") -> Tuple[Dict[str, Any], int]:
        """把可映射源改写为 DirectStream 播放。"""
        result = copy.deepcopy(data)
        sources = result.get("MediaSources") or []
        if not isinstance(sources, list):
            return result, 0

        patched_count = 0
        for source in sources:
            if not isinstance(source, dict):
                continue
            media_path = str(source.get("Path") or "")
            if not self.is_redirect_candidate(media_path):
                continue
            source_id = str(source.get("Id") or "")
            direct_stream_url = self.direct_stream_url(item_id=item_id, source_id=source_id, api_key=api_key)
            if direct_stream_url:
                source["DirectStreamUrl"] = direct_stream_url
            source["SupportsDirectPlay"] = True
            source["SupportsDirectStream"] = True
            source["SupportsTranscoding"] = False
            source["IsRemote"] = True
            for key in (
                "TranscodingUrl",
                "TranscodingSubProtocol",
                "TranscodingContainer",
                "TranscodeReasons",
            ):
                source.pop(key, None)
            patched_count += 1
        return result, patched_count

    @staticmethod
    def direct_stream_url(item_id: str, source_id: str, api_key: str = "") -> str:
        if not item_id:
            return ""
        params = [("MediaSourceId", source_id), ("Static", "true")]
        if api_key:
            params.append(("api_key", api_key))
        query = "&".join(f"{quote(k)}={quote(str(v))}" for k, v in params if v is not None)
        return f"/videos/{quote(item_id)}/stream?{query}"

    def resolve_media_path(self, media_path: str, ua: str = "") -> Optional[DirectLink]:
        """解析 Emby MediaSource.Path 到可跳转直链。"""
        candidate = self.normalize_media_path(media_path)
        if not candidate:
            return None

        strm_target = self.read_strm_target(candidate)
        if strm_target:
            candidate = self.normalize_media_path(strm_target)

        if self.is_remote_url(candidate):
            return self.direct_link_from_remote_url(candidate, ua=ua)

        remote_path = self.remote_path_from_media_path(candidate)
        if remote_path:
            return self.direct_link_from_cloud_path(remote_path, ua=ua)
        return None

    def is_redirect_candidate(self, media_path: str) -> bool:
        candidate = self.normalize_media_path(media_path)
        if not candidate:
            return False
        strm_target = self.read_strm_target(candidate)
        if strm_target:
            candidate = self.normalize_media_path(strm_target)
        return self.is_remote_url(candidate) or bool(self.remote_path_from_media_path(candidate))

    def direct_link_from_cloud_path(self, remote_path: str, ua: str = "") -> Optional[DirectLink]:
        plugin = self.plugin
        proxy = getattr(plugin, "_proxy", None)
        if proxy is None:
            return None
        if hasattr(plugin, "_cached_resolve") and hasattr(plugin, "_redirect_cache_key"):
            return plugin._cached_resolve(plugin._redirect_cache_key(remote_path, ua), remote_path, ua)
        if hasattr(proxy, "resolve_link"):
            return proxy.resolve_link(remote_path, ua)
        if hasattr(proxy, "resolve"):
            return DirectLink(url=proxy.resolve(remote_path, ua), source="legacy_proxy")
        return None

    def direct_link_from_remote_url(self, url: str, ua: str = "") -> DirectLink:
        final_url = url
        resolved_final = False
        proxy = getattr(self.plugin, "_proxy", None)
        if getattr(self.plugin, "_resolve_final_url", True) and hasattr(proxy, "_resolve_final_url"):
            final = proxy._resolve_final_url(url, ua)
            if final:
                final_url = final
                resolved_final = final_url != url
        return DirectLink(
            url=final_url,
            source="remote_url",
            resolved_final=resolved_final,
            expires_at=_extract_url_expiry(final_url),
        )

    def remote_path_from_media_path(self, media_path: str) -> str:
        candidate = self.normalize_media_path(media_path)
        if not candidate:
            return ""
        if self.is_cloud_path(candidate):
            return candidate
        if hasattr(self.plugin, "_build_remote_path"):
            try:
                return self.plugin._build_remote_path(candidate) or ""
            except Exception:
                return ""
        return ""

    def is_cloud_path(self, path: str) -> bool:
        if not path.startswith("/"):
            return False
        for root in self.cloud_roots():
            if self.has_posix_prefix(path, root):
                return True
        return False

    def cloud_roots(self) -> Iterable[str]:
        roots = []
        roots.extend([cloud for _, cloud in getattr(self.plugin, "_upload_mappings", []) or []])
        roots.extend([cloud for cloud, _ in getattr(self.plugin, "_strm_mappings", []) or []])
        target = getattr(self.plugin, "_alist_target_path", "")
        if target:
            roots.append(target)
        seen = set()
        for root in roots:
            root = str(root or "").strip().rstrip("/")
            if not root or root in seen:
                continue
            seen.add(root)
            yield root

    @staticmethod
    def has_posix_prefix(path: str, root: str) -> bool:
        path_norm = "/" + str(path or "").strip("/")
        root_norm = "/" + str(root or "").strip("/")
        return path_norm == root_norm or path_norm.startswith(root_norm + "/")

    @staticmethod
    def read_strm_target(path: str) -> str:
        fs_path = Emby302Proxy.filesystem_path(path)
        if not fs_path or Path(fs_path).suffix.lower() != ".strm":
            return ""
        if not os.path.isfile(fs_path):
            return ""
        try:
            with open(fs_path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    value = line.strip().strip("\"'")
                    if value:
                        return value
        except Exception as e:
            logger.debug(f"【Emby302代理】读取 STRM 失败: {fs_path}: {e}")
        return ""

    @staticmethod
    def normalize_media_path(value: str) -> str:
        text = str(value or "").strip().strip("\"'")
        if not text:
            return ""
        lower = text.lower()
        if lower.startswith("file://"):
            return unquote(urlsplit(text).path)
        if lower.startswith("nfs:"):
            stripped = text[4:]
            return stripped if stripped.startswith("/") else text
        return text

    @staticmethod
    def filesystem_path(value: str) -> str:
        text = Emby302Proxy.normalize_media_path(value)
        if not text:
            return ""
        if Emby302Proxy.is_remote_url(text):
            return ""
        return text

    @staticmethod
    def is_remote_url(value: str) -> bool:
        try:
            return urlsplit(value).scheme.lower() in ("http", "https")
        except Exception:
            return False

    @staticmethod
    def media_source_id_from_request(request: Request) -> str:
        values = parse_qs(request.url.query or "")
        return (values.get("MediaSourceId") or values.get("mediasourceid") or [""])[0]

    @staticmethod
    def origin_media_source_id(media_source_id: str) -> str:
        value = str(media_source_id or "")
        if MEDIA_SOURCE_ID_SEGMENT in value:
            return value.split(MEDIA_SOURCE_ID_SEGMENT, 1)[0]
        return value

    @staticmethod
    def api_key_from_request(request: Request) -> str:
        values = parse_qs(request.url.query or "")
        for key in ("api_key", "ApiKey", "X-Emby-Token", "X-MediaBrowser-Token"):
            if values.get(key):
                return values[key][0]
        for header in ("X-Emby-Token", "X-MediaBrowser-Token"):
            value = request.headers.get(header)
            if value:
                return value
        auth = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization") or ""
        match = re.search(r'Token="?([^",]+)"?', auth)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def safe_header_value(value: str) -> str:
        return re.sub(r"[\r\n]", "", str(value or ""))[:128]

    @staticmethod
    def _options_response():
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "86400",
            },
        )
