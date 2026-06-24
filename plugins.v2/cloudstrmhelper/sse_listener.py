"""sse_listener.py — MoviePilot system/message SSE 监听。

该模块只负责监听与解析事件消息，不做媒体文件读写、不访问 AList。
"""
import json
import re
import threading
import time
from typing import Any, Iterable, List
from urllib.parse import urljoin

import requests

from app.core.config import settings
from app.log import logger


TRANSFER_KEYWORDS = (
    "整理完成",
    "入库完成",
    "转移完成",
    "transfercomplete",
    "transfer.complete",
)


class MoviePilotSseAuthError(RuntimeError):
    """MoviePilot SSE endpoint refused API-token based authentication."""


class MoviePilotSseListener:
    """监听 MoviePilot `/api/v1/system/message` SSE。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._stop_event = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="CloudStrmSseListener",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _run(self) -> None:
        backoff = 3
        while not self._stop_event.is_set():
            try:
                self._listen_once()
                backoff = 3
            except MoviePilotSseAuthError as e:
                logger.info(
                    "【SSE监听】鉴权失败，已停止 SSE 监听，仅保留内部 TransferComplete 事件兜底: %s",
                    e,
                )
                return
            except Exception as e:
                logger.warning(f"【SSE监听】连接异常，{backoff}s 后重试: {e}")
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _listen_once(self) -> None:
        base_url = self._get_base_url()
        endpoint = urljoin(base_url.rstrip("/") + "/", "api/v1/system/message")
        token = settings.API_TOKEN or ""

        logger.info(f"【SSE监听】连接 MoviePilot 消息流: {endpoint}")
        auth_failed_status = None
        for label, params, headers in self._auth_candidates(token):
            with requests.get(
                endpoint,
                params=params,
                headers=headers,
                stream=True,
                timeout=(10, 90),
            ) as resp:
                if resp.status_code == 200:
                    self._consume_lines(resp.iter_lines(decode_unicode=True))
                    return
                if resp.status_code not in (401, 403):
                    raise RuntimeError(f"HTTP {resp.status_code}")
                auth_failed_status = resp.status_code
                logger.debug(f"【SSE监听】鉴权方式失败: {label}, HTTP {resp.status_code}")
        raise MoviePilotSseAuthError(f"HTTP {auth_failed_status or 403}")

    @staticmethod
    def _auth_candidates(token: str):
        base_headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if not token:
            return [("anonymous", None, base_headers)]
        return [
            ("apikey+authorization", {"apikey": token}, {**base_headers, "Authorization": token}),
            ("apikey+bearer", {"apikey": token}, {**base_headers, "Authorization": f"Bearer {token}"}),
            ("apikey-only", {"apikey": token}, base_headers),
            ("authorization-only", None, {**base_headers, "Authorization": token}),
        ]

    def _consume_lines(self, lines: Iterable[str]) -> None:
        data_lines: List[str] = []
        for raw_line in lines:
            if self._stop_event.is_set():
                break
            line = raw_line or ""
            if not line:
                if data_lines:
                    self._handle_data("\n".join(data_lines))
                    data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines and not self._stop_event.is_set():
            self._handle_data("\n".join(data_lines))

    def _handle_data(self, data: str) -> None:
        if not data or data == "[DONE]":
            return
        payload: Any = data
        try:
            payload = json.loads(data)
        except Exception:
            pass

        text = self._stringify(payload)
        if not self._is_transfer_message(text):
            return

        paths = self._extract_candidate_paths(payload, text)
        if not paths:
            logger.debug(f"【SSE监听】命中整理消息但未提取到路径: {text[:200]}")
            return

        listener = getattr(self.plugin, "_listener", None)
        if listener is None:
            return
        records = listener.handle_sse_paths(paths, event_text=text)
        if records and hasattr(self.plugin, "_accept_phase1_records"):
            self.plugin._accept_phase1_records(records)

    def _get_base_url(self) -> str:
        addr = (getattr(self.plugin, "_moviepilot_address", "") or "").strip()
        if not addr and hasattr(settings, "MP_DOMAIN"):
            try:
                addr = settings.MP_DOMAIN("") or ""
            except Exception:
                addr = ""
        if not addr:
            addr = "http://localhost:3000"
        if not addr.startswith(("http://", "https://")):
            addr = "http://" + addr
        return addr

    @staticmethod
    def _is_transfer_message(text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword.lower() in lowered for keyword in TRANSFER_KEYWORDS)

    def _extract_candidate_paths(self, payload: Any, text: str) -> List[str]:
        values: List[str] = []
        for value in self._walk_strings(payload):
            values.extend(self._paths_from_text(value))
        values.extend(self._paths_from_text(text))

        prefixes = list(getattr(self.plugin, "_event_filter_prefixes", []) or [])
        if not prefixes:
            prefixes = list(getattr(self.plugin, "_local_media_roots", []) or [])

        result: List[str] = []
        seen = set()
        for value in values:
            normalized = value.strip().rstrip("，。；;,.")
            if not normalized.startswith("/"):
                continue
            if prefixes and not any(normalized.startswith(prefix) for prefix in prefixes):
                continue
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def _walk_strings(self, value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from self._walk_strings(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                yield from self._walk_strings(item)

    @staticmethod
    def _paths_from_text(text: str) -> List[str]:
        if not text:
            return []
        return re.findall(r"/[^\s\"'<>|]+", text)

    @staticmethod
    def _stringify(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
