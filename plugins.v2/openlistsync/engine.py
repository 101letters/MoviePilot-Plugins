"""同步执行引擎：三种同步模式的核心逻辑。"""
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
                # 阶段1：copy
                try:
                    self.copy_one(src_dir, dst_dir, rel_path)
                    copied.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "copy", "error": str(e)})
                    continue

                # 阶段2：verify
                try:
                    verified = self.get_one(dst_dir, rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "verify", "error": str(e)})
                    continue

                if not verified or int(verified.get("size") or 0) != src_file["size"]:
                    failed.append({
                        "path": rel_path,
                        "operation": "verify",
                        "error": "copy verification failed",
                    })
                    continue

                # 阶段3：remove source
                try:
                    self.remove_one(src_dir, rel_path)
                    moved.append(rel_path)
                except Exception as e:
                    failed.append({"path": rel_path, "operation": "remove_source", "error": str(e)})

                continue

            # 目标已存在
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
