"""同步执行引擎：三种同步模式的核心逻辑。"""
import os
import fnmatch
from typing import Optional


def join_path(*parts):
    """拼接 OpenList 路径，统一用 /"""
    cleaned = []
    for part in parts:
        if not part:
            continue
        cleaned.append(str(part).strip("/"))
    if not cleaned:
        return "/"
    return "/" + "/".join(cleaned)


def join_rel_path(parent: str, name: str) -> str:
    """拼接相对路径"""
    if not parent:
        return name.strip("/")
    return f"{parent.strip('/')}/{name.strip('/')}"


def split_rel_path(rel_path: str):
    """拆分相对路径为 (父目录, 文件名)"""
    rel_path = rel_path.strip("/")
    if "/" not in rel_path:
        return "", rel_path
    parent, name = rel_path.rsplit("/", 1)
    return parent, name


class SyncEngine:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def rel_path_from_src(src_dir: str, abs_path: str) -> str:
        """计算事件路径相对于 src_dir 的相对路径"""
        src = src_dir.strip("/")
        ap = abs_path.strip("/")
        if ap == src:
            return ""
        prefix = src + "/"
        if ap.startswith(prefix):
            return ap[len(prefix):]
        raise ValueError(f"event_path '{abs_path}' 不在 src_dir '{src_dir}' 下")

    @staticmethod
    def normalize_path(path: str) -> str:
        path = str(path or "").strip()
        if not path:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    @staticmethod
    def join_rel_path(parent: str, name: str) -> str:
        return join_rel_path(parent, name)

    def should_exclude(self, rel_path: str, name: str, rules: list) -> bool:
        """用 fnmatch 匹配排除规则"""
        rel_path = rel_path.strip("/")
        name = name.strip("/")
        for rule in rules or []:
            pattern = str(rule).strip()
            if not pattern:
                continue
            if fnmatch.fnmatch(name, pattern):
                return True
            if fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def walk_files(self, root_dir: str, exclude_rules: list) -> dict:
        """递归扫描目录，返回 相对路径->文件信息 的字典"""
        result = {}
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            current_dir = join_path(root_dir, rel_dir)
            items = self.client.list_dir(current_dir)
            for item in items or []:
                name = item.get("name", "")
                if not name:
                    continue
                is_dir = bool(item.get("is_dir", False))
                size = int(item.get("size") or 0)
                rel_path = join_rel_path(rel_dir, name)
                abs_path = join_path(root_dir, rel_path)
                if self.should_exclude(rel_path, name, exclude_rules):
                    continue
                if is_dir:
                    stack.append(rel_path)
                else:
                    result[rel_path] = {
                        "name": name,
                        "rel_path": rel_path,
                        "abs_path": abs_path,
                        "size": size,
                        "is_dir": False,
                    }
        return result

    def copy_one(self, src_root: str, dst_root: str, rel_path: str):
        """复制单个文件"""
        parent_rel, filename = split_rel_path(rel_path)
        src_parent = join_path(src_root, parent_rel)
        dst_parent = join_path(dst_root, parent_rel)
        self.client.mkdir(dst_parent)
        self.client.copy(src_parent, dst_parent, [filename])

    def remove_one(self, root: str, rel_path: str):
        """删除单个文件"""
        parent_rel, filename = split_rel_path(rel_path)
        parent = join_path(root, parent_rel)
        self.client.remove(parent, [filename])

    def get_one(self, root: str, rel_path: str) -> Optional[dict]:
        """获取单个文件信息"""
        path = join_path(root, rel_path)
        return self.client.get(path)

    def execute(self, job: dict) -> dict:
        """公共执行入口"""
        src_dir = job["src_dir"]
        dst_dir = job["dst_dir"]
        mode = int(job["sync_mode"])
        exclude_rules = job.get("exclude_rules") or []
        self.client.mkdir(dst_dir)
        src_files = self.walk_files(src_dir, exclude_rules)
        dst_files = self.walk_files(dst_dir, exclude_rules)
        if mode == 0:
            result = self.sync_incremental(job, src_files, dst_files)
        elif mode == 1:
            result = self.sync_mirror(job, src_files, dst_files)
        elif mode == 2:
            result = self.sync_move(job, src_files, dst_files)
        else:
            raise ValueError(f"unsupported sync mode: {mode}")
        result["summary"] = {
            "scanned_src": len(src_files),
            "scanned_dst": len(dst_files),
            "copied": len(result.get("copied", [])),
            "deleted": len(result.get("deleted", [])),
            "moved": len(result.get("moved", [])),
            "skipped": len(result.get("skipped", [])),
            "conflicts": len(result.get("conflicts", [])),
            "failed": len(result.get("failed", [])),
        }
        return result

    def sync_incremental(self, job, src_files, dst_files) -> dict:
        """模式0：仅新增"""
        copied = []
        skipped = []
        conflicts = []
        failed = []
        src_dir = job["src_dir"]
        dst_dir = job["dst_dir"]
        for rel_path in sorted(src_files.keys()):
            src_file = src_files[rel_path]
            dst_file = dst_files.get(rel_path)
            if dst_file is None:
                try:
                    self.copy_one(src_dir, dst_dir, rel_path)
                    copied.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "copy", "error": str(e)})
            else:
                if dst_file["size"] == src_file["size"]:
                    skipped.append(rel_path)
                else:
                    conflicts.append({
                        "path": rel_path,
                        "src_size": src_file["size"],
                        "dst_size": dst_file["size"],
                    })
        return {
            "mode": 0,
            "copied": copied,
            "deleted": [],
            "moved": [],
            "skipped": skipped,
            "conflicts": conflicts,
            "failed": failed,
        }

    def sync_mirror(self, job, src_files, dst_files) -> dict:
        """模式1：全同步"""
        copied = []
        deleted = []
        skipped = []
        conflicts = []
        failed = []
        src_dir = job["src_dir"]
        dst_dir = job["dst_dir"]
        for rel_path in sorted(src_files.keys()):
            src_file = src_files[rel_path]
            dst_file = dst_files.get(rel_path)
            if dst_file is None:
                try:
                    self.copy_one(src_dir, dst_dir, rel_path)
                    copied.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "copy", "error": str(e)})
            else:
                if dst_file["size"] == src_file["size"]:
                    skipped.append(rel_path)
                else:
                    conflicts.append({
                        "path": rel_path,
                        "src_size": src_file["size"],
                        "dst_size": dst_file["size"],
                    })
        for rel_path in sorted(dst_files.keys()):
            if rel_path not in src_files:
                try:
                    self.remove_one(dst_dir, rel_path)
                    deleted.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "delete", "error": str(e)})
        return {
            "mode": 1,
            "copied": copied,
            "deleted": deleted,
            "moved": [],
            "skipped": skipped,
            "conflicts": conflicts,
            "failed": failed,
        }

    def sync_move(self, job, src_files, dst_files) -> dict:
        """模式2：移动"""
        copied = []
        moved = []
        skipped = []
        conflicts = []
        failed = []
        src_dir = job["src_dir"]
        dst_dir = job["dst_dir"]
        for rel_path in sorted(src_files.keys()):
            src_file = src_files[rel_path]
            dst_file = dst_files.get(rel_path)
            if dst_file is None:
                try:
                    self.copy_one(src_dir, dst_dir, rel_path)
                    copied.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "copy", "error": str(e)})
                    continue
                try:
                    verified = self.get_one(dst_dir, rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "verify", "error": str(e)})
                    continue
                if not verified or int(verified.get("size") or 0) != src_file["size"]:
                    failed.append({"path": rel_path, "operation": "verify", "error": "copy verification failed"})
                    continue
                try:
                    self.remove_one(src_dir, rel_path)
                    moved.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "remove_source", "error": str(e)})
                continue
            if dst_file["size"] == src_file["size"]:
                try:
                    self.remove_one(src_dir, rel_path)
                    skipped.append(rel_path)
                    moved.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "remove_source", "error": str(e)})
            else:
                conflicts.append({
                    "path": rel_path,
                    "src_size": src_file["size"],
                    "dst_size": dst_file["size"],
                })
        return {
            "mode": 2,
            "copied": copied,
            "deleted": [],
            "moved": moved,
            "skipped": skipped,
            "conflicts": conflicts,
            "failed": failed,
        }

    def sync_event(self, job: dict, event_path: str, file_list_new: list = None) -> dict:
        """事件驱动的单路径同步"""
        src_dir = job["src_dir"]
        mode = int(job["sync_mode"])
        exclude_rules = job.get("exclude_rules") or []
        file_list_new = file_list_new or []

        rel_base = self.rel_path_from_src(src_dir, event_path)

        if file_list_new:
            rel_paths = [
                self.join_rel_path(rel_base, fn)
                for fn in file_list_new if fn
            ]
        elif rel_base:
            rel_paths = [rel_base]
        else:
            raise ValueError("事件路径为作业根目录且无 file_list_new，跳过以避免全量同步")

        rel_paths = [
            p for p in rel_paths
            if not self.should_exclude(p, os.path.basename(p), exclude_rules)
        ]

        if not rel_paths:
            return {
                "mode": mode,
                "event_path": event_path,
                "copied": [], "deleted": [], "moved": [], "skipped": [], "conflicts": [], "failed": [],
                "summary": {"scanned_src": 0, "scanned_dst": 0, "copied": 0, "deleted": 0, "moved": 0, "skipped": 0, "conflicts": 0, "failed": 0}
            }

        if mode == 0:
            result = self._sync_event_incremental(job, rel_paths)
        elif mode == 1:
            result = self._sync_event_mirror(job, rel_base, rel_paths, bool(file_list_new))
        elif mode == 2:
            result = self._sync_event_move(job, rel_paths)
        else:
            raise ValueError(f"不支持的同步模式: {mode}")

        result["mode"] = mode
        result["event_path"] = event_path
        for k in ("copied", "deleted", "moved", "skipped", "conflicts", "failed"):
            items = result.get(k, [])
            result.setdefault("summary", {})[k] = len(items) if isinstance(items, list) else (1 if items else 0)
        summary = result.setdefault("summary", {})
        summary.setdefault("scanned_src", summary.get("copied", 0) + summary.get("skipped", 0) + summary.get("conflicts", 0) + summary.get("failed", 0))
        summary.setdefault("scanned_dst", summary.get("deleted", 0))
        return result

    def _expand_rel_paths_to_files(self, job: dict, rel_paths: list) -> list:
        """将 rel_paths 中的目录展开为文件列表"""
        result = []
        for rel_path in rel_paths:
            info = self.client.get(join_path(job["src_dir"], rel_path))
            if info and info.get("is_dir"):
                files = self.walk_files(join_path(job["src_dir"], rel_path), job.get("exclude_rules") or [])
                for local_rel in files:
                    result.append(join_rel_path(rel_path, local_rel))
            else:
                result.append(rel_path)
        return result

    def _sync_event_incremental(self, job: dict, rel_paths: list) -> dict:
        copied, skipped, conflicts, failed = [], [], [], []
        expanded = self._expand_rel_paths_to_files(job, rel_paths)

        for rel_path in expanded:
            try:
                src_file = self.client.get(join_path(job["src_dir"], rel_path))
                if not src_file:
                    failed.append({"path": rel_path, "error": "源文件不存在"})
                    continue

                dst_file = self.client.get(join_path(job["dst_dir"], rel_path))
                if not dst_file:
                    self.copy_one(job["src_dir"], job["dst_dir"], rel_path)
                    copied.append(rel_path)
                elif int(src_file.get("size") or 0) == int(dst_file.get("size") or 0):
                    skipped.append(rel_path)
                else:
                    conflicts.append({"path": rel_path, "src_size": src_file.get("size"), "dst_size": dst_file.get("size")})
            except Exception as e:
                failed.append({"path": rel_path, "error": str(e)})

        return {"copied": copied, "deleted": [], "moved": [], "skipped": skipped, "conflicts": conflicts, "failed": failed}

    def _sync_event_mirror(self, job: dict, rel_base: str, rel_paths: list, from_file_list: bool) -> dict:
        if from_file_list:
            return self._sync_event_incremental(job, rel_paths)

        src_scope = join_path(job["src_dir"], rel_base)
        dst_scope = join_path(job["dst_dir"], rel_base)

        src_files = self.walk_files(src_scope, job.get("exclude_rules") or [])
        dst_files = self.walk_files(dst_scope, job.get("exclude_rules") or [])

        copied, deleted, skipped, conflicts, failed = [], [], [], [], []

        for local_rel, src_info in src_files.items():
            full_rel = join_rel_path(rel_base, local_rel)
            if local_rel not in dst_files:
                try:
                    self.copy_one(job["src_dir"], job["dst_dir"], full_rel)
                    copied.append(full_rel)
                except Exception as e:
                    failed.append({"path": full_rel, "error": str(e)})
            elif int(src_info.get("size") or 0) == int(dst_files[local_rel].get("size") or 0):
                skipped.append(full_rel)
            else:
                conflicts.append({"path": full_rel, "src_size": src_info.get("size"), "dst_size": dst_files[local_rel].get("size")})

        for local_rel in dst_files:
            if local_rel not in src_files:
                full_rel = join_rel_path(rel_base, local_rel)
                try:
                    self.remove_one(job["dst_dir"], full_rel)
                    deleted.append(full_rel)
                except Exception as e:
                    failed.append({"path": full_rel, "error": str(e)})

        return {"copied": copied, "deleted": deleted, "moved": [], "skipped": skipped, "conflicts": conflicts, "failed": failed}

    def _sync_event_move(self, job: dict, rel_paths: list) -> dict:
        copied, moved, skipped, conflicts, failed = [], [], [], [], []
        expanded = self._expand_rel_paths_to_files(job, rel_paths)

        for rel_path in expanded:
            try:
                src_file = self.client.get(join_path(job["src_dir"], rel_path))
                if not src_file:
                    failed.append({"path": rel_path, "error": "源文件不存在"})
                    continue

                dst_file = self.client.get(join_path(job["dst_dir"], rel_path))
                if not dst_file:
                    self.copy_one(job["src_dir"], job["dst_dir"], rel_path)
                    copied.append(rel_path)
                    verified = self.client.get(join_path(job["dst_dir"], rel_path))
                    if verified and int(verified.get("size") or 0) == int(src_file.get("size") or 0):
                        self.remove_one(job["src_dir"], rel_path)
                        moved.append(rel_path)
                    else:
                        failed.append({"path": rel_path, "error": "复制后校验失败"})
                elif int(src_file.get("size") or 0) == int(dst_file.get("size") or 0):
                    self.remove_one(job["src_dir"], rel_path)
                    moved.append(rel_path)
                    skipped.append(rel_path)
                else:
                    conflicts.append({"path": rel_path, "src_size": src_file.get("size"), "dst_size": dst_file.get("size")})
            except Exception as e:
                failed.append({"path": rel_path, "error": str(e)})

        return {"copied": copied, "deleted": [], "moved": moved, "skipped": skipped, "conflicts": conflicts, "failed": failed}
