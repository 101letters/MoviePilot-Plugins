"""OpenList API client for server-side copy."""
from typing import Optional, List

from app.log import logger

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class OpenListError(Exception):
    pass


class AuthError(OpenListError):
    pass


class RetryableError(OpenListError):
    pass


class NonRetryableError(OpenListError):
    pass


class OpenListClient:
    def __init__(self, base_url: str = "", username: str = "", password: str = "", token: str = "", timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self._username = username or ""
        self._password = password or ""
        self._token = (token or "").strip()
        self.timeout = timeout or 60
        self._session = requests.Session() if requests else None

    def login(self) -> str:
        if not self._session:
            raise OpenListError("缺少 requests 依赖")
        if not self.base_url:
            raise NonRetryableError("OpenList 地址未配置")
        try:
            resp = self._session.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
                timeout=self.timeout,
            )
            data = self._parse_response(resp)
            token = (data or {}).get("token", "")
            if not token:
                raise AuthError("登录成功但未返回 token")
            self._token = token
            logger.info("OpenList 登录成功")
            return token
        except requests.RequestException as e:
            raise RetryableError(f"OpenList 登录失败: {e}") from e

    def ensure_token(self):
        if self._token:
            return
        if self._username and self._password:
            self.login()
            return
        raise AuthError("未配置 token 且无用户名密码")

    def _headers(self):
        self.ensure_token()
        return {"Authorization": self._token, "Content-Type": "application/json"}

    def fs_get(self, path: str) -> Optional[dict]:
        if not self._session:
            raise OpenListError("缺少 requests 依赖")
        try:
            resp = self._session.post(
                f"{self.base_url}/api/fs/get",
                json={"path": path},
                headers=self._headers(),
                timeout=self.timeout,
            )
            return self._parse_response(resp)
        except AuthError:
            if self._username and self._password:
                self._token = ""
                resp = self._session.post(
                    f"{self.base_url}/api/fs/get",
                    json={"path": path}, headers=self._headers(), timeout=self.timeout,
                )
                return self._parse_response(resp)
            raise
        except requests.RequestException as e:
            raise RetryableError(f"检查路径失败 {path}: {e}") from e

    def exists(self, path: str) -> bool:
        return self.fs_get(path) is not None

    def copy(self, src_dir: str, dst_dir: str, names: List[str]) -> bool:
        """Call OpenList/AList server-side copy API: POST /api/fs/copy."""
        if not self._session:
            raise OpenListError("缺少 requests 依赖")
        payload = {"src_dir": src_dir, "dst_dir": dst_dir, "names": names}
        try:
            resp = self._session.post(
                f"{self.base_url}/api/fs/copy",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            self._parse_response(resp)
            logger.info(f"OpenList copy 已提交: {src_dir} -> {dst_dir}, names={names}")
            return True
        except AuthError:
            if self._username and self._password:
                self._token = ""
                resp = self._session.post(
                    f"{self.base_url}/api/fs/copy",
                    json=payload, headers=self._headers(), timeout=self.timeout,
                )
                self._parse_response(resp)
                return True
            raise
        except requests.RequestException as e:
            raise RetryableError(f"OpenList copy 请求失败: {e}") from e

    def test_connection(self) -> str:
        if not self._session:
            return "缺少 requests 依赖"
        if not self.base_url:
            return "OpenList 地址未配置"
        try:
            resp = self._session.get(f"{self.base_url}/api/public/settings", timeout=10)
            if resp.status_code != 200:
                return f"连接失败: HTTP {resp.status_code}"
            data = resp.json()
            if data.get("code") == 200:
                version = (data.get("data") or {}).get("version", "未知")
                return f"连接成功 (v{version})"
            return f"连接失败: {data.get('message', '未知错误')}"
        except Exception as e:
            return f"连接失败: {e}"

    def _parse_response(self, resp):
        if resp.status_code == 401:
            raise AuthError("认证失败，token 无效")
        if resp.status_code >= 500:
            raise RetryableError(f"服务端错误: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            if 200 <= resp.status_code < 300:
                return None
            raise NonRetryableError(f"响应解析失败: HTTP {resp.status_code}")
        code = data.get("code", -1)
        if code == 200:
            return data.get("data")
        msg = data.get("message", f"未知错误 (code={code})")
        if code in (401, 403):
            raise AuthError(msg)
        if code == 404:
            return None
        if code in (429, 500, 502, 503):
            raise RetryableError(msg)
        if isinstance(msg, str) and ("not found" in msg.lower() or "不存在" in msg):
            return None
        raise NonRetryableError(msg)
