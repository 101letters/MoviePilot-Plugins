import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

try:
    import requests
except ImportError:
    requests = None


class OplistTransfer(_PluginBase):
    plugin_name = "OpenList 文件转运"
    plugin_desc = "监听整理完成事件，自动调用 OpenList API 执行文件复制并发送通知。"
    plugin_icon = "refresh2.png"
    plugin_version = "1.0.0"
    plugin_author = "default"
    author_url = "https://github.com/default"
    plugin_config_prefix = "oplisttransfer_"
    plugin_order = 66
    auth_level = 1

    _enabled = False
    _notify = True
    _oplist_url = ""
    _token = ""
    _username = ""
    _password = ""
    _copy_api_path = "/api/fs/copy"
    _mkdir_api_path = "/api/fs/mkdir"
    _storage_mount_path = "/"
    _movie_dest_root = "/movies"
    _tv_dest_root = "/tv"
    _path_mappings = ""
    _overwrite = False
    _create_dest_dir = True
    _delay = 0
    _timeout = 60
    _onlyonce = False
    _enabled_types = ["movie", "tv"]
    _last_result = {}

    def init_plugin(self, config: dict = None):
        if requests is None:
            logger.error("OpenList 文件转运插件缺少 requests 依赖")
            return

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._oplist_url = (config.get("oplist_url") or "").rstrip("/")
        self._token = config.get("token") or ""
        self._username = config.get("username") or ""
        self._password = config.get("password") or ""
        self._copy_api_path = config.get("copy_api_path") or "/api/fs/copy"
        self._mkdir_api_path = config.get("mkdir_api_path") or "/api/fs/mkdir"
        self._storage_mount_path = config.get("storage_mount_path") or "/"
        self._movie_dest_root = config.get("movie_dest_root") or "/movies"
        self._tv_dest_root = config.get("tv_dest_root") or "/tv"
        self._path_mappings = config.get("path_mappings") or ""
        self._overwrite = bool(config.get("overwrite", False))
        self._create_dest_dir = bool(config.get("create_dest_dir", True))
        self._delay = int(config.get("delay") or 0)
        self._timeout = int(config.get("timeout") or 60)
        self._onlyonce = bool(config.get("onlyonce", False))
        enabled_types = config.get("enabled_types") or ["movie", "tv"]
        self._enabled_types = [str(x).lower() for x in enabled_types if x]

        if self._onlyonce:
            self._onlyonce = False
            self.__save_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "开启通知"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "create_dest_dir", "label": "自动创建目标目录"}
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件监听 MoviePilot 的 TransferComplete 内部事件。该事件不会出现在公开 REST API 文档里，因为它是宿主内部插件事件，不是对外 HTTP 接口。"
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
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "oplist_url", "label": "OpenList 地址", "placeholder": "https://fox.example.com"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "timeout", "label": "请求超时(秒)", "placeholder": "60", "type": "number"}
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
                                        "props": {"model": "username", "label": "OpenList 用户名", "placeholder": "可选"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "password", "label": "OpenList 密码", "placeholder": "可选", "type": "password"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "token", "label": "OpenList Token", "placeholder": "优先使用 Token", "type": "password"}
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
                                        "props": {"model": "copy_api_path", "label": "复制 API 路径", "placeholder": "/api/fs/copy"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "mkdir_api_path", "label": "建目录 API 路径", "placeholder": "/api/fs/mkdir"}
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
                                        "props": {"model": "storage_mount_path", "label": "OpenList 源挂载根路径", "placeholder": "/"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "movie_dest_root", "label": "电影目标根目录", "placeholder": "/movies"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "tv_dest_root", "label": "剧集目标根目录", "placeholder": "/tv"}
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
                                        "props": {"model": "overwrite", "label": "允许覆盖(若 API 支持)"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "delay", "label": "整理后延迟(秒)", "placeholder": "0", "type": "number"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "enabled_types",
                                            "label": "处理媒体类型",
                                            "multiple": True,
                                            "chips": True,
                                            "items": [
                                                {"title": "电影", "value": "movie"},
                                                {"title": "剧集", "value": "tv"}
                                            ]
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
                                            "label": "路径映射",
                                            "rows": 6,
                                            "placeholder": "/mnt/media=/\n/mnt/media/电影=/movies\n/mnt/media/剧集=/tv\n每行一条：本地前缀=OpenList 源前缀"
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "默认按 copy 模式工作。由于 OpenList 各版本 API 字段可能略有差异，如你的接口不是 /api/fs/copy 或 /api/fs/mkdir，可直接在配置里覆盖路径。"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "oplist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "copy_api_path": "/api/fs/copy",
            "mkdir_api_path": "/api/fs/mkdir",
            "storage_mount_path": "/",
            "movie_dest_root": "/movies",
            "tv_dest_root": "/tv",
            "path_mappings": "",
            "overwrite": False,
            "create_dest_dir": True,
            "delay": 0,
            "timeout": 60,
            "enabled_types": ["movie", "tv"],
        }

    def get_page(self) -> List[dict]:
        last = self.get_data("last_result") or {}
        lines = []
        for key in ["title", "source_path", "source_dir", "source_name", "target_dir", "target_name", "result", "message", "time"]:
            value = last.get(key)
            if value is not None and value != "":
                lines.append(f"{key}: {value}")
        text = "\n".join(lines) if lines else "暂无执行记录"
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": text,
                }
            }
        ]

    def stop_service(self):
        pass

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled:
            return
        if requests is None:
            return
        if not event or not event.event_data:
            return

        event_info: dict = event.event_data
        transferinfo = event_info.get("transferinfo")
        mediainfo = event_info.get("mediainfo")
        meta = event_info.get("meta")

        if not transferinfo:
            logger.warning("OpenList 文件转运：缺少 transferinfo，跳过")
            return

        source_path = getattr(transferinfo, "target_path", None) or event_info.get("dest")
        if not source_path:
            logger.warning("OpenList 文件转运：未获取到整理后路径，跳过")
            return

        media_type = str(getattr(mediainfo, "type", "") or "").lower()
        if media_type and self._enabled_types and media_type not in self._enabled_types:
            logger.info(f"OpenList 文件转运：媒体类型 {media_type} 未启用，跳过")
            return

        if self._delay:
            time.sleep(self._delay)

        title = self.__build_title(mediainfo, meta, source_path)
        source_file = Path(source_path)
        source_name = source_file.name
        source_dir = self.__map_source_dir(str(source_file.parent))
        if not source_dir:
            self.__record_and_notify(
                title=title,
                source_path=source_path,
                source_dir="",
                source_name=source_name,
                target_dir="",
                target_name=source_name,
                result="failed",
                message="未匹配到路径映射，请检查 path_mappings",
            )
            return

        target_root = self.__get_target_root(media_type)
        target_dir = self.__build_target_dir(target_root, mediainfo, meta)

        try:
            token = self.__get_token()
            if not token:
                raise Exception("未获取到 OpenList token，请先配置 token 或用户名密码")

            if self._create_dest_dir:
                self.__mkdir(token, target_dir)

            resp = self.__copy_file(
                token=token,
                src_dir=source_dir,
                names=[source_name],
                dst_dir=target_dir,
            )

            self.__record_and_notify(
                title=title,
                source_path=source_path,
                source_dir=source_dir,
                source_name=source_name,
                target_dir=target_dir,
                target_name=source_name,
                result="success",
                message=self.__response_text(resp),
            )
        except Exception as e:
            logger.error(f"OpenList 文件转运失败：{e}\n{traceback.format_exc()}")
            self.__record_and_notify(
                title=title,
                source_path=source_path,
                source_dir=source_dir,
                source_name=source_name,
                target_dir=target_dir,
                target_name=source_name,
                result="failed",
                message=str(e),
            )

    def __save_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "oplist_url": self._oplist_url,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "copy_api_path": self._copy_api_path,
            "mkdir_api_path": self._mkdir_api_path,
            "storage_mount_path": self._storage_mount_path,
            "movie_dest_root": self._movie_dest_root,
            "tv_dest_root": self._tv_dest_root,
            "path_mappings": self._path_mappings,
            "overwrite": self._overwrite,
            "create_dest_dir": self._create_dest_dir,
            "delay": self._delay,
            "timeout": self._timeout,
            "enabled_types": self._enabled_types,
        })

    def __build_title(self, mediainfo, meta, source_path: str) -> str:
        title = getattr(mediainfo, "title_year", None) or getattr(mediainfo, "title", None)
        if title:
            season = getattr(meta, "season", None)
            episode = getattr(meta, "episode", None)
            extra = " ".join([x for x in [season, episode] if x])
            return f"{title} {extra}".strip()
        return Path(source_path).name

    def __get_target_root(self, media_type: str) -> str:
        if media_type == "movie":
            return self._movie_dest_root
        return self._tv_dest_root

    def __build_target_dir(self, target_root: str, mediainfo, meta) -> str:
        title = getattr(mediainfo, "title", None) or "Unknown"
        year = getattr(mediainfo, "year", None)
        media_type = str(getattr(mediainfo, "type", "") or "").lower()
        if media_type == "movie":
            name = f"{title} ({year})" if year else title
            return self.__join_path(target_root, name)

        season = getattr(meta, "season", None) or "Season 01"
        return self.__join_path(target_root, title, season)

    def __parse_mappings(self) -> List[Tuple[str, str]]:
        mappings = []
        for line in (self._path_mappings or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, right = line.split("=", 1)
            left = left.strip().rstrip("/")
            right = right.strip().rstrip("/") or "/"
            if left:
                mappings.append((left, right))
        mappings.sort(key=lambda x: len(x[0]), reverse=True)
        return mappings

    def __map_source_dir(self, local_dir: str) -> str:
        local_dir = local_dir.rstrip("/") or "/"
        mappings = self.__parse_mappings()
        for src_prefix, dst_prefix in mappings:
            if local_dir == src_prefix or local_dir.startswith(src_prefix + "/"):
                suffix = local_dir[len(src_prefix):].lstrip("/")
                if suffix:
                    return self.__join_path(dst_prefix, suffix)
                return dst_prefix or "/"
        base = (self._storage_mount_path or "/").rstrip("/")
        if base == "":
            base = "/"
        if base == "/":
            return local_dir
        if local_dir.startswith(base + "/") or local_dir == base:
            return local_dir
        return ""

    def __join_path(self, *parts: str) -> str:
        cleaned = []
        for idx, part in enumerate(parts):
            if part is None:
                continue
            part = str(part).strip()
            if not part:
                continue
            if idx == 0 and part == "/":
                cleaned.append("")
                continue
            cleaned.append(part.strip("/"))
        result = "/" + "/".join([p for p in cleaned if p != ""])
        return result or "/"

    def __headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def __get_token(self) -> str:
        if self._token:
            return self._token
        if not self._username or not self._password:
            return ""

        login_candidates = [
            "/api/auth/login",
            "/api/public/login",
            "/api/user/login",
        ]
        payloads = [
            {"username": self._username, "password": self._password},
            {"user": self._username, "password": self._password},
        ]
        for api_path in login_candidates:
            for payload in payloads:
                try:
                    url = f"{self._oplist_url}{api_path}"
                    resp = requests.post(url, json=payload, timeout=self._timeout)
                    if resp.ok:
                        data = self.__safe_json(resp)
                        token = self.__extract_token(data)
                        if token:
                            self._token = token
                            self.__save_config()
                            return token
                except Exception:
                    continue
        return ""

    def __extract_token(self, data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ["token", "access_token"]:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ["token", "access_token"]:
                value = inner.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    def __mkdir(self, token: str, path: str):
        payloads = [
            {"path": path},
            {"dir": path},
        ]
        self.__post_with_candidates(token, self._mkdir_api_path, payloads, allow_fail=True)

    def __copy_file(self, token: str, src_dir: str, names: List[str], dst_dir: str):
        payloads = [
            {"src_dir": src_dir, "names": names, "dst_dir": dst_dir},
            {"src_dir": src_dir, "src_names": names, "dst_dir": dst_dir},
            {"src": src_dir, "names": names, "dst": dst_dir, "overwrite": self._overwrite},
            {"path": src_dir, "names": names, "target_dir": dst_dir, "overwrite": self._overwrite},
        ]
        return self.__post_with_candidates(token, self._copy_api_path, payloads, allow_fail=False)

    def __post_with_candidates(self, token: str, api_path: str, payloads: List[Dict[str, Any]], allow_fail: bool):
        last_error = None
        for payload in payloads:
            try:
                url = f"{self._oplist_url}{api_path}"
                resp = requests.post(url, headers=self.__headers(token), json=payload, timeout=self._timeout)
                if resp.ok:
                    return resp
                text = resp.text[:500]
                last_error = Exception(f"HTTP {resp.status_code}: {text}")
            except Exception as e:
                last_error = e
        if allow_fail:
            logger.warning(f"OpenList 调用失败但忽略：{last_error}")
            return None
        raise last_error or Exception("OpenList 请求失败")

    def __safe_json(self, resp) -> Any:
        try:
            return resp.json()
        except Exception:
            return {}

    def __response_text(self, resp) -> str:
        if not resp:
            return "ok"
        data = self.__safe_json(resp)
        if isinstance(data, dict):
            for key in ["message", "msg"]:
                value = data.get(key)
                if value:
                    return str(value)
            inner = data.get("data")
            if inner:
                try:
                    return json.dumps(inner, ensure_ascii=False)
                except Exception:
                    return str(inner)
        return resp.text[:500] if hasattr(resp, "text") else "ok"

    def __record_and_notify(self, title: str, source_path: str, source_dir: str, source_name: str,
                            target_dir: str, target_name: str, result: str, message: str):
        record = {
            "title": title,
            "source_path": source_path,
            "source_dir": source_dir,
            "source_name": source_name,
            "target_dir": target_dir,
            "target_name": target_name,
            "result": result,
            "message": message,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("last_result", record)
        self._last_result = record

        if not self._notify:
            return

        notify_title = "【OpenList 文件转运成功】" if result == "success" else "【OpenList 文件转运失败】"
        notify_text = (
            f"媒体：{title}\n"
            f"源文件：{source_name}\n"
            f"源目录：{source_dir}\n"
            f"目标目录：{target_dir}\n"
            f"动作：copy\n"
            f"结果：{result}\n"
            f"详情：{message}"
        )
        self.post_message(
            mtype=NotificationType.Plugin,
            title=notify_title,
            text=notify_text,
        )
