"""Sync engine — three sync modes for OpenListSync."""
import fnmatch
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.log import logger

from .client import OpenListClient, OpenListError


# ---------------------------------------------------------------------------
# helper: exclude matching
# ---------------------------------------------------------------------------

def should_exclude(rel_path: str, name: str, rules: List[str]) -> bool:
    """Return True if *name* or *rel_path* matches any fnmatch rule."""
    for rule in rules:
        if fnmatch.fnmatch(name, rule) or fnmatch.fnmatch(rel_path, rule):
            return True
    return False


# ---------------------------------------------------------------------------
# walk_files — recursive listing via OpenList API
# ---------------------------------------------------------------------------

def walk_files(
    client: OpenListClient,
    root: str,
    exclude_rules: List[str],
) -> Dict[str, dict]:
    """Recursively list all **files** under *root*.

    Returns
    -------
    dict
        ``{rel_path: {"name": ..., "size": ..., "is_dir": False}}``

    Directories are not included in the result.  Directories and files
    matching an *exclude_rules* pattern are skipped.
    """
    result: Dict[str, dict] = {}

    # First try recursive listing for efficiency
    entries = client.list_dir_recursive(root)
    if entries:
        for entry in entries:
            name = entry.get("name", "")
            is_dir = entry.get("is_dir", False)
            if is_dir:
                continue
            if should_exclude(name, name, exclude_rules):
                continue
            # Build relative path from root
            # OpenList recursive listing may give full paths or just names
            full_path = entry.get("path", "") or name
            if full_path.startswith(root):
                rel = full_path[len(root):].lstrip("/")
            else:
                rel = name
            result[rel] = {
                "name": name,
                "size": entry.get("size", 0),
                "is_dir": False,
            }
        return result

    # Fallback: manual recursive walk with stack
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = client.list_dir(current)
        except OpenListError as e:
            logger.warning(f"walk_files: 列出目录失败 {current}: {e}")
            continue

        for entry in entries:
            name = entry.get("name", "")
            is_dir = entry.get("is_dir", False)

            # Build relative path
            if current == root:
                rel = name
            else:
                rel = current[len(root):].lstrip("/") + "/" + name

            if should_exclude(rel, name, exclude_rules):
                continue

            if is_dir:
                stack.append(current.rstrip("/") + "/" + name)
            else:
                result[rel] = {
                    "name": name,
                    "size": entry.get("size", 0),
                    "is_dir": False,
                }
    return result


# ---------------------------------------------------------------------------
# helper: ensure parent directories exist recursively
# ---------------------------------------------------------------------------

def _ensure_parent_dirs(client: OpenListClient, dst_dir: str, rel_path: str) -> None:
    """Make sure all parent directories of *rel_path* exist under *dst_dir*."""
    parts = rel_path.strip("/").split("/")
    if len(parts) <= 1:
        return  # file is directly in dst_dir, no subdirs to create

    current = dst_dir.rstrip("/")
    for part in parts[:-1]:
        current = current + "/" + part
        try:
            client.mkdir(current)
        except OpenListError:
            # Directory might already exist — ignore
            pass


# ---------------------------------------------------------------------------
# mode 0 — 仅新增
# ---------------------------------------------------------------------------

def _sync_mode_add_only(
    client: OpenListClient,
    src_dir: str,
    dst_dir: str,
    src_files: Dict[str, dict],
    dst_files: Dict[str, dict],
) -> dict:
    """Copy files that are missing or have different size in destination."""
    copied: List[str] = []
    skipped: List[str] = []
    failed: List[dict] = []

    for rel, src_info in src_files.items():
        dst_info = dst_files.get(rel)
        if dst_info is None or dst_info.get("size") != src_info.get("size"):
            try:
                # Ensure parent directory exists in destination
                _ensure_parent_dirs(client, dst_dir, rel)
                client.copy(src_dir, dst_dir, [rel])
                copied.append(rel)
                logger.info(f"仅新增 copy: {rel}")
            except OpenListError as e:
                failed.append({"path": rel, "error": str(e)})
                logger.error(f"仅新增 copy 失败: {rel} — {e}")
        else:
            skipped.append(rel)

    return {"copied": copied, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# mode 1 — 全同步（镜像）
# ---------------------------------------------------------------------------

def _sync_mode_full_sync(
    client: OpenListClient,
    src_dir: str,
    dst_dir: str,
    src_files: Dict[str, dict],
    dst_files: Dict[str, dict],
) -> dict:
    """Mirror src to dst: copy missing/changed, delete extra files."""
    # Step 1: add-only logic
    add_result = _sync_mode_add_only(client, src_dir, dst_dir, src_files, dst_files)

    # Step 2: delete files in dst that don't exist in src
    deleted: List[str] = []
    delete_failed: List[dict] = []

    extra = [rel for rel in dst_files if rel not in src_files]
    if extra:
        try:
            client.remove(dst_dir, extra)
            deleted = extra
            logger.info(f"全同步 remove {len(extra)} 个多余文件")
        except OpenListError as e:
            # Try one by one
            for rel in extra:
                try:
                    client.remove(dst_dir, [rel])
                    deleted.append(rel)
                except OpenListError as e2:
                    delete_failed.append({"path": rel, "error": str(e2)})
                    logger.error(f"全同步 remove 失败: {rel} — {e2}")

    return {
        "copied": add_result["copied"],
        "skipped": add_result["skipped"],
        "deleted": deleted,
        "failed": add_result["failed"] + delete_failed,
    }


# ---------------------------------------------------------------------------
# mode 2 — 移动
# ---------------------------------------------------------------------------

def _sync_mode_move(
    client: OpenListClient,
    src_dir: str,
    dst_dir: str,
    src_files: Dict[str, dict],
    dst_files: Dict[str, dict],
) -> dict:
    """Copy then delete source files. Handle existing == already moved."""
    copied: List[str] = []
    moved: List[str] = []
    skipped: List[str] = []
    conflicts: List[str] = []
    failed: List[dict] = []

    for rel, src_info in src_files.items():
        dst_info = dst_files.get(rel)
        if dst_info is None:
            # Not in dest → copy → verify → delete source
            try:
                _ensure_parent_dirs(client, dst_dir, rel)
                client.copy(src_dir, dst_dir, [rel])
                # Verify
                verify = client.get(dst_dir.rstrip("/") + "/" + rel)
                if verify and verify.get("size") == src_info.get("size"):
                    client.remove(src_dir, [rel])
                    moved.append(rel)
                    logger.info(f"移动完成: {rel}")
                else:
                    copied.append(rel)
                    logger.warning(f"移动验证失败，保留源: {rel}")
            except OpenListError as e:
                failed.append({"path": rel, "error": str(e)})
                logger.error(f"移动失败: {rel} — {e}")
        elif dst_info.get("size") == src_info.get("size"):
            # Same size → already moved, just delete source
            try:
                client.remove(src_dir, [rel])
                moved.append(rel)
                logger.info(f"已移动（仅删源）: {rel}")
            except OpenListError as e:
                skipped.append(rel)
                logger.warning(f"删除源失败（可能已移动）: {rel} — {e}")
        else:
            # Different size → conflict
            conflicts.append(rel)
            logger.warning(f"冲突（目标存在且大小不同）: {rel}")

    return {
        "copied": copied,
        "moved": moved,
        "skipped": skipped,
        "conflicts": conflicts,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def execute_job(job: dict, client: OpenListClient) -> dict:
    """Execute one sync job and return result dict.

    Parameters
    ----------
    job : dict
        Job definition with keys:
        - src_dir, dst_dir, sync_mode, exclude_rules
    client : OpenListClient

    Returns
    -------
    dict
        Keys: copied, skipped, deleted, moved, conflicts, failed
        Each value is a list of relative paths (or list of dict for failed).
    """
    src_dir = job.get("src_dir", "").rstrip("/")
    dst_dir = job.get("dst_dir", "").rstrip("/")
    sync_mode = job.get("sync_mode", 0)
    exclude_rules = job.get("exclude_rules", [])

    if not src_dir or not dst_dir:
        raise ValueError("src_dir 和 dst_dir 不能为空")

    logger.info(
        f"开始同步 job={job.get('name', job.get('id', ''))}, "
        f"mode={sync_mode}, src={src_dir}, dst={dst_dir}"
    )

    t0 = time.time()

    # 1. Ensure destination directory exists
    try:
        client.mkdir(dst_dir)
    except OpenListError:
        pass  # may already exist

    # 2. List source files
    logger.info(f"walk_files src: {src_dir}")
    src_files = walk_files(client, src_dir, exclude_rules)
    logger.info(f"walk_files src 完成，共 {len(src_files)} 个文件")

    # 3. List destination files
    logger.info(f"walk_files dst: {dst_dir}")
    dst_files = walk_files(client, dst_dir, exclude_rules)
    logger.info(f"walk_files dst 完成，共 {len(dst_files)} 个文件")

    # 4. Execute by mode
    if sync_mode == 0:
        result = _sync_mode_add_only(client, src_dir, dst_dir, src_files, dst_files)
    elif sync_mode == 1:
        result = _sync_mode_full_sync(client, src_dir, dst_dir, src_files, dst_files)
    elif sync_mode == 2:
        result = _sync_mode_move(client, src_dir, dst_dir, src_files, dst_files)
    else:
        raise ValueError(f"不支持的同步模式: {sync_mode}")

    elapsed = time.time() - t0
    logger.info(
        f"同步完成 job={job.get('name', '')}, "
        f"耗时 {elapsed:.1f}s, "
        f"结果: {_summarize(result)}"
    )

    return result


def _summarize(result: dict) -> str:
    """Human-readable summary string."""
    parts = []
    for key in ("copied", "moved", "deleted", "skipped", "conflicts", "failed"):
        val = result.get(key, [])
        if val:
            parts.append(f"{key}={len(val)}")
    return ", ".join(parts)
