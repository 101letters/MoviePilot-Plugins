"""OpenList API 客户端 — 支持登录、创建目录、检查文件、流式上传"""

import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app.log import logger

try:
    import requests
except ImportError:
    requests = None


class OpenListError(Exception):
    """OpenList API 通用异常"""


class AuthError(OpenListError):
    """认证失败 — token 无效或未登录"""


class RetryableError(OpenListError):
    """可重试的临时错误 — 网络超时、5xx、429 等"""


class NonRetryableError(OpenListError):
    """不可重试的错误 — 路径非法、权限不足、文件不存在等"""


class OpenListClient:
    """OpenList/AList API 客户端

    支持两种认证方式：
      1. 用户名+密码自动登录（token 失效可自动刷新）
      2. 固定 token（不保存密码，过期需手动更新）
    """

    def __init__(
        self,
        base_url: str = "",
        username: str = "",
        password: str = "",
        token: str = "",
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token.strip()
        self.timeout = timeout
        self._session = requests.Session() if requests else None

    # ─── 登录 / Token 管理 ──────────────────────────────────────

    def login(self) -> str:
        """用用户名+密码登录，返回 token"""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")

        url = f"{self.base_url}/api/auth/login"
        payload = {
            "username": self._username,
            "password": self._password,
        }

        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            data = self._parse_response(resp)
            self._token = data.get("token", "")
            if not self._token:
                raise AuthError("登录成功但未返回 token")
            logger.info("OpenList 登录成功")
            return self._token
        except requests.RequestException as e:
            raise RetryableError(f"OpenList 登录失败: {e}") from e

    def ensure_token(self):
        """确保 token 有效，必要时重新登录"""
        if self._token:
            return
        if self._username and self._password:
            self.login()
        else:
            raise AuthError("未配置 token 且无用户名密码")

    # ─── 目录操作 ───────────────────────────────────────────────

    def mkdir(self, remote_dir: str) -> bool:
        """创建远端目录，已存在也不报错"""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")

        self.ensure_token()
        url = f"{self.base_url}/api/fs/mkdir"
        headers = {"Authorization": self._token}

        try:
            resp = self._session.post(
                url,
                headers=headers,
                json={"path": remote_dir},
                timeout=self.timeout,
            )
            # 200 = 创建成功，目录已存在时也是 200（不同版本可能有差异）
            return resp.status_code == 200
        except requests.RequestException as e:
            raise RetryableError(f"创建目录失败 {remote_dir}: {e}") from e

    # ─── 文件检查 ───────────────────────────────────────────────

    def exists(self, remote_path: str) -> bool:
        """检查远端文件是否存在"""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")

        self.ensure_token()
        url = f"{self.base_url}/api/fs/get"
        headers = {"Authorization": self._token}

        try:
            resp = self._session.post(
                url,
                headers=headers,
                json={"path": remote_path},
                timeout=self.timeout,
            )
            data = self._parse_response(resp)
            return data is not None
        except (RetryableError, NonRetryableError):
            return False
        except requests.RequestException:
            return False

    # ─── 上传 ───────────────────────────────────────────────────

    def upload_put(self, local_path: Path, remote_path: str) -> bool:
        """流式上传文件 PUT /api/fs/put"""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")

        if not local_path.is_file():
            raise NonRetryableError(f"本地文件不存在: {local_path}")

        self.ensure_token()

        url = f"{self.base_url}/api/fs/put"
        headers = {
            "Authorization": self._token,
            "File-Path": quote(remote_path, safe="/"),
            "Content-Type": "application/octet-stream",
        }

        file_size = local_path.stat().st_size
        if file_size == 0:
            raise NonRetryableError(f"文件大小为 0，跳过: {local_path}")

        try:
            with open(local_path, "rb") as f:
                resp = self._session.put(
                    url,
                    headers=headers,
                    data=f,
                    timeout=self.timeout,
                )
            self._parse_response(resp)
            logger.info(f"上传成功: {local_path.name} → {remote_path}")
            return True
        except AuthError:
            # token 失效，尝试重新登录后重试一次
            if self._username and self._password:
                logger.warning("Token 失效，重新登录后重试")
                self._token = ""
                self.login()
                return self.upload_put(local_path, remote_path)
            raise
        except requests.RequestException as e:
            raise RetryableError(f"上传失败 {local_path.name}: {e}") from e

    def upload_form(self, local_path: Path, remote_path: str) -> bool:
        """表单上传文件 PUT /api/fs/form（备选方案）"""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")

        if not local_path.is_file():
            raise NonRetryableError(f"本地文件不存在: {local_path}")

        self.ensure_token()

        url = f"{self.base_url}/api/fs/form"
        headers = {
            "Authorization": self._token,
            "File-Path": quote(remote_path, safe="/"),
        }

        file_size = local_path.stat().st_size
        if file_size == 0:
            raise NonRetryableError(f"文件大小为 0，跳过: {local_path}")

        try:
            with open(local_path, "rb") as f:
                files = {"file": (local_path.name, f, "application/octet-stream")}
                resp = self._session.put(
                    url,
                    headers=headers,
                    files=files,
                    timeout=self.timeout,
                )
            self._parse_response(resp)
            logger.info(f"表单上传成功: {local_path.name} → {remote_path}")
            return True
        except AuthError:
            if self._username and self._password:
                logger.warning("Token 失效，重新登录后重试")
                self._token = ""
                self.login()
                return self.upload_form(local_path, remote_path)
            raise
        except requests.RequestException as e:
            raise RetryableError(f"表单上传失败 {local_path.name}: {e}") from e

    # ─── 连接测试 ───────────────────────────────────────────────

    def test_connection(self) -> str:
        """测试与 OpenList 的连接，返回状态信息"""
        if not self._session:
            return "缺少 requests 依赖"

        try:
            resp = self._session.get(
                f"{self.base_url}/api/public/settings",
                timeout=10,
            )
            if resp.status_code != 200:
                return f"连接失败: HTTP {resp.status_code}"
            data = resp.json()
            if data.get("code") == 200:
                version = data.get("data", {}).get("version", "未知")
                return f"连接成功 (v{version})"
            return f"连接失败: {data.get('message', '未知错误')}"
        except requests.RequestException as e:
            return f"连接失败: {e}"

    # ─── 内部方法 ──────────────────────────────────────────────

    def _parse_response(self, resp):
        """解析 OpenList API 响应，异常时抛对应错误"""
        if resp.status_code == 401:
            raise AuthError("认证失败，token 无效")

        if resp.status_code >= 500:
            raise RetryableError(f"服务端错误: HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            if resp.status_code == 200:
                return None
            raise NonRetryableError(f"响应解析失败: HTTP {resp.status_code}")

        code = data.get("code", -1)

        if code == 200:
            return data.get("data")

        if code in (401, 403):
            raise AuthError(data.get("message", "认证失败"))

        if code in (429, 500, 502, 503):
            raise RetryableError(data.get("message", f"服务端错误 (code={code})"))

        if code == 404:
            return None  # 文件不存在

        raise NonRetryableError(data.get("message", f"未知错误 (code={code})"))
