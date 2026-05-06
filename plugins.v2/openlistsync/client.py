"""OpenList API client for OpenListSync plugin."""
from typing import Optional, List
from app.log import logger

try:
    import requests
except ImportError:
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
    def __init__(self, base_url: str = "", username: str = "", password: str = "",
                 token: str = "", timeout: int = 60):
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

    def _post(self, api_path: str, payload: dict) -> dict:
        if not self._session:
            raise OpenListError("缺少 requests 依赖")
        if not self.base_url:
            raise NonRetryableError("OpenList 地址未配置")
        try:
            resp = self._session.post(
                f"{self.base_url}{api_path}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            return self._parse_response(resp)
        except AuthError:
            if not (self._username and self._password):
                raise
            self._token = ""
            self.login()
            try:
                resp = self._session.post(
                    f"{self.base_url}{api_path}",
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                return self._parse_response(resp)
            except requests.RequestException as e:
                raise RetryableError(f"OpenList API 重试失败 {api_path}: {e}") from e
        except requests.RequestException as e:
            raise RetryableError(f"OpenList API 请求失败 {api_path}: {e}") from e

    @staticmethod
    def _parse_response(resp):
        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"OpenList 鉴权失败: HTTP {status}")
        if status == 404:
            raise NonRetryableError(f"OpenList 路径不存在: HTTP {status}")
        if 400 <= status < 500 and status != 429:
            raise NonRetryableError(f"OpenList 请求失败: HTTP {status}")
        if status != 200:
            raise RetryableError(f"OpenList 服务异常: HTTP {status}")
        try:
            data = resp.json()
        except Exception as e:
            raise RetryableError(f"OpenList 响应非 JSON: HTTP {status}") from e
        code = data.get("code")
        message = data.get("message") or data.get("msg") or ""
        result = data.get("data")
        if code in (200, 0):
            return result
        msg_lower = str(message).lower()
        if code in (401, 403):
            raise AuthError(f"OpenList 鉴权失败: {message or code}")
        if code == 404 or "not found" in msg_lower or "not exist" in msg_lower or "不存在" in str(message):
            raise NonRetryableError(f"OpenList 路径不存在: {message or code}")
        try:
            code_int = int(code or 0)
        except Exception:
            code_int = 0
        if 400 <= code_int < 500 and code_int != 429:
            raise NonRetryableError(f"OpenList 请求失败: {message or code}")
        raise RetryableError(f"OpenList 服务异常: {message or code}")

    def fs_get(self, path: str) -> Optional[dict]:
        try:
            return self._post("/api/fs/get", {"path": path})
        except NonRetryableError as e:
            msg = str(e).lower()
            if "not found" in msg or "not exist" in msg or "不存在" in str(e):
                return None
            raise

    def get(self, path: str) -> Optional[dict]:
        return self.fs_get(path)

    def exists(self, path: str) -> bool:
        return self.fs_get(path) is not None

    def copy(self, src_dir: str, dst_dir: str, names: List[str]) -> bool:
        if not names:
            return True
        self._post("/api/fs/copy", {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names,
        })
        return True

    def move(self, src_dir: str, dst_dir: str, names: List[str]) -> bool:
        if not names:
            return True
        self._post("/api/fs/move", {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names,
        })
        return True

    def remove(self, dir_path: str, names: List[str]) -> bool:
        if not names:
            return True
        self._post("/api/fs/remove", {
            "dir": dir_path,
            "names": names,
        })
        return True

    def mkdir(self, path: str) -> bool:
        self._post("/api/fs/mkdir", {"path": path})
        return True

    def list_dir(self, path: str) -> List[dict]:
        data = self._post("/api/fs/list", {
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False,
        })
        content = (data or {}).get("content") or []
        return content if isinstance(content, list) else []

    def test_connection(self):
        if not self.base_url:
            raise NonRetryableError("OpenList 地址未配置")
        self.ensure_token()
        self._post("/api/fs/list", {
            "path": "/", "password": "", "page": 1, "per_page": 1, "refresh": False
        })
        return True, "连接成功"
