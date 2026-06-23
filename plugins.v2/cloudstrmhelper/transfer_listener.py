"""transfer_listener.py — 整理完成事件的过滤与分发

实现要点（参考 p123strmhelper 的 generate_strm 事件处理 + chinesesubfinder）：
- MoviePilot「整理完成」通过进程内 EventType.TransferComplete 事件传递（非 SSE）。
- event.event_data 含 mediainfo/meta/transferinfo/fileitem。
- 过滤链：事件路径前缀过滤 → pathspec 排除 → 扩展名过滤 → 蓝光目录跳过。
- 对 transferinfo.file_list_new 中每个文件计算 local/remote 路径，增量判定后入队 cloud_sync。
"""
import os
from pathlib import Path
from typing import List, Optional

import pathspec
from app.log import logger
from app.utils.system import SystemUtils


class TransferListener:
    """整理完成事件监听/分发器。"""

    def __init__(self, plugin):
        self.plugin = plugin

    def handle(self, event) -> None:
        """处理 TransferComplete 事件。"""
        try:
            data = event.event_data or {}
            transferinfo = data.get("transferinfo")
            if transferinfo is None:
                logger.debug("【整理监听】事件无 transferinfo，跳过")
                return

            # 整理成功才处理
            success = getattr(transferinfo, "success", None)
            if success is False:
                logger.debug("【整理监听】整理未成功，跳过")
                return

            mediainfo = data.get("mediainfo")
            meta = data.get("meta")

            # 目标目录 FileItem（整理后文件所在目录）
            target_diritem = getattr(transferinfo, "target_diritem", None)
            if target_diritem is None:
                target_item = getattr(transferinfo, "target_item", None)
                if target_item is not None:
                    target_diritem = target_item
            if target_diritem is None:
                logger.warning("【整理监听】事件无 target_diritem，无法定位目标目录")
                return

            target_dir = getattr(target_diritem, "path", None) or ""
            if not target_dir:
                logger.warning("【整理监听】目标目录 path 为空，跳过")
                return

            # 蓝光目录跳过
            try:
                if SystemUtils.is_bluray_dir(Path(target_dir)):
                    logger.info(f"【整理监听】蓝光目录，跳过: {target_dir}")
                    return
            except Exception:
                pass

            # 整理后新文件相对路径列表
            file_list = getattr(transferinfo, "file_list_new", None)
            if not file_list:
                file_list = getattr(transferinfo, "file_list", None)
            if not file_list:
                logger.debug(f"【整理监听】无新文件列表，跳过: {target_dir}")
                return

            self._process_files(target_dir, file_list, mediainfo, meta)
        except Exception as e:
            logger.error(f"【整理监听】处理事件异常: {e}", exc_info=True)

    def _process_files(self, target_dir: str, file_list: list,
                       mediainfo, meta) -> None:
        """对每个新文件计算路径、过滤、增量判定、入队。"""
        local_root = self.plugin._local_media_path
        cloud_root = self.plugin._alist_target_path
        if not local_root or not cloud_root:
            logger.warning("【整理监听】未配置本地媒体路径或云端目标路径，跳过")
            return

        exclude_spec = self.plugin._exclude_spec
        event_prefixes = self.plugin._event_filter_prefixes
        media_exts = self.plugin._rmt_mediaext

        queued = 0
        skipped = 0
        for rel in file_list:
            # rel 可能是相对路径字符串
            rel_str = str(rel).replace("\\", "/").lstrip("/")
            local_path = os.path.join(target_dir, rel_str)

            # 1. 事件前缀过滤
            if event_prefixes and not any(local_path.startswith(p) for p in event_prefixes):
                skipped += 1
                continue

            # 2. 排除规则（pathspec，相对 local_root）
            if exclude_spec and self._is_excluded(local_path, local_root, exclude_spec):
                logger.debug(f"【整理监听】命中排除规则，跳过: {local_path}")
                skipped += 1
                continue

            # 3. 扩展名过滤
            ext = Path(local_path).suffix.lower().lstrip(".")
            if ext not in media_exts:
                skipped += 1
                continue

            # 4. 本地文件存在性
            if not os.path.exists(local_path):
                logger.debug(f"【整理监听】本地文件不存在，跳过: {local_path}")
                skipped += 1
                continue

            # 5. 计算云端路径：cloud_root + (local_path 相对 local_root 的部分)
            try:
                rel_to_root = Path(local_path).relative_to(local_root)
            except ValueError:
                # target_dir 可能不在 local_root 下（如整理到其他目录），用 rel 推导
                rel_to_root = Path(rel_str)
            remote_path = (cloud_root.rstrip("/") + "/" + str(rel_to_root).replace("\\", "/")).rstrip("/")

            # 6. 增量判定
            cloud_sync = self.plugin._cloud_sync
            if cloud_sync is None:
                logger.warning("【整理监听】云同步引擎未初始化，跳过")
                return
            try:
                if not cloud_sync.need_upload(local_path, remote_path):
                    logger.debug(f"【整理监听】远端已存在且一致，跳过上传: {remote_path}")
                    # 即使不上传也尝试生成 STRM（可能 STRM 缺失）
                    if self.plugin and hasattr(self.plugin, "_on_file_synced"):
                        self.plugin._on_file_synced(local_path, remote_path, mediainfo, meta)
                    skipped += 1
                    continue
            except Exception as e:
                logger.warning(f"【整理监听】增量判定异常，按需上传: {e}")

            # 7. 入队
            cloud_sync.enqueue_file(local_path, remote_path, mediainfo, meta)
            queued += 1

        logger.info(f"【整理监听】目录 {target_dir}: 入队 {queued}，跳过 {skipped}")

    @staticmethod
    def _is_excluded(local_path: str, root: str, spec) -> bool:
        """pathspec gitignore 匹配，路径相对 root。"""
        try:
            rel = str(Path(local_path).relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = local_path
        return spec.match_file(rel)
