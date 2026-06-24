"""离线单元测试：mock app.* 后 import 真实模块，验证纯逻辑。

运行：python3 tests/test_logic.py
（不依赖真实 MoviePilot 环境，用 stub 注入 app.* 模块）
"""
import os
import sys
import tempfile
import types
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    fastapi = _stub("fastapi", {"FastAPI": MagicMock, "Request": MagicMock})
    _stub("fastapi.responses", {
        "RedirectResponse": MagicMock,
        "JSONResponse": MagicMock,
        "Response": MagicMock,
        "StreamingResponse": MagicMock,
    })
    # pydantic（fastapi 间接依赖，本机若有则用真实的，否则用最小可用 stub）
    try:
        import pydantic  # noqa
    except Exception:
        class _StubBaseModel:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

            @classmethod
            def model_validate(cls, data):
                return cls(**data)

        def _Field(default="", **kw):
            return default

        _stub("pydantic", {"BaseModel": _StubBaseModel, "Field": _Field})

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
        plugin._local_media_roots = [local_root]
        plugin._local_strm_paths = f"{local_root}#{strm_root}"
        plugin._local_strm_mappings = [(local_root, strm_root)]
        plugin._strm_output_path = strm_root
        plugin._moviepilot_address = "http://mp:3000"
        plugin._overwrite_mode = "never"
        plugin._refresh_enabled = False
        plugin._mediaservers = []
        plugin._transfer_mp_mediaserver_paths = ""
        # 防御性过滤：测试用例默认不限制扩展名、不排除
        plugin._rmt_mediaext = []
        plugin._exclude_spec = None
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

    def test_defensive_filter_skips_non_media_ext(self):
        """防御性过滤：扩展名不在白名单则跳过 STRM 生成。"""
        gen, plugin = self._make_plugin("/media/movies", "/strm/movies")
        plugin._rmt_mediaext = ["mkv"]  # 只允许 mkv
        ok, path, created = gen.generate("/media/movies/Foo.mp4", "/cloud/Foo.mp4")
        self.assertFalse(ok)
        self.assertIsNone(path)

    def test_defensive_filter_skips_excluded(self):
        """防御性过滤：命中排除规则则跳过 STRM 生成。"""
        import pathspec
        gen, plugin = self._make_plugin("/media/movies", "/strm/movies")
        plugin._rmt_mediaext = []
        plugin._exclude_spec = pathspec.PathSpec.from_lines(
            pathspec.patterns.GitWildMatchPattern, ["sample/**"])
        ok, path, created = gen.generate("/media/movies/sample/x.mkv", "/cloud/sample/x.mkv")
        self.assertFalse(ok)
        self.assertIsNone(path)

    def test_defensive_filter_allows_media(self):
        """防御性过滤：白名单内且未排除则正常生成。"""
        import pathspec
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            local_root = Path(tmp) / "media"
            strm_root = Path(tmp) / "strm"
            local_root.mkdir()
            strm_root.mkdir()
            media_file = local_root / "Foo.mkv"
            media_file.write_bytes(b"movie")
            gen, plugin = self._make_plugin(str(local_root), str(strm_root))
            plugin._rmt_mediaext = ["mkv", "mp4"]
            plugin._exclude_spec = pathspec.PathSpec.from_lines(
                pathspec.patterns.GitWildMatchPattern, ["*.tmp"])
            ok, path, created = gen.generate(str(media_file), "/cloud/Foo.mkv")
            self.assertTrue(ok)
            self.assertTrue(created)

    def test_overwrite_always_rewrites_existing_strm(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            local_root = Path(tmp) / "media"
            strm_root = Path(tmp) / "strm"
            local_root.mkdir()
            strm_root.mkdir()
            media_file = local_root / "Foo.mkv"
            media_file.write_bytes(b"movie")
            strm_file = strm_root / "Foo.strm"
            strm_file.write_text("old", encoding="utf-8")

            gen, plugin = self._make_plugin(str(local_root), str(strm_root))
            plugin._overwrite_mode = "always"
            ok, path, created = gen.generate(str(media_file), "/cloud/Foo.mkv")

            self.assertTrue(ok)
            self.assertEqual(path, strm_file)
            self.assertFalse(created)
            self.assertNotEqual(strm_file.read_text(encoding="utf-8"), "old")


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

    def test_file_list_accepts_absolute_path_and_fileitem(self):
        from cloudstrmhelper import CloudStrmHelper
        from cloudstrmhelper.transfer_listener import TransferListener

        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._local_media_path = "/media/movies"
        plugin._local_media_roots = ["/media/movies"]
        plugin._alist_target_path = "/cloud"
        plugin._upload_path_mappings = "/media/movies#/cloud"
        plugin._upload_mappings = [("/media/movies", "/cloud")]
        plugin._event_filter_prefixes = []
        plugin._exclude_spec = None
        plugin._rmt_mediaext = ["mkv"]

        listener = TransferListener(plugin)
        records = listener._records_from_file_list(
            source="event",
            target_dir="/media/movies/Target",
            file_list=[
                "/media/movies/Foo.mkv",
                types.SimpleNamespace(path="/media/movies/Bar.mkv"),
                "Relative.mkv",
            ],
            mediainfo=None,
            meta=None,
        )

        self.assertEqual(
            [record.local_path for record in records],
            [
                "/media/movies/Foo.mkv",
                "/media/movies/Bar.mkv",
                "/media/movies/Target/Relative.mkv",
            ],
        )
        self.assertEqual(
            [record.remote_path for record in records],
            [
                "/cloud/Foo.mkv",
                "/cloud/Bar.mkv",
                "/cloud/Target/Relative.mkv",
            ],
        )


class TestSseListenerAuth(unittest.TestCase):
    class _Resp:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self, decode_unicode=True):
            return []

    def test_sse_retries_bearer_auth_after_403(self):
        from cloudstrmhelper.sse_listener import MoviePilotSseListener

        plugin = MagicMock()
        plugin._moviepilot_address = "http://mp:3000"
        listener = MoviePilotSseListener(plugin)
        listener._consume_lines = MagicMock()

        with patch("cloudstrmhelper.sse_listener.requests.get") as get:
            get.side_effect = [self._Resp(403), self._Resp(200)]
            listener._listen_once()

        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "test-token-123")
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer test-token-123")
        listener._consume_lines.assert_called_once()

    def test_sse_auth_error_stops_run_loop(self):
        from cloudstrmhelper.sse_listener import MoviePilotSseListener

        plugin = MagicMock()
        plugin._moviepilot_address = "http://mp:3000"
        listener = MoviePilotSseListener(plugin)

        with patch("cloudstrmhelper.sse_listener.requests.get", return_value=self._Resp(403)) as get:
            listener._run()

        self.assertEqual(get.call_count, 4)


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

    def test_raw_url_only_rejects_d_fallback(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.5:5244"
        ph = ProxyHandler(alist, follow_redirects=False, direct_link_mode="raw_url_only")
        with self.assertRaises(Exception):
            ph._build_url({"sign": "mysign"}, "/foo.mkv")

    def test_alist_download_mode_ignores_raw_url(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.5:5244"
        ph = ProxyHandler(alist, follow_redirects=False, direct_link_mode="alist_download")
        url = ph._build_url({"raw_url": "http://cdn/foo?sig=x", "sign": "mysign"}, "/foo.mkv")
        self.assertEqual(url, "http://192.168.31.5:5244/d/foo.mkv?sign=mysign")


class TestPluginMetadata(unittest.TestCase):
    def test_metadata_present(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertEqual(CloudStrmHelper.plugin_name, "云端STRM整理助手")
        self.assertEqual(CloudStrmHelper.plugin_version, "1.5.5")
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
        self.assertIn("upload_path_mappings", defaults)
        self.assertIn("strm_path_mappings", defaults)
        self.assertIn("local_strm_paths", defaults)
        self.assertIn("strm_output_path", defaults)
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["cloud_storage_type"], "alist")
        self.assertEqual(defaults["moviepilot_address"], "http://192.168.31.6:3000")
        self.assertEqual(defaults["alist_url"], "http://192.168.31.6:5244/")
        self.assertEqual(defaults["alist_target_path"], "/123云盘/影视/华语电影")
        self.assertIn("/media/movies#/123云盘/影视/华语电影", defaults["upload_path_mappings"])
        self.assertIn("/123云盘/影视/华语电影#/strm/test/华语电影", defaults["strm_path_mappings"])
        self.assertIn("/media/movies", defaults["local_media_path"])
        self.assertIn("/media/tv", defaults["local_media_path"])
        self.assertIn("/media/movies#/strm/test/华语电影", defaults["local_strm_paths"])
        self.assertIn("/media/tv#/strm/test/电视剧", defaults["local_strm_paths"])
        self.assertEqual(defaults["strm_output_path"], "/strm/test/华语电影")
        self.assertEqual(defaults["sync_mode"], "copy")
        self.assertEqual(defaults["strm_url_mode"], "alist_direct")
        self.assertEqual(defaults["direct_link_mode"], "prefer_raw_url")
        self.assertFalse(defaults["sse_enabled"])
        self.assertFalse(defaults["emby_proxy_enabled"])
        self.assertEqual(defaults["emby_server_url"], "http://192.168.31.6:8096")
        self.assertEqual(defaults["emby_proxy_host"], "0.0.0.0")
        self.assertEqual(defaults["emby_proxy_port"], 8095)
        self.assertEqual(defaults["manual_upload_action"], "none")
        self.assertFalse(defaults["manual_execute"])
        # v1.5.3: 配置页拆 4 Tab，_tabs 默认指向第一个 Tab；手动处理卡片已移除（字段保留兼容旧配置）
        # v1.5.6: 去掉外层 VCard，form[0]=VTabs, form[1]=VWindow
        self.assertEqual(defaults["_tabs"], "base")
        self.assertEqual(form[0]["component"], "VTabs")
        vwindow = next(c for c in form if c["component"] == "VWindow")
        tab_values = [it["props"]["value"] for it in vwindow["content"]]
        self.assertEqual(tab_values, ["base", "play", "cloud", "sync"])
        # 确认手动处理相关标题已从配置页移除（遍历所有 div.text-h6 标题）
        def _section_titles(node):
            titles = []
            if isinstance(node, dict):
                if node.get("component") == "div" and "text-h6" in str(node.get("props", {}).get("class", "")):
                    titles.append(node.get("text", ""))
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        titles.extend(_section_titles(v))
            elif isinstance(node, list):
                for v in node:
                    titles.extend(_section_titles(v))
            return titles
        all_titles = [t for it in vwindow["content"] for t in _section_titles(it)]
        self.assertNotIn("手动处理", all_titles)
        # 配置页 Tab 内不应有 VCard 边框容器
        def _has_vcard(node):
            if isinstance(node, dict):
                if node.get("component") == "VCard":
                    return True
                for v in node.values():
                    if isinstance(v, (list, dict)) and _has_vcard(v):
                        return True
            elif isinstance(node, list):
                for v in node:
                    if _has_vcard(v):
                        return True
            return False
        for it in vwindow["content"]:
            self.assertFalse(_has_vcard(it), f"Tab {it['props']['value']} 内仍有 VCard")

    def test_api_endpoints(self):
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        paths = [a["path"] for a in apis]
        self.assertIn("/redirect", paths)
        self.assertIn("/status", paths)
        self.assertIn("/diagnose", paths)
        self.assertIn("/sync_now", paths)
        self.assertIn("/manual_action", paths)

    def test_manual_action_endpoint_registered_with_apikey(self):
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        manual = next((a for a in apis if a["path"] == "/manual_action"), None)
        self.assertIsNotNone(manual, "/manual_action 端点未注册")
        self.assertEqual(manual["auth"], "apikey")
        self.assertIn("POST", manual["methods"])

    def test_clear_upload_history_endpoint_registered(self):
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        ep = next((a for a in apis if a["path"] == "/clear_upload_history"), None)
        self.assertIsNotNone(ep, "/clear_upload_history 端点未注册")
        self.assertEqual(ep["auth"], "apikey")
        self.assertIn("POST", ep["methods"])
        self.assertIn("仅清除", ep["description"])

    def test_clear_strm_history_endpoint_registered(self):
        from cloudstrmhelper import CloudStrmHelper
        apis = CloudStrmHelper.get_api(CloudStrmHelper)
        ep = next((a for a in apis if a["path"] == "/clear_strm_history"), None)
        self.assertIsNotNone(ep, "/clear_strm_history 端点未注册")
        self.assertEqual(ep["auth"], "apikey")
        self.assertIn("POST", ep["methods"])
        self.assertIn("仅清除", ep["description"])

    def test_clear_upload_history_resets_stats(self):
        from cloudstrmhelper import CloudStrmHelper, JSONResponse
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._stats = {
            "recent_uploads": [{"name": "a.mkv", "remote": "/cloud/a.mkv", "status": "uploaded"}],
            "upload_count": 5,
            "last_upload_time": "2025-01-01 12:00:00",
            "recent_strms": [{"name": "a.strm", "path": "/strm/a.strm"}],
            "strm_count": 3,
            "last_strm_time": "2025-01-01 12:00:00",
        }
        calls = []

        def fake_json(body, **kw):
            calls.append((body, kw.get("status_code", 200)))
            return body

        with patch("cloudstrmhelper.JSONResponse", fake_json):
            plugin.clear_upload_history()
        body, status = calls[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body["state"])
        self.assertEqual(plugin._stats["recent_uploads"], [])
        self.assertEqual(plugin._stats["upload_count"], 0)
        self.assertEqual(plugin._stats["last_upload_time"], "")
        # STRM 相关不应受影响
        self.assertEqual(len(plugin._stats["recent_strms"]), 1)
        self.assertEqual(plugin._stats["strm_count"], 3)

    def test_clear_strm_history_resets_stats(self):
        from cloudstrmhelper import CloudStrmHelper, JSONResponse
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._stats = {
            "recent_uploads": [{"name": "a.mkv", "remote": "/cloud/a.mkv", "status": "uploaded"}],
            "upload_count": 5,
            "last_upload_time": "2025-01-01 12:00:00",
            "recent_strms": [{"name": "a.strm", "path": "/strm/a.strm"}],
            "strm_count": 3,
            "last_strm_time": "2025-01-01 12:00:00",
        }
        calls = []

        def fake_json(body, **kw):
            calls.append((body, kw.get("status_code", 200)))
            return body

        with patch("cloudstrmhelper.JSONResponse", fake_json):
            plugin.clear_strm_history()
        body, status = calls[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body["state"])
        self.assertEqual(plugin._stats["recent_strms"], [])
        self.assertEqual(plugin._stats["strm_count"], 0)
        self.assertEqual(plugin._stats["last_strm_time"], "")
        # 上传相关不应受影响
        self.assertEqual(len(plugin._stats["recent_uploads"]), 1)
        self.assertEqual(plugin._stats["upload_count"], 5)

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

    def test_diagnostic_snapshot_is_sanitized(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._moviepilot_address = "http://mp:3000"
        plugin._cloud_storage_type = "alist"
        plugin._alist_url = "http://alist:5244/"
        plugin._alist_token = "secret-token-value"
        plugin._alist_target_path = "/cloud"
        plugin._local_strm_paths = "/media/movies#/strm/movies\n/media/tv#/strm/tv"
        plugin._local_strm_mappings = [("/media/movies", "/strm/movies"), ("/media/tv", "/strm/tv")]
        plugin._local_media_path = "/media/movies\n/media/tv"
        plugin._local_media_roots = ["/media/movies", "/media/tv"]
        plugin._strm_output_path = "/strm/movies\n/strm/tv"
        plugin._sync_mode = "copy"
        plugin._overwrite_mode = "never"
        plugin._upload_concurrency = 3
        plugin._rmt_mediaext = ["mkv", "mp4"]
        plugin._event_filter_prefixes = ["/media/movies"]
        plugin._refresh_enabled = True
        plugin._mediaservers = []
        plugin._transfer_mp_mediaserver_paths = "/media#/data"
        plugin._sse_listener = None
        plugin._alist_client = None
        plugin._cloud_sync = None
        plugin._strm_gen = None
        plugin._proxy = None
        plugin._strm_url_mode = "moviepilot_redirect"
        plugin._resolve_final_url = True
        plugin._direct_link_mode = "prefer_raw_url"
        plugin._redirect_cache_ttl = 120
        plugin._head_probe_mode = "ok"
        plugin._sse_enabled = False
        plugin._emby_proxy_enabled = True
        plugin._emby_server_url = "http://emby:8096"
        plugin._emby_proxy_host = "0.0.0.0"
        plugin._emby_proxy_port = 8095
        plugin._emby_proxy_server = object()
        plugin._stats = {"strm_count": 2, "last_strm_time": "2026-01-01 00:00:00", "recent_files": []}

        data = plugin._diagnostic_snapshot(probe=False)
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("secret-token-value", serialized)
        self.assertEqual(data["config"]["alist_token"], "secr...alue")
        self.assertEqual(data["mapping_sample"]["remote"], "/cloud/example.mkv")
        self.assertEqual(data["mapping_sample"]["strm"], "/strm/movies/example.strm")
        self.assertEqual(data["redirect"]["direct_link_mode"], "prefer_raw_url")
        self.assertTrue(data["redirect"]["emby_proxy_enabled"])
        self.assertTrue(data["redirect"]["emby_proxy_running"])
        self.assertEqual(data["redirect"]["emby_proxy_listen"], "0.0.0.0:8095")
        self.assertEqual(data["phase_order"], ["listen", "sync", "strm", "refresh"])


class TestPathComputation(unittest.TestCase):
    """测试本地/云端/STRM 路径映射逻辑。"""

    def _make_plugin(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._local_media_path = "/media/movies\n/media/tv"
        plugin._local_media_roots = ["/media/movies", "/media/tv"]
        plugin._local_strm_paths = "/media/movies#/strm/movies\n/media/tv#/strm/tv"
        plugin._local_strm_mappings = [("/media/movies", "/strm/movies"), ("/media/tv", "/strm/tv")]
        plugin._alist_target_path = "/123云盘/影视/华语电影"
        plugin._strm_output_path = "/strm/movies\n/strm/tv"
        plugin._rmt_mediaext = ["mp4", "mkv"]
        return plugin

    def test_remote_path_from_multiple_local_roots(self):
        plugin = self._make_plugin()
        self.assertEqual(
            plugin._build_remote_path("/media/movies/Foo (2024)/Foo (2024).mkv"),
            "/123云盘/影视/华语电影/Foo (2024)/Foo (2024).mkv",
        )
        self.assertEqual(
            plugin._build_remote_path("/media/tv/Show/Season 01/S01E01.mkv"),
            "/123云盘/影视/华语电影/Show/Season 01/S01E01.mkv",
        )

    def test_upload_path_mapping_custom_cloud_root(self):
        plugin = self._make_plugin()
        plugin._upload_path_mappings = "/media/华语电影#/123云盘/影视/华语电影"
        plugin._upload_mappings = [("/media/华语电影", "/123云盘/影视/华语电影")]
        self.assertEqual(
            plugin._build_remote_path("/media/华语电影/流浪地球.mkv"),
            "/123云盘/影视/华语电影/流浪地球.mkv",
        )

    def test_strm_path_mapping_custom_cloud_to_local(self):
        plugin = self._make_plugin()
        plugin._strm_path_mappings = "/123云盘/影视/华语电影#/strm/华语电影"
        plugin._strm_mappings = [("/123云盘/影视/华语电影", "/strm/华语电影")]
        plugin._strm_path_mappings_explicit = True
        self.assertEqual(
            plugin._strm_output_path_from_remote("/123云盘/影视/华语电影/流浪地球.mkv"),
            Path("/strm/华语电影/流浪地球.strm"),
        )

    def test_explicit_strm_mapping_overrides_legacy_local_mapping(self):
        plugin = self._make_plugin()
        plugin._strm_path_mappings = "/123云盘/影视/电视剧#/strm/custom-tv"
        plugin._strm_mappings = [("/123云盘/影视/电视剧", "/strm/custom-tv")]
        plugin._strm_path_mappings_explicit = True
        self.assertEqual(
            plugin._strm_output_path_for(
                "/media/tv/Show/Season 01/S01E01.mkv",
                "/123云盘/影视/电视剧/Show/Season 01/S01E01.mkv",
            ),
            Path("/strm/custom-tv/Show/Season 01/S01E01.strm"),
        )

    def test_strm_path_from_remote_path(self):
        plugin = self._make_plugin()
        self.assertEqual(
            plugin._strm_output_path_from_remote("/123云盘/影视/华语电影/movie.mp4"),
            Path("/strm/movies/movie.strm"),
        )
        self.assertEqual(
            plugin._strm_output_path_from_remote("/123云盘/影视/华语电影/Show/S01E01.mkv"),
            Path("/strm/movies/Show/S01E01.strm"),
        )

    def test_strm_path_uses_matching_local_mapping(self):
        plugin = self._make_plugin()
        self.assertEqual(
            plugin._strm_output_path_for(
                "/media/tv/Show/Season 01/S01E01.mkv",
                "/123云盘/影视/华语电影/Show/Season 01/S01E01.mkv",
            ),
            Path("/strm/tv/Show/Season 01/S01E01.strm"),
        )

    def test_expand_sse_directory_record_in_phase2(self):
        from cloudstrmhelper import CloudStrmHelper
        from cloudstrmhelper.transfer_listener import TransferRecord

        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            root = Path(tmp) / "media"
            root.mkdir()
            (root / "Foo.mkv").write_bytes(b"movie")
            (root / "Bar.mp4").write_bytes(b"movie")
            (root / "readme.txt").write_text("skip", encoding="utf-8")
            (root / "skip.tmp").write_bytes(b"skip")
            (root / "sample").mkdir()
            (root / "sample" / "Sample.mkv").write_bytes(b"skip")

            plugin = CloudStrmHelper.__new__(CloudStrmHelper)
            plugin._local_strm_paths = f"{root}#/strm"
            plugin._local_strm_mappings = [(str(root), "/strm")]
            plugin._local_media_path = str(root)
            plugin._local_media_roots = [str(root)]
            plugin._alist_target_path = "/cloud"
            plugin._strm_output_path = "/strm"
            plugin._rmt_mediaext = ["mp4", "mkv"]
            plugin._exclude_spec = CloudStrmHelper._build_exclude_spec("*.tmp\n/sample/**")

            record = TransferRecord(source="sse", local_path=str(root), remote_path="/cloud")
            files = plugin._expand_record_media_files(record)

        remote_paths = sorted(item[1] for item in files)
        self.assertEqual(remote_paths, ["/cloud/Bar.mp4", "/cloud/Foo.mkv"])

    def test_move_mode_removes_local_file_after_success(self):
        from cloudstrmhelper import CloudStrmHelper
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            media_file = Path(tmp) / "Foo.mkv"
            media_file.write_bytes(b"movie")
            plugin = CloudStrmHelper.__new__(CloudStrmHelper)
            plugin._sync_mode = "move"
            plugin._cleanup_local_after_move(str(media_file))
            self.assertFalse(media_file.exists())

    def test_copy_mode_keeps_local_file_after_success(self):
        from cloudstrmhelper import CloudStrmHelper
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            media_file = Path(tmp) / "Foo.mkv"
            media_file.write_bytes(b"movie")
            plugin = CloudStrmHelper.__new__(CloudStrmHelper)
            plugin._sync_mode = "copy"
            plugin._cleanup_local_after_move(str(media_file))
            self.assertTrue(media_file.exists())

    def test_manual_reupload_validation_uses_upload_mapping(self):
        from cloudstrmhelper import CloudStrmHelper
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            root = Path(tmp) / "media"
            root.mkdir()
            media_file = root / "Foo Bar.mkv"
            media_file.write_bytes(b"movie")

            plugin = CloudStrmHelper.__new__(CloudStrmHelper)
            plugin._upload_mappings = [(str(root), "/cloud/movies")]
            plugin._upload_path_mappings = f"{root}#/cloud/movies"
            local_path, remote_path = plugin._validate_reupload_paths(
                str(media_file),
                "/cloud/movies/Foo+Bar.mkv",
            )

        self.assertEqual(local_path, str(media_file))
        self.assertEqual(remote_path, "/cloud/movies/Foo Bar.mkv")

    def test_manual_delete_remote_does_not_require_existing_local_file(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._upload_mappings = [("/missing/media", "/cloud/movies")]
        plugin._strm_mappings = []
        plugin._alist_target_path = "/cloud"

        local_path, remote_path = plugin._decode_manual_delete_target(
            json.dumps({
                "local": "/missing/media/Foo.mkv",
                "remote": "/cloud/movies/Foo.mkv",
            })
        )

        self.assertEqual(local_path, "/missing/media/Foo.mkv")
        self.assertEqual(remote_path, "/cloud/movies/Foo.mkv")
        with self.assertRaises(Exception):
            plugin._decode_manual_delete_target(
                json.dumps({
                    "local": "/missing/media/Foo.mkv",
                    "remote": "/cloud/other/Foo.mkv",
                }),
                require_local=True,
            )

    def _manual_action_plugin(self):
        from cloudstrmhelper import CloudStrmHelper, ManualActionParams
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._upload_mappings = [("/media/movies", "/cloud/movies")]
        plugin._strm_mappings = []
        plugin._local_strm_mappings = []
        plugin._alist_target_path = "/cloud"
        plugin._strm_gen = None
        return plugin

    @staticmethod
    def _params(**kw):
        from cloudstrmhelper import ManualActionParams
        return ManualActionParams.model_validate(kw)

    def test_manual_action_rejects_unknown_action(self):
        plugin = self._manual_action_plugin()
        calls = []

        def fake_json(body, **kw):
            calls.append((body, kw.get("status_code", 200)))
            return body

        with patch("cloudstrmhelper.JSONResponse", fake_json):
            resp = plugin.manual_action(self._params(action="bogus", remote="/cloud/movies/x.mkv"))
        body, status = calls[-1]
        self.assertEqual(status, 400)
        self.assertFalse(body["state"])

    def test_manual_action_rejects_missing_remote(self):
        plugin = self._manual_action_plugin()
        calls = []
        with patch("cloudstrmhelper.JSONResponse", lambda b, **kw: calls.append((b, kw.get("status_code", 200))) or b):
            plugin.manual_action(self._params(action="delete_remote"))
        body, status = calls[-1]
        self.assertEqual(status, 400)
        self.assertFalse(body["state"])

    def test_manual_action_rejects_remote_outside_known_roots(self):
        plugin = self._manual_action_plugin()
        calls = []
        with patch("cloudstrmhelper.JSONResponse", lambda b, **kw: calls.append((b, kw.get("status_code", 200))) or b):
            # remote 不在任何已配置云端根下 → 校验失败 400（不启动后台线程）
            plugin.manual_action(self._params(action="delete_remote", remote="/other/x.mkv"))
        body, status = calls[-1]
        self.assertEqual(status, 400)
        self.assertFalse(body["state"])

    def test_manual_action_accepts_valid_regenerate_strm(self):
        """校验通过时返回 state=True 且启动后台线程（线程被 mock，不真跑 worker）。"""
        plugin = self._manual_action_plugin()
        plugin._strm_mappings = [("/cloud/movies", "/strm/movies")]
        calls = []
        started = {"called": False}

        class _FakeThread:
            def __init__(self, *a, **k): pass
            def start(self): started["called"] = True

        def fake_json(body, **kw):
            calls.append((body, kw.get("status_code", 200)))
            return body

        with patch("cloudstrmhelper.JSONResponse", fake_json), \
             patch("cloudstrmhelper.threading.Thread", _FakeThread):
            resp = plugin.manual_action(self._params(
                action="regenerate_strm",
                strm="/strm/movies/Foo.strm",
                remote="/cloud/movies/Foo.mkv",
            ))
        body, status = calls[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body["state"])
        self.assertTrue(started["called"], "校验通过应启动后台线程")


class TestEmby302Proxy(unittest.TestCase):
    def _make_plugin(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._upload_path_mappings = "/media/movies#/cloud/movies"
        plugin._upload_mappings = [("/media/movies", "/cloud/movies")]
        plugin._strm_path_mappings = "/cloud/movies#/strm/movies"
        plugin._strm_mappings = [("/cloud/movies", "/strm/movies")]
        plugin._alist_target_path = "/cloud"
        return plugin

    def test_qmediasync_route_patterns(self):
        from cloudstrmhelper.emby_proxy import Emby302Proxy
        self.assertTrue(Emby302Proxy.is_playback_info_request("/emby/Items/123/PlaybackInfo"))
        self.assertTrue(Emby302Proxy.is_openlist_redirect_request("/emby/videos/123/stream.mkv"))
        self.assertTrue(Emby302Proxy.is_openlist_redirect_request("/emby/audio/abc/universal"))
        self.assertFalse(Emby302Proxy.is_openlist_redirect_request("/emby/videos/123/subtitles/1/stream.vtt"))
        self.assertEqual(Emby302Proxy.item_id_from_path("/emby/videos/123/main.m3u8"), "123")
        self.assertEqual(Emby302Proxy.item_id_from_path("/emby/items/456/download"), "456")

    def test_local_media_path_maps_to_cloud_path(self):
        from cloudstrmhelper.emby_proxy import Emby302Proxy
        plugin = self._make_plugin()
        proxy = Emby302Proxy(plugin, "http://emby:8096")
        self.assertEqual(
            proxy.remote_path_from_media_path("/media/movies/Foo/Foo.mkv"),
            "/cloud/movies/Foo/Foo.mkv",
        )
        self.assertEqual(
            proxy.remote_path_from_media_path("/cloud/movies/Foo/Foo.mkv"),
            "/cloud/movies/Foo/Foo.mkv",
        )

    def test_patch_playback_info_to_direct_stream(self):
        from cloudstrmhelper.emby_proxy import Emby302Proxy
        plugin = self._make_plugin()
        proxy = Emby302Proxy(plugin, "http://emby:8096")
        data = {
            "MediaSources": [{
                "Id": "ms1",
                "Path": "/media/movies/Foo.mkv",
                "SupportsTranscoding": True,
                "TranscodingUrl": "/videos/123/master.m3u8",
            }]
        }

        patched, count = proxy.patch_playback_info(data, item_id="123", api_key="token")

        source = patched["MediaSources"][0]
        self.assertEqual(count, 1)
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsDirectStream"])
        self.assertFalse(source["SupportsTranscoding"])
        self.assertEqual(
            source["DirectStreamUrl"],
            "/videos/123/stream?MediaSourceId=ms1&Static=true&api_key=token",
        )
        self.assertNotIn("TranscodingUrl", source)

    def test_resolve_local_strm_cloud_path_to_direct_link(self):
        from cloudstrmhelper.emby_proxy import Emby302Proxy
        from cloudstrmhelper.proxy_handler import DirectLink

        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            strm = Path(tmp) / "Foo.strm"
            strm.write_text("/cloud/movies/Foo.mkv\n", encoding="utf-8")

            plugin = types.SimpleNamespace()
            plugin._upload_mappings = []
            plugin._strm_mappings = [("/cloud/movies", "/strm/movies")]
            plugin._alist_target_path = "/cloud"
            plugin._resolve_final_url = True
            plugin._proxy = MagicMock()
            plugin._proxy.resolve_link.return_value = DirectLink(
                url="http://cdn.example.com/Foo.mkv?sig=1",
                source="raw_url",
            )

            proxy = Emby302Proxy(plugin, "http://emby:8096")
            link = proxy.resolve_media_path(str(strm), ua="Player")

        self.assertEqual(link.url, "http://cdn.example.com/Foo.mkv?sig=1")
        self.assertEqual(link.source, "raw_url")
        plugin._proxy.resolve_link.assert_called_once_with("/cloud/movies/Foo.mkv", "Player")


class TestCloudSyncPolicy(unittest.TestCase):
    def test_need_upload_skips_existing_even_size_differs(self):
        from cloudstrmhelper.cloud_sync import CloudSync
        alist = MagicMock()
        alist.list_dir.return_value = {"Foo.mkv": 123}
        sync = CloudSync(plugin=None, alist_client=alist)
        self.assertFalse(sync.need_upload("/media/movies/Foo.mkv", "/cloud/Foo.mkv"))

    def test_need_upload_when_missing(self):
        from cloudstrmhelper.cloud_sync import CloudSync
        alist = MagicMock()
        alist.list_dir.return_value = {}
        sync = CloudSync(plugin=None, alist_client=alist)
        self.assertTrue(sync.need_upload("/media/movies/Foo.mkv", "/cloud/Foo.mkv"))

    def test_alist_put_stream_never_overwrites(self):
        from unittest.mock import mock_open, patch
        from cloudstrmhelper.cloud_sync import AlistClient
        client = AlistClient.__new__(AlistClient)
        client.url = "http://alist"
        client.token = "token"
        client.timeout = (1, 1)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 200, "data": {}}
        with patch("os.path.getsize", return_value=5), \
                patch("builtins.open", mock_open(read_data=b"abc")), \
                patch("cloudstrmhelper.cloud_sync.requests.put", return_value=response) as put:
            client.put_stream("/local/Foo.mkv", "/cloud/Foo.mkv", as_task=False)
        headers = put.call_args.kwargs["headers"]
        self.assertEqual(headers["Overwrite"], "false")

    def test_alist_remove_file_uses_dir_and_name(self):
        from cloudstrmhelper.cloud_sync import AlistClient
        client = AlistClient.__new__(AlistClient)
        client.post = MagicMock(return_value={})
        ok = client.remove_file("/cloud/Foo Bar.mkv")
        self.assertTrue(ok)
        client.post.assert_called_once_with(
            "/api/fs/remove",
            data={"dir": "/cloud", "names": ["Foo Bar.mkv"]},
        )

    def test_alist_put_stream_existing_raises_specific_error(self):
        from unittest.mock import mock_open, patch
        from cloudstrmhelper.cloud_sync import AlistAlreadyExists, AlistClient
        client = AlistClient.__new__(AlistClient)
        client.url = "http://alist"
        client.token = "token"
        client.timeout = (1, 1)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 500, "message": "path already exists"}
        with patch("os.path.getsize", return_value=5), \
                patch("builtins.open", mock_open(read_data=b"abc")), \
                patch("cloudstrmhelper.cloud_sync.requests.put", return_value=response):
            with self.assertRaises(AlistAlreadyExists):
                client.put_stream("/local/Foo.mkv", "/cloud/Foo.mkv", as_task=False)

    def test_alist_put_stream_http_existing_raises_specific_error(self):
        from unittest.mock import mock_open, patch
        from cloudstrmhelper.cloud_sync import AlistAlreadyExists, AlistClient
        client = AlistClient.__new__(AlistClient)
        client.url = "http://alist"
        client.token = "token"
        client.timeout = (1, 1)
        response = MagicMock()
        response.status_code = 409
        response.text = "path already exists"
        with patch("os.path.getsize", return_value=5), \
                patch("builtins.open", mock_open(read_data=b"abc")), \
                patch("cloudstrmhelper.cloud_sync.requests.put", return_value=response):
            with self.assertRaises(AlistAlreadyExists):
                client.put_stream("/local/Foo.mkv", "/cloud/Foo.mkv", as_task=False)

    def test_existing_during_upload_is_successful_skip(self):
        from cloudstrmhelper.cloud_sync import AlistAlreadyExists, CloudSync, TASK_SKIPPED, _SyncItem
        alist = MagicMock()
        alist.put_stream.side_effect = AlistAlreadyExists("exists")
        sync = CloudSync(plugin=None, alist_client=alist)
        item = _SyncItem("/media/movies/Foo.mkv", "/cloud/Foo.mkv", 1)
        sync._do_upload(item)
        self.assertEqual(item.status, TASK_SKIPPED)

    def test_existing_from_upload_task_error_is_successful_skip(self):
        from unittest.mock import patch
        from cloudstrmhelper.cloud_sync import CloudSync, TASK_FAILED, TASK_SKIPPED, _SyncItem
        alist = MagicMock()
        alist.upload_task_info.return_value = {
            "state": TASK_FAILED,
            "progress": None,
            "error": "path already exists",
        }
        sync = CloudSync(plugin=None, alist_client=alist)
        item = _SyncItem("/media/movies/Foo.mkv", "/cloud/Foo.mkv", 1)
        item.alist_task_id = "task-1"
        with patch("cloudstrmhelper.cloud_sync.time.sleep", return_value=None):
            sync._poll_task(item)
        self.assertEqual(item.status, TASK_SKIPPED)
        alist.upload_task_delete.assert_called_once_with("task-1")


class TestStrmUrlMode(unittest.TestCase):
    """v1.3.0: STRM URL 双模式。"""

    def _make_gen(self, strm_url_mode="moviepilot_redirect", alist_url="http://192.168.31.6:5244/",
                  alist_client=None):
        from cloudstrmhelper.strm_generator import StrmGenerator
        plugin = MagicMock()
        plugin._moviepilot_address = "http://mp:3000"
        plugin._strm_url_mode = strm_url_mode
        plugin._alist_url = alist_url
        plugin._alist_client = alist_client
        plugin._resolve_final_url = False
        plugin._direct_link_mode = "prefer_raw_url"
        return StrmGenerator(plugin), plugin

    def test_moviepilot_redirect_mode(self):
        gen, _ = self._make_gen("moviepilot_redirect")
        url = gen._build_strm_url("/cloud/Foo.mkv")
        self.assertIn("/api/v1/plugin/", url)
        self.assertIn("/redirect", url)
        self.assertIn("apikey=", url)
        self.assertIn("path=", url)
        self.assertNotIn("/d/", url.split("?")[0].split("/redirect")[0])

    def test_alist_direct_mode_with_sign(self):
        alist = MagicMock()
        alist.fs_get.return_value = {"sign": "abc", "name": "Foo.mkv"}
        gen, _ = self._make_gen("alist_direct", alist_client=alist)
        url = gen._build_alist_direct_url("/cloud/Foo.mkv")
        self.assertTrue(url.startswith("http://192.168.31.6:5244/d/"))
        self.assertIn("/d/", url)
        self.assertNotIn("/redirect", url)
        self.assertIn("?sign=abc", url)

    def test_alist_direct_mode_no_sign_on_fs_get_failure(self):
        """fs_get 失败时仍生成 /d/ 地址（无 sign），不中断。"""
        alist = MagicMock()
        alist.fs_get.side_effect = Exception("boom")
        gen, _ = self._make_gen("alist_direct", alist_client=alist)
        url = gen._build_alist_direct_url("/cloud/Foo.mkv")
        self.assertTrue(url.startswith("http://192.168.31.6:5244/d/"))
        self.assertNotIn("sign=", url)

    def test_alist_direct_mode_empty_sign(self):
        alist = MagicMock()
        alist.fs_get.return_value = {"sign": "", "name": "Foo.mkv"}
        gen, _ = self._make_gen("alist_direct", alist_client=alist)
        url = gen._build_alist_direct_url("/cloud/Foo.mkv")
        self.assertNotIn("sign=", url)

    def test_alist_direct_does_not_write_raw_url(self):
        """实验模式不写 raw_url（可能过期）。"""
        alist = MagicMock()
        alist.fs_get.return_value = {"sign": "s", "raw_url": "http://upstream/expiring?token=x"}
        gen, _ = self._make_gen("alist_direct", alist_client=alist)
        url = gen._build_alist_direct_url("/cloud/Foo.mkv")
        self.assertNotIn("upstream", url)
        self.assertNotIn("raw_url", url)

    def test_cloud_raw_url_mode_uses_raw_url(self):
        alist = MagicMock()
        alist.fs_get.return_value = {"raw_url": "http://cdn.example.com/Foo.mkv?sig=abc", "sign": "s"}
        gen, _ = self._make_gen("cloud_raw_url", alist_client=alist)
        url = gen._build_strm_url("/cloud/Foo.mkv")
        self.assertEqual(url, "http://cdn.example.com/Foo.mkv?sig=abc")
        self.assertNotIn("/redirect", url)
        self.assertNotIn("/d/", url)

    def test_cloud_raw_url_mode_falls_back_to_d_with_sign(self):
        alist = MagicMock()
        alist.fs_get.return_value = {"sign": "abc"}
        gen, _ = self._make_gen("cloud_raw_url", alist_client=alist)
        url = gen._build_strm_url("/cloud/Foo.mkv")
        self.assertEqual(url, "http://192.168.31.6:5244/d/cloud/Foo.mkv?sign=abc")

    def test_cloud_raw_url_strict_mode_requires_raw_url(self):
        alist = MagicMock()
        alist.fs_get.return_value = {"sign": "abc"}
        gen, plugin = self._make_gen("cloud_raw_url", alist_client=alist)
        plugin._direct_link_mode = "raw_url_only"
        with self.assertRaises(Exception):
            gen._build_strm_url("/cloud/Foo.mkv")


class TestSafeUrlForLog(unittest.TestCase):
    """v1.3.0: 日志脱敏。"""
    def test_strips_query_values(self):
        from cloudstrmhelper.proxy_handler import _safe_url_for_log
        safe = _safe_url_for_log("http://h/p/d/foo.mkv?sign=secret&token=abc")
        self.assertIn("http://h/p/d/foo.mkv", safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("abc", safe)

    def test_no_query_unchanged(self):
        from cloudstrmhelper.proxy_handler import _safe_url_for_log
        self.assertEqual(_safe_url_for_log("http://h/p/foo.mkv"), "http://h/p/foo.mkv")


class TestProxyHandlerResolve(unittest.TestCase):
    """v1.3.0: 302 resolver 解析逻辑（不发起真实 HTTP）。"""

    def _make_ph(self, resolve_final_url=False):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.6:5244"
        return ProxyHandler(alist, resolve_final_url=resolve_final_url)

    def test_raw_url_preferred(self):
        ph = self._make_ph()
        ph.alist.fs_get.return_value = {"raw_url": "http://upstream/foo?token=abc", "name": "x.mkv"}
        url = ph.resolve("/x.mkv", resolve_final_url=False)
        self.assertEqual(url, "http://upstream/foo?token=abc")

    def test_resolve_link_keeps_source_and_expiry(self):
        ph = self._make_ph()
        ph.alist.fs_get.return_value = {
            "raw_url": "http://upstream/foo.mkv?Expires=4102444800&token=abc",
            "name": "x.mkv",
        }
        link = ph.resolve_link("/x.mkv", resolve_final_url=False)
        self.assertEqual(link.source, "raw_url")
        self.assertEqual(link.url, "http://upstream/foo.mkv?Expires=4102444800&token=abc")
        self.assertEqual(link.expires_at, 4102444800)

    def test_no_raw_url_uses_d_with_sign(self):
        ph = self._make_ph()
        ph.alist.fs_get.return_value = {"sign": "mysign", "name": "x.mkv"}
        url = ph.resolve("/媒体库/Foo.mkv", resolve_final_url=False)
        self.assertEqual(url, "http://192.168.31.6:5244/d/%E5%AA%92%E4%BD%93%E5%BA%93/Foo.mkv?sign=mysign")

    def test_fs_get_failure_falls_back_to_d_in_compat_mode(self):
        ph = self._make_ph()
        ph.alist.fs_get.side_effect = Exception("api path failed")
        url = ph.resolve("/123云盘/影视/外语电影/阿甘正传 (1994)/阿甘正传 (1994) - 4k.mkv",
                         resolve_final_url=False)
        self.assertEqual(
            url,
            "http://192.168.31.6:5244/d/123%E4%BA%91%E7%9B%98/%E5%BD%B1%E8%A7%86/%E5%A4%96%E8%AF%AD%E7%94%B5%E5%BD%B1/%E9%98%BF%E7%94%98%E6%AD%A3%E4%BC%A0%20%281994%29/%E9%98%BF%E7%94%98%E6%AD%A3%E4%BC%A0%20%281994%29%20-%204k.mkv",
        )

    def test_fs_get_failure_still_raises_in_raw_only_mode(self):
        from cloudstrmhelper.proxy_handler import ProxyHandler
        alist = MagicMock()
        alist.url = "http://192.168.31.6:5244"
        alist.fs_get.side_effect = Exception("api path failed")
        ph = ProxyHandler(alist, resolve_final_url=False, direct_link_mode="raw_url_only")
        with self.assertRaises(Exception):
            ph.resolve("/cloud/Foo.mkv", resolve_final_url=False)

    def test_resolve_final_url_failure_falls_back_to_origin(self):
        """resolve_final_url=True 但预解析失败时，回退 _build_url 的原始 URL，不抛异常。"""
        ph = self._make_ph(resolve_final_url=True)
        ph.alist.fs_get.return_value = {"raw_url": "http://upstream/foo?token=abc"}
        with patch("cloudstrmhelper.proxy_handler.requests.Session") as session_cls:
            session = MagicMock()
            session.__enter__.return_value = session
            session.__exit__.return_value = False
            session.request.side_effect = Exception("simulated")
            session_cls.return_value = session
            url = ph.resolve("/x.mkv")
        # 预解析失败回退原始 raw_url
        self.assertEqual(url, "http://upstream/foo?token=abc")

    def test_head_4xx_falls_back_to_get_range(self):
        """HEAD 返回 4xx/5xx 时不能当成最终地址，应回退 GET Range 取可播放 URL。"""
        ph = self._make_ph(resolve_final_url=True)
        ph.alist.fs_get.return_value = {"raw_url": "http://upstream/start?token=abc"}

        class _Resp:
            def __init__(self, status_code, url, headers=None):
                self.status_code = status_code
                self.url = url
                self.headers = headers or {}
                self.closed = False

            def close(self):
                self.closed = True

        with patch("cloudstrmhelper.proxy_handler.requests.Session") as session_cls:
            session = MagicMock()
            session.__enter__.return_value = session
            session.__exit__.return_value = False
            session.request.side_effect = [
                _Resp(403, "http://upstream/start?token=abc"),
                _Resp(206, "http://cdn.example.com/final?sig=xyz"),
            ]
            session_cls.return_value = session

            url = ph.resolve("/x.mkv", ua="Infuse/8")

        self.assertEqual(url, "http://cdn.example.com/final?sig=xyz")
        calls = session.request.call_args_list
        self.assertEqual(calls[0].args[0], "HEAD")
        self.assertEqual(calls[1].args[0], "GET")
        self.assertEqual(calls[1].kwargs["headers"]["Range"], "bytes=0-0")
        self.assertTrue(calls[1].kwargs["stream"])

    def test_is_dir_raises(self):
        ph = self._make_ph()
        ph.alist.fs_get.return_value = {"is_dir": True}
        with self.assertRaises(Exception):
            ph.resolve("/some/dir", resolve_final_url=False)


class TestRedirectEndpointCaching(unittest.TestCase):
    """v1.3.0: redirect 端点缓存命中/未命中 + 负缓存 + HEAD 策略。"""

    def _make_plugin(self):
        from cloudstrmhelper import CloudStrmHelper
        from cloudstrmhelper.proxy_handler import DirectLink
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._strm_url_mode = "moviepilot_redirect"
        plugin._resolve_final_url = False
        plugin._direct_link_mode = "prefer_raw_url"
        plugin._redirect_cache_ttl = 120
        plugin._head_probe_mode = "ok"
        from cachetools import TTLCache
        plugin._redirect_cache = TTLCache(maxsize=512, ttl=120)
        plugin._redirect_error_cache = TTLCache(maxsize=256, ttl=30)
        plugin._resolve_locks = {}
        plugin._resolve_locks_guard = __import__("threading").Lock()
        plugin._proxy = MagicMock()
        plugin._proxy.resolve_link.return_value = DirectLink(url="http://resolved/foo", source="raw_url")
        return plugin

    def test_cache_key_contains_ua_hash_and_mode(self):
        plugin = self._make_plugin()
        k1 = plugin._redirect_cache_key("/p", "VLC/1")
        k2 = plugin._redirect_cache_key("/p", "VLC/2")
        k3 = plugin._redirect_cache_key("/p", "VLC/1")
        self.assertEqual(len(k1), 4)
        self.assertEqual(k1, k3)  # 同 UA 同 path 同 key
        self.assertNotEqual(k1, k2)  # 不同 UA 不同 key
        self.assertEqual(k1[0], "/p")
        self.assertEqual(k1[2], "origin")  # resolve_final_url=False
        self.assertEqual(k1[3], "prefer_raw_url")

    def test_normalize_remote_path_arg_decodes_chinese_url_path(self):
        from cloudstrmhelper import CloudStrmHelper
        encoded = (
            "/123%E4%BA%91%E7%9B%98/%E5%BD%B1%E8%A7%86/%E5%A4%96%E8%AF%AD%E7%94%B5%E5%BD%B1/"
            "%E9%98%BF%E7%94%98%E6%AD%A3%E4%BC%A0+%281994%29/"
            "%E9%98%BF%E7%94%98%E6%AD%A3%E4%BC%A0+%281994%29+-+4k.mkv"
        )
        self.assertEqual(
            CloudStrmHelper._normalize_remote_path_arg(encoded),
            "/123云盘/影视/外语电影/阿甘正传 (1994)/阿甘正传 (1994) - 4k.mkv",
        )

    def test_cached_resolve_hits_cache(self):
        from cloudstrmhelper.proxy_handler import DirectLink
        plugin = self._make_plugin()
        key = plugin._redirect_cache_key("/p", "ua")
        plugin._redirect_cache[key] = DirectLink(url="http://cached/foo", source="raw_url")
        link = plugin._cached_resolve(key, "/p", "ua")
        self.assertEqual(link.url, "http://cached/foo")
        plugin._proxy.resolve_link.assert_not_called()

    def test_cached_resolve_miss_calls_proxy(self):
        plugin = self._make_plugin()
        key = plugin._redirect_cache_key("/p", "ua")
        link = plugin._cached_resolve(key, "/p", "ua")
        self.assertEqual(link.url, "http://resolved/foo")
        self.assertEqual(link.source, "raw_url")
        plugin._proxy.resolve_link.assert_called_once()
        # 写入缓存
        self.assertEqual(plugin._redirect_cache.get(key).url, "http://resolved/foo")

    def test_cached_resolve_double_check_under_lock(self):
        """in-flight 合并：锁内再次查缓存，避免并发重复调用。"""
        from cloudstrmhelper.proxy_handler import DirectLink
        plugin = self._make_plugin()
        key = plugin._redirect_cache_key("/p", "ua")
        # 预置缓存，模拟另一个线程刚写
        plugin._redirect_cache[key] = DirectLink(url="http://just-cached", source="raw_url")
        link = plugin._cached_resolve(key, "/p", "ua")
        self.assertEqual(link.url, "http://just-cached")
        plugin._proxy.resolve_link.assert_not_called()

    def test_cached_resolve_drops_expiring_direct_link(self):
        from cloudstrmhelper.proxy_handler import DirectLink
        plugin = self._make_plugin()
        key = plugin._redirect_cache_key("/p", "ua")
        plugin._redirect_cache[key] = DirectLink(
            url="http://expired/foo?Expires=1",
            source="raw_url",
            expires_at=1,
        )
        link = plugin._cached_resolve(key, "/p", "ua")
        self.assertEqual(link.url, "http://resolved/foo")
        plugin._proxy.resolve_link.assert_called_once()

    def test_redirect_headers_expose_source_without_url(self):
        from cloudstrmhelper.proxy_handler import DirectLink
        plugin = self._make_plugin()
        resp = MagicMock()
        resp.headers = {}
        plugin._set_redirect_headers(
            resp,
            DirectLink(url="http://cdn/foo?token=secret", source="raw_url", resolved_final=False),
        )
        self.assertEqual(resp.headers["X-CloudStrm-Link-Source"], "raw_url")
        self.assertEqual(resp.headers["X-CloudStrm-Direct-Link"], "1")
        serialized = json.dumps(resp.headers)
        self.assertNotIn("secret", serialized)


class TestStatsMigration(unittest.TestCase):
    """v1.3.0: stats 旧结构迁移。"""

    def test_migrate_old_stats(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        old = {"strm_count": 1, "last_strm_time": "2026-01-01 00:00:00",
               "recent_files": [{"name": "a.strm", "time": "2026-01-01 00:00:00"}]}
        plugin.get_data = lambda key=None, plugin_id=None: old if key == "stats" else None
        plugin.save_data = lambda key, value, plugin_id=None: None
        stats = plugin._load_stats()
        self.assertEqual(stats["upload_count"], 0)
        self.assertEqual(stats["last_upload_time"], "")
        self.assertEqual(stats["recent_uploads"], [])
        self.assertEqual(stats["strm_count"], 1)
        self.assertEqual(stats["last_strm_time"], "2026-01-01 00:00:00")
        # recent_files 迁移到 recent_strms
        self.assertEqual(stats["recent_strms"][0]["name"], "a.strm")

    def test_new_stats_kept(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        new = {"upload_count": 5, "last_upload_time": "t", "recent_uploads": [],
               "strm_count": 9, "last_strm_time": "t2", "recent_strms": [{"name": "b.strm"}]}
        plugin.get_data = lambda key=None, plugin_id=None: new if key == "stats" else None
        plugin.save_data = lambda key, value, plugin_id=None: None
        stats = plugin._load_stats()
        self.assertEqual(stats["upload_count"], 5)
        self.assertEqual(stats["strm_count"], 9)
        self.assertEqual(stats["recent_strms"][0]["name"], "b.strm")


class TestUploadStrmStats(unittest.TestCase):
    """v1.3.0: 上传/STRM 统计计数与最近列表。"""

    def _make_plugin(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._stats = None
        plugin.get_data = lambda key=None, plugin_id=None: None
        saved = {}
        plugin.save_data = lambda key, value, plugin_id=None: saved.update({key: value})
        plugin._load_stats = lambda: {
            "upload_count": 0, "last_upload_time": "", "recent_uploads": [],
            "strm_count": 0, "last_strm_time": "", "recent_strms": [],
        }
        plugin._save_stats = lambda: __import__("cloudstrmhelper", fromlist=["_dummy"]) or None
        return plugin, saved

    def test_upload_uploaded_increments(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin, saved = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        plugin._record_upload_stat("/l/Foo.mkv", "/c/Foo.mkv", 100, status="uploaded")
        self.assertEqual(plugin._stats["upload_count"], 1)
        self.assertTrue(plugin._stats["last_upload_time"])
        self.assertEqual(plugin._stats["recent_uploads"][0]["status"], "uploaded")

    def test_upload_skipped_does_not_increment(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin, saved = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        plugin._record_upload_stat("/l/Foo.mkv", "/c/Foo.mkv", 100, status="skipped")
        self.assertEqual(plugin._stats["upload_count"], 0)
        self.assertEqual(plugin._stats["recent_uploads"][0]["status"], "skipped")

    def test_recent_uploads_max_20(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin, _ = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        for i in range(25):
            plugin._record_upload_stat(f"/l/{i}.mkv", f"/c/{i}.mkv", 1, status="uploaded")
        self.assertLessEqual(len(plugin._stats["recent_uploads"]), 20)

    def test_strm_created_increments(self):
        from cloudstrmhelper import CloudStrmHelper
        from pathlib import Path
        plugin, _ = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        plugin._record_strm_stat(
            Path("/strm/Foo.strm"),
            created=True,
            remote_path="/c/Foo.mkv",
            local_path="/l/Foo.mkv",
        )
        self.assertEqual(plugin._stats["strm_count"], 1)
        self.assertTrue(plugin._stats["recent_strms"][0]["created"])
        self.assertEqual(plugin._stats["recent_strms"][0]["local"], "/l/Foo.mkv")

    def test_strm_not_created_does_not_increment(self):
        from cloudstrmhelper import CloudStrmHelper
        from pathlib import Path
        plugin, _ = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        plugin._record_strm_stat(Path("/strm/Foo.strm"), created=False, remote_path="/c/Foo.mkv")
        self.assertEqual(plugin._stats["strm_count"], 0)
        self.assertFalse(plugin._stats["recent_strms"][0]["created"])

    def test_recent_strms_max_20(self):
        from cloudstrmhelper import CloudStrmHelper
        from pathlib import Path
        plugin, _ = self._make_plugin()
        plugin._stats = plugin._load_stats()
        plugin._save_stats = lambda: None
        for i in range(25):
            plugin._record_strm_stat(Path(f"/strm/{i}.strm"), created=True, remote_path=f"/c/{i}.mkv")
        self.assertLessEqual(len(plugin._stats["recent_strms"]), 20)


class TestNewConfigPersistence(unittest.TestCase):
    """v1.3.0: 4 个新配置项被读取/持久化/诊断输出。"""

    def test_update_config_includes_new_keys(self):
        from cloudstrmhelper import CloudStrmHelper
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._moviepilot_address = "http://mp:3000"
        plugin._cloud_storage_type = "alist"
        plugin._alist_url = "http://alist:5244/"
        plugin._alist_token = "t"
        plugin._alist_target_path = "/cloud"
        plugin._upload_path_mappings = "/media/movies#/cloud/movies"
        plugin._strm_path_mappings = "/cloud/movies#/strm/movies"
        plugin._local_strm_paths = "/media/movies#/strm/movies"
        plugin._local_media_path = "/media/movies"
        plugin._strm_output_path = "/strm/movies"
        plugin._sync_mode = "copy"
        plugin._overwrite_mode = "never"
        plugin._exclude_patterns = ""
        plugin._event_filters = ""
        plugin._refresh_enabled = True
        plugin._mediaservers = []
        plugin._transfer_mp_mediaserver_paths = ""
        plugin._notify_enabled = True
        plugin._rmt_mediaext = ["mkv"]
        plugin._upload_concurrency = 3
        plugin._once_sync = False
        plugin._strm_url_mode = "moviepilot_redirect"
        plugin._resolve_final_url = True
        plugin._direct_link_mode = "prefer_raw_url"
        plugin._redirect_cache_ttl = 120
        plugin._head_probe_mode = "ok"
        plugin._sse_enabled = False
        plugin._emby_proxy_enabled = False
        plugin._emby_server_url = ""
        plugin._emby_proxy_host = "0.0.0.0"
        plugin._emby_proxy_port = 8095
        plugin._manual_upload_action = "none"
        plugin._manual_upload_target = ""
        plugin._manual_strm_target = ""
        plugin._manual_confirm = False
        plugin._manual_execute = False
        captured = {}
        plugin.update_config = lambda cfg: captured.update(cfg)
        plugin._update_config()
        for key in (
            "upload_path_mappings", "strm_path_mappings",
            "strm_url_mode", "resolve_final_url", "direct_link_mode",
            "redirect_cache_ttl", "head_probe_mode", "sse_enabled",
            "manual_upload_action", "manual_execute",
        ):
            self.assertIn(key, captured, f"{key} 未持久化")

    def test_normalize_strm_url_mode(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode("cloud_raw_url"), "cloud_raw_url")
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode("raw_url"), "cloud_raw_url")
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode("alist_direct"), "alist_direct")
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode("moviepilot_redirect"), "alist_direct")
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode("garbage"), "alist_direct")
        self.assertEqual(CloudStrmHelper._normalize_strm_url_mode(""), "alist_direct")

    def test_normalize_head_probe_mode(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertEqual(CloudStrmHelper._normalize_head_probe_mode("ok"), "ok")
        self.assertEqual(CloudStrmHelper._normalize_head_probe_mode("redirect"), "redirect")
        self.assertEqual(CloudStrmHelper._normalize_head_probe_mode("resolve"), "resolve")
        self.assertEqual(CloudStrmHelper._normalize_head_probe_mode("garbage"), "ok")

    def test_normalize_direct_link_mode(self):
        from cloudstrmhelper import CloudStrmHelper
        self.assertEqual(CloudStrmHelper._normalize_direct_link_mode("prefer_raw_url"), "prefer_raw_url")
        self.assertEqual(CloudStrmHelper._normalize_direct_link_mode("raw_url"), "raw_url_only")
        self.assertEqual(CloudStrmHelper._normalize_direct_link_mode("alist"), "alist_download")
        self.assertEqual(CloudStrmHelper._normalize_direct_link_mode("garbage"), "prefer_raw_url")

    def test_diagnose_includes_redirect_fields(self):
        from cloudstrmhelper import CloudStrmHelper
        import json
        plugin = CloudStrmHelper.__new__(CloudStrmHelper)
        plugin._enabled = True
        plugin._moviepilot_address = "http://mp:3000"
        plugin._cloud_storage_type = "alist"
        plugin._alist_url = "http://alist:5244/"
        plugin._alist_token = "secret-token-value"
        plugin._alist_target_path = "/cloud"
        plugin._local_strm_paths = "/media/movies#/strm/movies"
        plugin._local_strm_mappings = [("/media/movies", "/strm/movies")]
        plugin._local_media_path = "/media/movies"
        plugin._local_media_roots = ["/media/movies"]
        plugin._strm_output_path = "/strm/movies"
        plugin._sync_mode = "copy"
        plugin._overwrite_mode = "never"
        plugin._upload_concurrency = 3
        plugin._rmt_mediaext = ["mkv"]
        plugin._event_filter_prefixes = []
        plugin._refresh_enabled = True
        plugin._mediaservers = []
        plugin._transfer_mp_mediaserver_paths = ""
        plugin._strm_url_mode = "moviepilot_redirect"
        plugin._resolve_final_url = True
        plugin._direct_link_mode = "prefer_raw_url"
        plugin._redirect_cache_ttl = 120
        plugin._head_probe_mode = "ok"
        from cachetools import TTLCache
        plugin._redirect_cache = TTLCache(maxsize=10, ttl=10)
        plugin._redirect_error_cache = TTLCache(maxsize=10, ttl=10)
        plugin._sse_listener = None
        plugin._alist_client = None
        plugin._cloud_sync = None
        plugin._strm_gen = None
        plugin._proxy = None
        plugin._stats = {"upload_count": 0, "last_upload_time": "", "recent_uploads": [],
                         "strm_count": 0, "last_strm_time": "", "recent_strms": []}
        data = plugin._diagnostic_snapshot(probe=False)
        self.assertEqual(data["redirect"]["strm_url_mode"], "moviepilot_redirect")
        self.assertEqual(data["redirect"]["resolve_final_url"], True)
        self.assertEqual(data["redirect"]["direct_link_mode"], "prefer_raw_url")
        self.assertEqual(data["redirect"]["redirect_cache_ttl"], 120)
        self.assertEqual(data["redirect"]["head_probe_mode"], "ok")
        self.assertEqual(data["redirect"]["redirect_cache_size"], 0)
        # 不泄露 token
        self.assertNotIn("secret-token-value", json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
