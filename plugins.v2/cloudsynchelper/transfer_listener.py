"""transfer_listener.py — Phase 1 整理完成事件记录。

只做事件识别、路径过滤与记录构造，不做本地文件检查、不访问 AList。
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from app.log import logger


@dataclass
class TransferRecord:
    """一次有效事件中提取出的待处理路径记录。"""

    source: str
    local_path: str
    remote_path: str
    mediainfo: Any = None
    meta: Any = None
    event_text: str = ""


class TransferListener:
    """整理完成事件记录器。"""

    def __init__(self, plugin):
        self.plugin = plugin

    def handle(self, event) -> List[TransferRecord]:
        """处理进程内 TransferComplete 事件，返回 Phase 1 记录。"""
        try:
            data = event.event_data or {}
            transferinfo = data.get("transferinfo")
            if transferinfo is None:
                logger.debug("【整理监听】事件无 transferinfo，跳过")
                return []

            success = getattr(transferinfo, "success", None)
            if success is False:
                logger.debug("【整理监听】整理未成功，跳过")
                return []

            mediainfo = data.get("mediainfo")
            meta = data.get("meta")

            target_diritem = getattr(transferinfo, "target_diritem", None)
            if target_diritem is None:
                target_item = getattr(transferinfo, "target_item", None)
                if target_item is not None:
                    target_diritem = target_item
            if target_diritem is None:
                logger.warning("【整理监听】事件无 target_diritem，无法定位目标目录")
                return []

            target_dir = getattr(target_diritem, "path", None) or ""
            if not target_dir:
                logger.warning("【整理监听】目标目录 path 为空，跳过")
                return []

            file_list = getattr(transferinfo, "file_list_new", None)
            if not file_list:
                file_list = getattr(transferinfo, "file_list", None)
            if not file_list:
                logger.debug(f"【整理监听】无新文件列表，跳过: {target_dir}")
                return []

            records = self._records_from_file_list(
                source="event",
                target_dir=target_dir,
                file_list=file_list,
                mediainfo=mediainfo,
                meta=meta,
            )
            logger.info(f"【整理监听】Phase 1 记录整理事件: {target_dir}, 有效路径 {len(records)}")
            return records
        except Exception as e:
            logger.error(f"【整理监听】处理事件异常: {e}", exc_info=True)
            return []

    def handle_sse_paths(self, paths: List[str], event_text: str = "") -> List[TransferRecord]:
        """处理 SSE 中提取出的路径，返回 Phase 1 记录。"""
        records: List[TransferRecord] = []
        for path in paths or []:
            local_path = str(path).strip()
            if not local_path:
                continue
            record = self._build_record(
                source="sse",
                local_path=local_path,
                mediainfo=None,
                meta=None,
                event_text=event_text,
            )
            if record:
                records.append(record)
        if records:
            logger.info(f"【SSE监听】Phase 1 记录整理事件，有效路径 {len(records)}")
        return records

    def _records_from_file_list(self, source: str, target_dir: str, file_list: list,
                                mediainfo, meta) -> List[TransferRecord]:
        records: List[TransferRecord] = []
        for item in file_list:
            local_path = self._resolve_file_item_path(target_dir, item)
            if not local_path:
                continue
            record = self._build_record(source, local_path, mediainfo, meta)
            if record:
                records.append(record)
        return records

    @staticmethod
    def _resolve_file_item_path(target_dir: str, item) -> str:
        raw_path = getattr(item, "path", None) or getattr(item, "file_path", None) or str(item)
        path = str(raw_path).strip().replace("\\", "/")
        if not path:
            return ""
        if Path(path).is_absolute():
            return path
        return os.path.join(target_dir, path.lstrip("/"))

    def _build_record(self, source: str, local_path: str, mediainfo, meta,
                      event_text: str = "") -> Optional[TransferRecord]:
        local_root = self.plugin._local_media_path
        if not local_root:
            logger.warning("【整理监听】未配置本地路径映射，跳过")
            return None

        exclude_spec = self.plugin._exclude_spec
        event_prefixes = self.plugin._event_filter_prefixes
        media_exts = self.plugin._rmt_mediaext

        if event_prefixes and not any(local_path.startswith(p) for p in event_prefixes):
            return None

        if exclude_spec and self._is_excluded(local_path, local_root, exclude_spec):
            logger.debug(f"【整理监听】命中排除规则，跳过: {local_path}")
            return None

        ext = Path(local_path).suffix.lower().lstrip(".")
        if ext and ext not in media_exts:
            return None

        remote_path = self.plugin._build_remote_path(local_path)
        if not remote_path:
            logger.debug(f"【整理监听】无法映射云端路径，跳过: {local_path}")
            return None

        return TransferRecord(
            source=source,
            local_path=local_path,
            remote_path=remote_path,
            mediainfo=mediainfo,
            meta=meta,
            event_text=event_text,
        )

    @staticmethod
    def _is_excluded(local_path: str, root: str, spec) -> bool:
        roots = [line.strip() for line in str(root or "").splitlines() if line.strip()]
        if not roots:
            roots = [root]
        for item in roots:
            if not item:
                continue
            try:
                rel = str(Path(local_path).relative_to(item)).replace("\\", "/")
            except ValueError:
                continue
            if spec.match_file(rel):
                return True
        try:
            rel = str(Path(local_path)).replace("\\", "/")
        except ValueError:
            rel = local_path
        return spec.match_file(rel)
