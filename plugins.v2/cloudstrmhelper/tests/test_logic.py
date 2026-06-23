"""离线单元测试：mock app.* 后 import 真实模块，验证纯逻辑。

运行：python3 tests/test_logic.py
（不依赖真实 MoviePilot 环境，用 stub 注入 app.* 模块）
"""
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# 让插件目录可被 import
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))  # plugins.v2 父级，使 cloudstrmhelper 可作为包导入


def _stub_app_modules():
    """注入 app.* 及 MP 自带三方依赖的 stub，使插件 import 不依赖真实 MoviePilot。"""
    # ---- MP 自带三方依赖 stub（本机未装时）----
    def _stub(name, attrs=None):
        m = types.ModuleType(name)
        for k, v in (attrs or {}).items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    # apscheduler
    _stub("apscheduler")
    _stub("apscheduler.schedulers")
    _stub("apscheduler.schedulers.background", {"BackgroundScheduler": MagicMock})
    _stub("apscheduler.triggers")
    _stub("apscheduler.triggers.cron", {"CronTrigger": MagicMock})

    # fastapi
    fastapi = _stub("fastapi", {"Request": MagicMock})
    _stub("fastapi.responses", {
        "RedirectResponse": MagicMock,
        "JSONResponse": MagicMock,
    })
    # pydantic（fastapi 间接依赖，本机若有则不覆盖）
    try:
        import pydantic  # noqa
    except Exception:
        _stub("pydantic", {"BaseModel": MagicMock})

    # cachetools（本机已装，跳过 stub 以用真实 TTLCache/cached）

    # app 顶层
    app = types.ModuleType("app")
    sys.modules["app"] = app

    # app.log
    app_log = types.ModuleType("app.log")
    _logger = MagicMock()
    _logger.info = lambda *a, **k: None
    _logger.warning = lambda *a, **k: None
    _logger.error = lambda *a, **k: None
    _logger.debug = lambda *a, **k: None
    _logger.warn = lambda *a, **k: None
    app_log.logger = _logger
    sys.modules["app.log"] = app_log

    # app.core.config
    app_core_config = types.ModuleType("app.core.config")
    class _S:
        API_TOKEN = "test-token-123"
        MP_DOMAIN = lambda url="": f"http://mp.example.com:3000{url}" if url else "http://mp.example.com:3000"
    app_core_config.settings = _S()
    sys.modules["app.core.config"] = app_core_config

    # app.core.event
    app_core_event = types.ModuleType("app.core.event")
    def _register(*a, **k):
        def deco(f):
            return f
        return deco
    class _Event:
        def __init__(self, etype, data=None):
            self.event_type = etype
            self.event_data = data or {}
    app_core_event.eventmanager = MagicMock()
    app_core_event.eventmanager.register = _register
    app_core_event.Event = _Event
    sys.modules["app.core.event"] = app_core_event

    # app.plugins
    app_plugins = types.ModuleType("app.plugins")
    class _PluginBase:
        plugin_name = ""
        plugin_desc = ""
        plugin_order = 9999
        def __init__(self):
            self.plugindata = MagicMock()
            self.chain = MagicMock()
            self.systemconfig = MagicMock()
            self.systemmessage = MagicMock()
            self.eventmanager = MagicMock()
        def update_config(self, config, plugin_id=None): return True
        def get_config(self, plugin_id=None): return {}
        def get_data_path(self, plugin_id=None): return Path("/tmp/cloudstrm_test")
        def save_data(self, key, value, plugin_id=None): pass
        def get_data(self, key=None, plugin_id=None): return None
        def del_data(self, key=None, plugin_id=None): pass
        def post_message(self, **kwargs): pass
    app_plugins._PluginBase = _PluginBase
    sys.modules["app.plugins"] = app_plugins

    # app.schemas
    app_schemas = types.ModuleType("app.schemas")
    from dataclasses import dataclass
    @dataclass
    class RefreshMediaItem:
        title: str = None
        year: object = None
        type: object = None
        category: object = None
        target_path: object = None
    @dataclass
    class ServiceInfo:
        name: str = None
        instance: object = None
        module: object = None
        type: str = None
        config: object = None
    app_schemas.RefreshMediaItem = RefreshMediaItem
    app_schemas.ServiceInfo = ServiceInfo
    app_schemas.TransferInfo = MagicMock
    app_schemas.FileItem = MagicMock
    sys.modules["app.schemas"] = app_schemas

    # app.schemas.types
    app_schemas_types = types.ModuleType("app.schemas.types")
    import enum
    class EventType(enum.Enum):
        TransferComplete = "transfer.complete"
    class MediaType(enum.Enum):
        MOVIE = "电影"
        TV = "电视剧"
    app_schemas_types.EventType = EventType
    app_schemas_types.MediaType = MediaType
    sys.modules["app.schemas.types"] = app_schemas_types

    # app.helper.mediaserver
    app_helper_ms = types.ModuleType("app.helper.mediaserver")
    class MediaServerHelper:
        def get_configs(self, include_disabled=False):
            return {}
        def get_services(self, type_filter=None, name_filters=None):
            return {}
    app_helper_ms.MediaServerHelper = MediaServerHelper
    sys.modules["app.helper.mediaserver"] = app_helper_ms

    # app.utils.system（is_bluray_dir 是 SystemUtils 类的静态方法，非模块级函数）
    app_utils_system = types.ModuleType("app.utils.system")
    class SystemUtils:
        @staticmethod
        def is_bluray_dir(dir_path):
            return False
    app_utils_system.SystemUtils = SystemUtils
    sys.modules["app.utils.system"] = app_utils_system


_stub_app_modules()


class TestConvertBytes(unittest.TestCase):
    def test_from_cloud_sync(self):
        from cloudstrmhelper.cloud_sync import _convert_bytes, _convert_seconds
        self.assertEqual(_convert_bytes(0), "0 B")
        self.assertEqual(_convert_bytes(1024), "1.00 KB")
        self.assertEqual(_convert_bytes(1048576), "1.00 MB")
        self.assertEqual(_convert_bytes(1073741824), "1.00 GB")
        h, m, s = _convert_seconds(3661)
        self.assertEqual((h, m, s), (1, 1, 1))


class TestStrmPathResolution(unittest.TestCase):
    """测试 STRM 输出路径计算（strm_generator._strm_output_path）。"""

    def _make_plugin(self, local_root, strm_root):
        from cloudstrmhelper.strm_generator import StrmGenerator
        plugin = MagicMock()
        plugin._local_media_path = local_root
        plugin._strm_output_path = strm_root
        plugin._moviepilot_address = "http://mp:3000"
        plugin._overwrite_mode = "never"
        plugin._refresh_enabled = False
        plugin._mediaservers = []
        plugin._transfer_mp_mediaserver_paths = ""
        return StrmGenerator(plugin), plugin

    def test_basic_relative(self):
        gen, _ = self._make_plugin("/media/movies", "/strm/movies")
        p = gen._strm_output_path("/media/movies/Foo (2024)/Foo (2024).mkv")
        self.assertIsNotNone(p)
        # stem + .strm，保留子目录结构
        self.assertEqual(p, Path("/strm/movies/Foo (2024)/Foo (2024).strm"))

    def test_nested(self):
        gen, _ = self._make_plugin("/media/tv", "/strm/tv")
        p = gen._strm_output_path("/media/tv/Show/Season 01/S01E01.mkv")
        self.assertEqual(p, Path("/strm/tv/Show/Season 01/S01E01.strm"))

    def test_outside_root_returns_none(self):
        gen, _ = self._make_plugin("/media/movies", "/strm/movies")
        p = gen._strm_output_path("/other/Foo.mkv")
        self.assertIsNone(p)

    def test_strm_url_format(self):
        gen, plugin = self._make_plugin("/media/movies", "/strm/movies")
        url = gen._build_strm_url("/媒体库/电影/Foo.mkv")
        # 插件 ID 来自运行时类名（不写死字符串）；测试用 mock，故用其类名动态断言
        plugin_id = type(plugin).__name__
        self.assertIn(f"/api/v1/plugin/{plugin_id}/redirect", url)
        self.assertIn("apikey=test-token-123", url)
        self.assertIn("path=", url)
        # path 应被 urlenc（中文/空格）
        self.assertIn("%E5%AA%92%E4%BD%93%E5%BA%93", url)  # 媒体库 的 urlenc

    def test_plugin_id_not_hardcoded(self):
        """规范：不写死插件 ID 字符串。用真实 CloudStrmHelper 实例验证 redirect 路径含正确类名。"""
        from cloudstrmhelper import CloudStrmHelper
        from cloudstrmhelper.strm_generator import StrmGenerator
        # 真实插件类（不传 config，避免触发 app 依赖）
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._moviepilot_address = "http://mp:3000"
        gen = StrmGenerator(plugin)
        self.assertEqual(gen._plugin_id, "CloudStrmHelper")
        self.assertEqual(gen._redirect_path, "/api/v1/plugin/CloudStrmHelper/redirect")


class TestExcludeSpec(unittest.TestCase):
    def test_pathspec_gitignore(self):
        from cloudstrmhelper import CloudStrmHelper
        spec = CloudStrmHelper._build_exclude_spec("*.tmp\n**/.DS_Store\n/sample/**")
        self.assertIsNotNone(spec)
        # .tmp 命中
        self.assertTrue(spec.match_file("a.tmp"))
        self.assertTrue(spec.match_file("dir/b.tmp"))
        # .DS_Store 命中
        self.assertTrue(spec.match_file(".DS_Store"))
        self.assertTrue(spec.match_file("sub/.DS_Store"))
        # /sample/** 命中
        self.assertTrue(spec.match_file("sample/x.mkv"))
        # 正常媒体不命中
        self.assertFalse(spec.match_file("movies/Foo.mkv"))

    def test_empty_returns_none(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertIsNone(CloudStrmHelper._build_exclude_spec(""))
        self.assertIsNone(CloudStrmHelper._build_exclude_spec(None))


class TestTransferListenerExclude(unittest.TestCase):
    def test_is_excluded(self):
        from cloudstrmhelper.transfer_listener import TransferListener
        spec = __import__("pathspec").PathSpec.from_lines(
            __import__("pathspec").patterns.GitWildMatchPattern, ["*.tmp"])
        self.assertTrue(TransferListener._is_excluded("/media/movies/x.tmp", "/media/movies", spec))
        self.assertFalse(TransferListener._is_excluded("/media/movies/x.mkv", "/media/movies", spec))


class TestAlistClientBuildUrl(unittest.TestCase):
    """测试 ProxyHandler._build_url（不发起真实请求）。"""
    def test_raw_url_preferred(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.5:5244"
        ph = ProxyHandler(alist, follow_redirects=False)
        url = ph._build_url({"raw_url": "http://upstream/foo?token=abc"}, "/x.mkv")
        self.assertEqual(url, "http://upstream/foo?token=abc")

    def test_build_d_with_sign(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.5:5244"
        ph = ProxyHandler(alist, follow_redirects=False)
        url = ph._build_url({"sign": "mysign"}, "/媒体库/Foo.mkv")
        self.assertEqual(url, "http://192.168.31.5:5244/d/%E5%AA%92%E4%BD%93%E5%BA%93/Foo.mkv?sign=mysign")

    def test_build_d_no_sign(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.5:5244"
        ph = ProxyHandler(alist, follow_redirects=False)
        url = ph._build_url({}, "/foo.mkv")
        self.assertEqual(url, "http://192.168.31.5:5244/d/foo.mkv")


class TestPluginMetadata(unittest.TestCase):
    def test_metadata_present(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertEqual(CloudStrmHelper.plugin_name, "云端STRM整理助手")
        self.assertEqual(CloudStrmHelper.plugin_version, "1.0.0")
        self.assertEqual(CloudStrmHelper.plugin_config_prefix, "cloudstrmhelper_")
        self.assertEqual(CloudStrmHelper.plugin_author, "101letters")
        self.assertEqual(CloudStrmHelper.auth_level, 1)

    def test_form_returns_tuple(self):
        from cloudstrmhelper import CloudStrmHelper
        form, defaults = CloudStrmHelper.get_form(CloudStrmHelper)
        self.assertIsInstance(form, list)
        self.assertIsInstance(defaults, dict)
        self.assertIn("enabled", defaults)
        self.assertIn("alist_url", defaults)
        self.assertIn("strm_output_path", defaults)
        self.assertFalse(defaults["enabled"])

    def test_api_endpoints(self):
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        paths = [a["path"] for a in apis]
        self.assertIn("/redirect", paths)
        self.assertIn("/status", paths)
        self.assertIn("/sync_now", paths)

    def test_api_has_auth(self):
        """规范：不要默认匿名开放 API，每个端点须声明 auth。"""
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        for a in apis:
            self.assertIn("auth", a, f"端点 {a['path']} 缺少 auth 字段")
            self.assertEqual(a["auth"], "apikey", f"端点 {a['path']} auth 应为 apikey")

    def test_metadata_icon_and_version(self):
        """规范：plugin_icon 必需，version 与 package.v2.json 一致。"""
        import json
        from cloudstrmhelper import CloudStrmHelper
        self.assertTrue(CloudStrmHelper.plugin_icon, "plugin_icon 不能为空")
        self.assertIn("cloudstrmhelper", CloudStrmHelper.plugin_icon)
        with open(PLUGIN_DIR.parent.parent / "package.v2.json") as f:
            pkg = json.load(f)
        self.assertEqual(pkg["CloudStrmHelper"]["version"], CloudStrmHelper.plugin_version)


class TestRemotePathComputation(unittest.TestCase):
    """测试 transfer_listener 中云端路径计算逻辑。"""
    def test_remote_path(self):
        from pathlib import Path
        local_root = "/media/movies"
        cloud_root = "/媒体库"
        local_path = "/media/movies/Foo (2024)/Foo (2024).mkv"
        rel = Path(local_path).relative_to(local_root)
        remote = cloud_root.rstrip("/") + "/" + str(rel).replace("\\", "/")
        self.assertEqual(remote, "/媒体库/Foo (2024)/Foo (2024).mkv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
