"""OpenList 上传插件 — 监听整理完成事件，通过 OpenList API 上传文件到网盘"""

import hashlib
import json
import os
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .client import OpenListClient
from .queue import UploadQueue, UploadTask, TaskStatus

try:
    import requests as _req
except ImportError:
    _req = None


class OpenListUploader(_PluginBase):
    plugin_name = "OpenList 上传"
    plugin_desc = "监听整理完成事件，通过 OpenList API 将媒体文件上传到网盘。"
    plugin_icon = "cloud-upload.png"
    plugin_version = "0.1.0"
    plugin_author = "101letters"
    author_url = "https://github.com/101letters"
    plugin_config_prefix = "openlistuploader_"
    plugin_order = 66
    auth_level = 1

    # ─── 配置字段 ───────────────────────────────────────────────

    # 基础设置
    _enabled: bool = True
    _notify: bool = True
    _oplist_url: str = ""
    _token: str = ""
    _username: str = ""
    _password: str = ""

    # 上传设置
    _timeout: int = 120
    _skip_existing: bool = True
    _mkdir_before_upload: bool = True

    # 路径映射
    _path_mappings: str = "[]"
    _default_remote_root: str = ""

    # 过滤规则
    _include_exts: str = ".mkv,.mp4,.avi,.mov,.ts,.m2ts,.srt,.ass,.ssa,.nfo,.jpg,.png"
    _exclude_exts: str = ".tmp,.part"
    _exclude_keywords: str = "sample,trailer"
    _min_size_mb: int = 10
    _upload_video: bool = True
    _upload_subtitle: bool = True
    _upload_nfo: bool = False
    _upload_image: bool = False

    # 重试设置
    _max_retries: int = 3
    _retry_interval: int = 300
    _exponential_backoff: bool = True

    # ─── 私有属性 ───────────────────────────────────────────────

    _client: Optional[OpenListClient] = None
    _queue: Optional[UploadQueue] = None
    _mapper: Optional["PathMapper"] = None
    _filters: Optional["UploadFilter"] = None
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if _req is None:
            logger.error("OpenList 上传插件缺少 requests 依赖")
            return

        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._notify = bool(config.get("notify", True))
        self._oplist_url = (config.get("oplist_url") or "").rstrip("/")
        self._token = (config.get("token") or "").strip()
        self._username = config.get("username") or ""
        self._password = config.get("password") or ""
        self._timeout = int(config.get("timeout", 120))
        self._skip_existing = bool(config.get("skip_existing", True))
        self._mkdir_before_upload = bool(config.get("mkdir_before_upload", True))
        self._path_mappings = config.get("path_mappings") or "[]"
        self._default_remote_root = config.get("default_remote_root") or ""
        self._include_exts = config.get("include_exts") or ".mkv,.mp4,.avi,.mov,.ts,.m2ts,.srt,.ass,.ssa,.nfo,.jpg,.png"
        self._exclude_exts = config.get("exclude_exts") or ".tmp,.part"
        self._exclude_keywords = config.get("exclude_keywords") or "sample,trailer"
        self._min_size_mb = int(config.get("min_size_mb", 10))
        self._upload_video = bool(config.get("upload_video", True))
        self._upload_subtitle = bool(config.get("upload_subtitle", True))
        self._upload_nfo = bool(config.get("upload_nfo", False))
        self._upload_image = bool(config.get("upload_image", False))
        self._max_retries = int(config.get("max_retries", 3))
        self._retry_interval = int(config.get("retry_interval", 300))
        self._exponential_backoff = bool(config.get("exponential_backoff", True))

        # 重新初始化组件
        self._init_components()

        # 停止旧的 worker 后重新启动
        if self._enabled and self._queue:
            self._queue.start()

    def _init_components(self):
        """初始化各组件"""
        if not self._oplist_url:
            logger.warning("OpenList 地址未配置")
            return

        # OpenList 客户端
        self._client = OpenListClient(
            base_url=self._oplist_url,
            username=self._username,
            password=self._password,
            token=self._token,
            timeout=self._timeout,
        )

        # 路径映射器
        try:
            mappings = json.loads(self._path_mappings) if isinstance(self._path_mappings, str) else self._path_mappings
        except (json.JSONDecodeError, TypeError):
            mappings = []
        self._mapper = PathMapper(mappings, self._default_remote_root)

        # 文件过滤器
        self._filters = UploadFilter(
            include_exts=self._parse_ext_list(self._include_exts),
            exclude_exts=self._parse_ext_list(self._exclude_exts),
            exclude_keywords=self._parse_keyword_list(self._exclude_keywords),
            min_size_bytes=self._min_size_mb * 1024 * 1024,
            upload_video=self._upload_video,
            upload_subtitle=self._upload_subtitle,
            upload_nfo=self._upload_nfo,
            upload_image=self._upload_image,
        )

        # 上传队列（如果已存在则复用，避免重启丢失状态）
        if self._queue is None:
            self._queue = UploadQueue(
                client=self._client,
                mapper=self._mapper,
                filters=self._filters,
                max_retries=self._max_retries,
                retry_interval=self._retry_interval,
                exponential_backoff=self._exponential_backoff,
                skip_existing=self._skip_existing,
                notify_callback=self._send_notification,
            )

    # ─── 事件监听 ───────────────────────────────────────────────

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """监听整理完成事件"""
        if not self._enabled or not self._client or not self._queue:
            return

        item = event.event_data
        if not item:
            return

        try:
            self._handle_transfer(item)
        except Exception as e:
            logger.error(f"处理 TransferComplete 事件异常: {e}\n{traceback.format_exc()}")

    def _handle_transfer(self, item: dict):
        """处理转移事件，生成上传任务"""
        # 获取转移信息
        transfer_info = item.get("transferinfo")
        media_info = item.get("mediainfo")

        if not transfer_info:
            logger.warning("事件中无 transferinfo 数据")
            return

        # 获取文件列表
        file_list = transfer_info.get("file_list_new") or []
        target_dir = transfer_info.get("target_diritem") or {}

        if not file_list and target_dir:
            # 可能只有目录没有文件列表，尝试获取目录路径
            dir_path = target_dir.get("path", "")
            if dir_path and os.path.isdir(dir_path):
                file_list = self._scan_directory(dir_path)

        if not file_list:
            logger.warning("事件中无文件列表")
            return

        # 媒体类型
        media_type = ""
        media_title = ""
        if media_info:
            media_type = media_info.get("type", "")
            media_title = media_info.get("title", "")

        # 为每个文件创建上传任务
        task_count = 0
        for file_path in file_list:
            if isinstance(file_path, dict):
                file_path = file_path.get("path") or file_path.get("name", "")
            if not file_path or not os.path.isfile(file_path):
                continue

            local_path = Path(file_path)

            # 应用过滤器
            if self._filters and not self._filters.should_upload(local_path):
                logger.debug(f"跳过（被过滤）: {local_path.name}")
                continue

            # 路径映射
            if not self._mapper:
                logger.warning("路径映射器未初始化")
                continue

            remote_path = self._mapper.map_path(str(local_path))
            if not remote_path:
                logger.debug(f"跳过（路径未匹配）: {local_path}")
                continue

            # 生成任务 ID
            file_stat = local_path.stat()
            raw_id = f"{file_path}:{remote_path}:{file_stat.st_size}"
            task_id = hashlib.sha256(raw_id.encode()).hexdigest()[:32]

            task = UploadTask(
                task_id=task_id,
                local_path=str(local_path),
                remote_path=remote_path,
                size=file_stat.st_size,
                max_retries=self._max_retries,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
                source_event=json.dumps({
                    "media_type": media_type,
                    "media_title": media_title,
                }, ensure_ascii=False),
            )

            if self._queue.enqueue(task):
                task_count += 1

        if task_count > 0:
            logger.info(f"本次共入队 {task_count} 个上传任务")

    # ─── 插件生命周期 ───────────────────────────────────────────

    def stop_service(self):
        """停止插件服务"""
        if self._queue:
            self._queue.stop()

    def get_state(self) -> bool:
        return self._enabled

    # ─── 配置页 ─────────────────────────────────────────────────

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test_connection",
                "endpoint": self.api_test_connection,
                "methods": ["POST"],
                "summary": "测试 OpenList 连接",
                "description": "测试与 OpenList 服务的连接状态",
            },
            {
                "path": "/queue_stats",
                "endpoint": self.api_queue_stats,
                "methods": ["GET"],
                "summary": "获取队列统计",
                "description": "获取上传队列的统计信息",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    # ── 基础设置 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "监听 MP 整理完成事件，通过 OpenList 上传 API 将文件上传到 123 云盘等目标网盘。"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "启用通知",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "oplist_url",
                                            "label": "OpenList 地址",
                                            "placeholder": "http://192.168.1.100:5244",
                                            "hint": "OpenList 服务地址，同一局域网推荐用内网 IP",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "token",
                                            "label": "Token（优先）",
                                            "type": "password",
                                            "hint": "OpenList 管理后台获取的 token，有 token 时优先使用",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username",
                                            "label": "用户名（备选）",
                                            "hint": "无 token 时使用用户名密码自动登录",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "type": "password",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ── 路径映射 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "secondary",
                                            "variant": "tonal",
                                            "text": "路径映射：JSON 数组，每条规则包含 name（名称）、local（本地前缀）、remote（远端前缀）。按顺序匹配，取最长匹配前缀。"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射规则（JSON）",
                                            "rows": 4,
                                            "placeholder": '[\n  {"name": "电影", "local": "/media/Movies", "remote": "/123云盘/影视/电影"},\n  {"name": "剧集", "local": "/media/TV", "remote": "/123云盘/影视/剧集"}\n]',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "default_remote_root",
                                            "label": "未匹配时的默认远端根路径",
                                            "placeholder": "/123云盘/影视/其他",
                                            "hint": "路径未匹配任何规则时，以相对路径拼接到此目录下",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ── 上传设置 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "secondary",
                                            "variant": "tonal",
                                            "text": "上传设置：控制上传行为与 OpenList API 调用参数。"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "skip_existing",
                                            "label": "远端已存在则跳过",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "mkdir_before_upload",
                                            "label": "上传前创建目录",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "请求超时（秒）",
                                            "type": "number",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ── 过滤规则 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "secondary",
                                            "variant": "tonal",
                                            "text": "过滤规则：控制哪些文件会上传到网盘。"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "include_exts",
                                            "label": "包含后缀",
                                            "hint": "逗号分隔，留空表示全部",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_exts",
                                            "label": "排除后缀",
                                            "hint": "逗号分隔",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键词",
                                            "hint": "文件名含这些关键词的不上传",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "upload_video",
                                            "label": "上传视频",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "upload_subtitle",
                                            "label": "上传字幕",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "upload_nfo",
                                            "label": "上传 NFO",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "upload_image",
                                            "label": "上传图片",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_size_mb",
                                            "label": "最小文件大小（MB）",
                                            "type": "number",
                                            "hint": "小于此大小的文件不上传",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ── 重试设置 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "secondary",
                                            "variant": "tonal",
                                            "text": "重试设置：上传失败时的自动重试策略。"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_retries",
                                            "label": "最大重试次数",
                                            "type": "number",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_interval",
                                            "label": "重试间隔（秒）",
                                            "type": "number",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "exponential_backoff",
                                            "label": "指数退避",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": True,
            "notify": True,
            "oplist_url": "",
            "token": "",
            "username": "",
            "password": "",
            "timeout": 120,
            "skip_existing": True,
            "mkdir_before_upload": True,
            "path_mappings": json.dumps([
                {"name": "电影", "local": "/media/Movies", "remote": "/123云盘/影视/电影"},
                {"name": "剧集", "local": "/media/TV", "remote": "/123云盘/影视/剧集"},
            ], ensure_ascii=False, indent=2),
            "default_remote_root": "/123云盘/影视/其他",
            "include_exts": ".mkv,.mp4,.avi,.mov,.ts,.m2ts,.srt,.ass,.ssa,.nfo,.jpg,.png",
            "exclude_exts": ".tmp,.part",
            "exclude_keywords": "sample,trailer",
            "min_size_mb": 10,
            "upload_video": True,
            "upload_subtitle": True,
            "upload_nfo": False,
            "upload_image": False,
            "max_retries": 3,
            "retry_interval": 300,
            "exponential_backoff": True,
        }

    def get_page(self) -> List[dict]:
        """状态页面"""
        if not self._queue:
            return [{"component": "VAlert", "props": {"type": "info", "text": "队列未初始化"}}]

        stats = self._queue.stats()
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [{
                            "component": "VCard",
                            "props": {"class": "pa-3 text-center"},
                            "content": [{
                                "component": "div",
                                "content": [
                                    {"component": "div", "props": {"class": "text-h4 font-weight-bold primary--text"}, "content": str(stats["pending"])},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "content": "待上传"},
                                ]
                            }]
                        }]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [{
                            "component": "VCard",
                            "props": {"class": "pa-3 text-center"},
                            "content": [{
                                "component": "div",
                                "content": [
                                    {"component": "div", "props": {"class": "text-h4 font-weight-bold success--text"}, "content": str(stats["success"])},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "content": "上传成功"},
                                ]
                            }]
                        }]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [{
                            "component": "VCard",
                            "props": {"class": "pa-3 text-center"},
                            "content": [{
                                "component": "div",
                                "content": [
                                    {"component": "div", "props": {"class": "text-h4 font-weight-bold warning--text"}, "content": str(stats["failed"])},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "content": "上传失败"},
                                ]
                            }]
                        }]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [{
                            "component": "VCard",
                            "props": {"class": "pa-3 text-center"},
                            "content": [{
                                "component": "div",
                                "content": [
                                    {"component": "div", "props": {"class": "text-h4 font-weight-bold info--text"}, "content": str(stats["skipped"])},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "content": "已跳过"},
                                ]
                            }]
                        }]
                    },
                ]
            },
            {
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal", "text": f"队列中共 {stats['total']} 个任务"}
                    }]
                }]
            }
        ]

    # ─── API ────────────────────────────────────────────────────

    def api_test_connection(self, **kwargs) -> dict:
        """测试 OpenList 连接"""
        if not self._client:
            return {"success": False, "message": "客户端未初始化"}

        try:
            result = self._client.test_connection()
            return {"success": result.startswith("连接成功"), "message": result}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def api_queue_stats(self, **kwargs) -> dict:
        """获取队列统计"""
        if not self._queue:
            return {"success": False, "message": "队列未初始化"}
        return {"success": True, "data": self._queue.stats()}

    # ─── 辅助方法 ───────────────────────────────────────────────

    def _send_notification(self, title: str, message: str, type_: str = "info"):
        """发送 MP 通知"""
        try:
            self.post_message(
                title=title,
                text=message,
                mtype=NotificationType.__members__.get(type_.upper(), NotificationType.Info),
            )
        except Exception:
            pass

    def _scan_directory(self, dir_path: str) -> List[str]:
        """递归扫描目录下的文件"""
        files = []
        try:
            for root, _, filenames in os.walk(dir_path):
                for fn in filenames:
                    files.append(os.path.join(root, fn))
        except Exception as e:
            logger.error(f"扫描目录失败 {dir_path}: {e}")
        return files

    @staticmethod
    def _parse_ext_list(ext_str: str) -> list:
        """解析后缀列表字符串"""
        if not ext_str:
            return []
        return [e.strip().lower() for e in ext_str.split(",") if e.strip()]

    @staticmethod
    def _parse_keyword_list(kw_str: str) -> list:
        """解析关键词列表字符串"""
        if not kw_str:
            return []
        return [k.strip().lower() for k in kw_str.split(",") if k.strip()]


# ─── 路径映射器 ────────────────────────────────────────────────

class PathMapper:
    """本地路径 → 远端路径映射

    规则示例：
      {"local": "/media/Movies", "remote": "/123云盘/影视/电影"}
      {"local": "/media/TV",     "remote": "/123云盘/影视/剧集"}
    """

    def __init__(self, mappings: List[dict], default_remote: str = ""):
        # 按 local 路径长度降序排列（最长前缀优先匹配）
        self._mappings = sorted(
            mappings,
            key=lambda m: len(m.get("local", "")),
            reverse=True,
        ) if mappings else []
        self._default_remote = default_remote.rstrip("/")

    def map_path(self, local_path: str) -> str:
        """将本地路径映射为远端路径"""
        local_norm = local_path.replace("\\", "/")

        for mapping in self._mappings:
            local_prefix = mapping.get("local", "").rstrip("/")
            remote_prefix = mapping.get("remote", "").rstrip("/")

            if local_norm.startswith(local_prefix + "/") or local_norm == local_prefix:
                relative = local_norm[len(local_prefix):].lstrip("/")
                return f"{remote_prefix}/{relative}" if relative else remote_prefix

        # 未匹配，使用默认根路径
        if self._default_remote:
            # 取 MP 媒体库相对路径
            for mp_root in ["/media", "/data", "/downloads"]:
                if local_norm.startswith(mp_root + "/"):
                    relative = local_norm[len(mp_root):].lstrip("/")
                    return f"{self._default_remote}/{relative}"
            return f"{self._default_remote}/{Path(local_norm).name}"

        return ""


# ─── 文件过滤器 ─────────────────────────────────────────────────

class UploadFilter:
    """文件上传过滤"""

    VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts", ".iso"}
    SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sup"}
    NFO_EXTS = {".nfo"}
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tbn"}

    def __init__(
        self,
        include_exts: list = None,
        exclude_exts: list = None,
        exclude_keywords: list = None,
        min_size_bytes: int = 0,
        upload_video: bool = True,
        upload_subtitle: bool = True,
        upload_nfo: bool = False,
        upload_image: bool = False,
    ):
        self._include_exts = set(e.lower() for e in (include_exts or []))
        self._exclude_exts = set(e.lower() for e in (exclude_exts or []))
        self._exclude_keywords = exclude_keywords or []
        self._min_size = min_size_bytes
        self._upload_video = upload_video
        self._upload_subtitle = upload_subtitle
        self._upload_nfo = upload_nfo
        self._upload_image = upload_image

    def should_upload(self, file_path: Path) -> bool:
        """判断文件是否应该上传"""
        # 大小检查
        if self._min_size > 0 and file_path.stat().st_size < self._min_size:
            return False

        ext = file_path.suffix.lower()

        # 排除后缀
        if ext in self._exclude_exts:
            return False

        # 包含后缀（如果配置了）
        if self._include_exts and ext not in self._include_exts:
            return False

        # 按类型开关过滤
        if ext in self.VIDEO_EXTS and not self._upload_video:
            return False
        if ext in self.SUBTITLE_EXTS and not self._upload_subtitle:
            return False
        if ext in self.NFO_EXTS and not self._upload_nfo:
            return False
        if ext in self.IMAGE_EXTS and not self._upload_image:
            return False

        # 排除关键词
        name_lower = file_path.name.lower()
        for kw in self._exclude_keywords:
            if kw in name_lower:
                return False

        return True
