"""OpenList API client for OpenListSync plugin."""
from typing import Optional, List, Dict

from app.log import logger

try:
    import requests
except ImportError:
    requests = None


class OpenListError(Exception):
    pass


class AuthError(OpenListError):
    pass


class OpenListClient:
    """OpenList / AList API client.

    All requests carry ``Authorization: <token>`` header.
    """

    def __init__(self, base_url: str = "", token: str = "", timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self._token = (token or "").strip()
        self.timeout = timeout or 60
        self._session = None if requests is None else requests.Session()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _check_session(self):
        if self._session is None:
            raise OpenListError("缺少 requests 依赖")

    def _headers(self) -> dict:
        return {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

    def _request(self, path: str, json_body: dict) -> dict:
        """Send POST request and parse response."""
        self._check_session()
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.post(
                url,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OpenListError(f"请求失败 {path}: {e}") from e

        data = self._parse_response(resp)
        return data

    @staticmethod
    def _parse_response(resp) -> dict:
        """Validate response code==200, return JSON data."""
        try:
            data = resp.json()
        except Exception:
            raise OpenListError(f"响应非 JSON: {resp.status_code}")
        code = data.get("code")
        if code != 200:
            raise OpenListError(
                f"API 返回错误 code={code}, message={data.get('message', '')}"
            )
        return data.get("data", {})

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def get(self, path: str) -> dict:
        """GET file/dir info via POST /api/fs/get."""
        return self._request("/api/fs/get", {"path": path})

    def list_dir(self, path: str) -> List[dict]:
        """List directory contents via POST /api/fs/list.

        Returns list of dict with keys: name, size, is_dir, ...
        """
        data = self._request("/api/fs/list", {
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False,
        })
        if isinstance(data, dict):
            files = data.get("content") or data.get("files") or []
            if not files and isinstance(data.get("data"), list):
                files = data["data"]
            return files if isinstance(files, list) else []
        if isinstance(data, list):
            return data
        return []

    def copy(self, src_dir: str, dst_dir: str, names: List[str]) -> None:
        """Copy files within OpenList via POST /api/fs/copy."""
        logger.debug(f"OpenList copy: {src_dir} -> {dst_dir}, names={names}")
        self._request("/api/fs/copy", {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names,
        })

    def move(self, src_dir: str, dst_dir: str, names: List[str]) -> None:
        """Move files within OpenList via POST /api/fs/move."""
        logger.debug(f"OpenList move: {src_dir} -> {dst_dir}, names={names}")
        self._request("/api/fs/move", {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names,
        })

    def remove(self, dir_path: str, names: List[str]) -> None:
        """Remove files via POST /api/fs/remove."""
        logger.debug(f"OpenList remove: {dir_path}, names={names}")
        self._request("/api/fs/remove", {
            "dir": dir_path,
            "names": names,
        })

    def mkdir(self, path: str) -> None:
        """Create directory via POST /api/fs/mkdir."""
        logger.debug(f"OpenList mkdir: {path}")
        self._request("/api/fs/mkdir", {"path": path})

    def list_dir_recursive(self, path: str) -> List[dict]:
        """Try to list all files recursively via API (if supported).

        Falls back to non-recursive list_dir if the API doesn't support it.
        """
        data = self._request("/api/fs/list", {
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False,
            "recursive": True,
        })
        if isinstance(data, dict):
            files = data.get("content") or data.get("files") or []
            if not files and isinstance(data.get("data"), list):
                files = data["data"]
            return files if isinstance(files, list) else []
        if isinstance(data, list):
            return data
        return []
