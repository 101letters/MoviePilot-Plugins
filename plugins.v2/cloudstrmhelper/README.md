# 云端STRM整理助手 (CloudStrmHelper)

MoviePilot V2 插件：整理入库后自动把本地媒体复制到 AList 云端，生成 STRM 文件，并让 Emby/Jellyfin 通过 302 直链播放云端文件，绕开本地上行带宽限制。

## 功能链路

```
整理完成事件 ──► 过滤分发 ──► 复制本地文件到 AList（PUT /fs/put，As-Task 轮询进度）
                                       │
                                       ▼
                        生成 .strm（内容=插件自带 /redirect 302 端点 URL）
                                       │
                                       ▼
                        刷新 Emby/Jellyfin 媒体库
                                       │
              播放时：客户端打开 .strm ──► /redirect 端点
                                       │ FsGet 取 raw_url
                                       ▼
                                 302 → 云端真实直链（流量不经过 Emby/MP）
```

## 安装

把 `cloudstrmhelper/` 目录放入 MoviePilot 插件目录：

- **本地安装**：`config/plugins/cloudstrmhelper/`（与插件目录同名，类名小写），重启 MoviePilot，在「插件市场」→「已安装」中看到「云端STRM整理助手」。
- **仓库安装**：将本目录放入你的插件仓库 `plugins.v2/cloudstrmhelper/`，在 MP 插件市场添加仓库地址后安装。

依赖（`requirements.txt`，MP 启用插件时自动 pip 安装）：`requests`、`pathspec`、`cachetools`。FastAPI/httpx/apscheduler 由 MoviePilot 自带，无需列出。

## 配置

在插件配置页填写：

| 配置项 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| MoviePilot 内网地址 | 如 `http://192.168.1.10:3000`，用于构建 STRM 内的 302 跳转 URL；留空则用 `MP_DOMAIN`，再不行回退 `localhost:3000` |
| 云端存储类型 | AList（已实现）/ 本地挂载目录（直接复制）/ WebDAV（预留未实现） |
| AList 地址 | 如 `http://192.168.31.5:5244` |
| AList Token | AList 管理后台 → 设置 → 令牌（静态 token，裸 Authorization 鉴权） |
| 云端目标路径 | AList 中的根目录，如 `/媒体库` |
| 本地媒体库路径 | 本地整理后的媒体根目录，如 `/media/movies` |
| STRM 输出目录 | Emby/Jellyfin 媒体库扫描的 STRM 目录，如 `/strm/movies` |
| 同步模式 | 仅新增（不删远端）/ 全同步（补传，删除逻辑预留） |
| STRM 覆盖模式 | 从不（跳过已存在）/ 总是（覆盖） |
| 并发上传数 | 同时上传的文件数，默认 3 |
| 可处理媒体扩展名 | 逗号分隔，默认 mp4,mkv,ts,iso,... |
| 排除规则 | gitignore 语法，一行一条，如 `*.tmp`、`/sample/**` |
| 事件路径过滤 | 一行一个本地目录前缀，仅这些路径下才处理；留空=全部 |
| 生成 STRM 后刷新媒体服务器 | 开关 |
| 媒体服务器 | 选择要刷新的 Emby/Jellyfin（多选） |
| 路径映射 | 「媒体服务器路径#MP路径」一行一条，如 `/media#/data`；两者路径不同时用于刷新入库 |
| 任务完成通知 | 开关，通过 MP 内建消息渠道推送 |

### 路径对应关系（关键）

STRM 输出路径 = `STRM 输出目录` + (本地文件相对于 `本地媒体库路径` 的部分) + `.strm`。

例：本地 `/media/movies/Foo (2024)/Foo (2024).mkv`，本地媒体库路径 `/media/movies`，STRM 输出目录 `/strm/movies` → 生成 `/strm/movies/Foo (2024)/Foo (2024).strm`。

云端路径 = `云端目标路径` + 相对部分。例：云端目标路径 `/媒体库` → `/媒体库/Foo (2024)/Foo (2024).mkv`。

## 工作原理

### 触发：进程内事件（非 SSE）

MoviePilot 的「整理完成」通过进程内 `EventType.TransferComplete` 事件可靠传递，`event.event_data` 含 `mediainfo/meta/transferinfo/fileitem`。本插件用 `@eventmanager.register(EventType.TransferComplete)` 接收，过滤后入队云同步。

### 上传：AList PUT /api/fs/put + As-Task

- 本地→云端用 AList 流式上传 `PUT /api/fs/put`，设 `As-Task: true` 变成可轮询进度的 AList 任务。
- 轮询 `POST /api/admin/task/upload/info?tid=<id>`（注意是 `/upload/` 任务组）读取 `state`/`progress`。
- 增量判定：list 远端目录建 name→size dict，缺失或 size 不一致才上传（size-only，与 taosync 一致）。
- 并发上传（默认 3），单文件失败不中断队列，上传 PUT 有 3 次指数退避重试。

### STRM：自托管 302

`.strm` 文件内容是：

```
http://<MP地址>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<API_TOKEN>&path=<AList路径>
```

播放时客户端请求该 URL，插件 `/redirect` 端点：
1. 校验 `apikey`。
2. `AList FsGet(path)` 取 `raw_url`（上游真实直链，优先）或构建 `{alist}/d{path}?sign={sign}`。
3. 可选跟随重定向取最终 URL（HEAD，传客户端 UA，≤10 跳）。
4. 返回 `302` 跳转到直链。

**HEAD 请求放行**（返回 200 而非 302），兼容 Infuse/Fileball 等先 HEAD 探测的客户端。解析结果按 `(path, ua)` 缓存 2 分钟。

### Emby/Jellyfin 刷新

用 `MediaServerHelper().get_services()` → `service.instance.refresh_library_by_items([RefreshMediaItem])`，媒体服务器无关。支持路径映射替换。

## API 端点

插件暴露三个端点（MoviePilot 标准前缀 `/api/v1/plugin/CloudStrmHelper/`）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/redirect` | GET/HEAD | 302 跳转（STRM 播放用），参数 `apikey`、`path` |
| `/status` | GET | 查询云同步进度（num/size by 状态、duration、进行中列表） |
| `/sync_now` | GET/POST | 手动触发一次全量同步（异步） |

## 部署与验证

1. **安装插件**：按上文「安装」放入目录并重启 MP。
2. **配置**：填 AList 地址/token、云端目标路径、本地媒体路径、STRM 输出目录、媒体服务器。
3. **触发**：
   - 自动：MoviePilot 整理一次媒体（下载→整理入库），观察 MP 日志出现「【整理监听】... 入队 N」。
   - 手动：勾选「立刻全量同步」保存，或调用 `/sync_now`。
4. **观察日志**：`【云同步】入队` → `/fs/put 上传` → 任务轮询 → `【STRM生成】生成成功` → `已通知 Emby 刷新`。
5. **播放验证**：Emby 扫到新 STRM 条目 → 播放 → 抓包确认客户端先请求 `/api/v1/plugin/CloudStrmHelper/redirect?...` → 收到 302 → 直连 AList 直链播放（流量不经过 MP/Emby）。
6. **客户端兼容**：Infuse/Fileball 的 HEAD 探测应放行；Web/iOS Emby 走 GET 302。如遇跨域 CDN 的 CORS 问题（浏览器播放器），可参考 embyreverseproxy 的 crossOrigin 补丁思路（本插件未内置反向代理端口，必要时可加装）。
7. **状态查询**：`GET /api/v1/plugin/CloudStrmHelper/status` 返回进度 JSON。
8. **通知**：MP 消息中心收到任务成功/失败消息。

## 规格 vs 实现偏差

| 规格原文 | 实际做法 | 原因 |
|---|---|---|
| 模块1 监听 SSE `/api/v1/system/message` | 进程内 `EventType.TransferComplete` 事件 | SSE 流只承载瞬时弹窗，不含结构化媒体路径；参考插件（p123strmhelper/chinesesubfinder）均用事件处理器 |
| 模块4 前置反向代理 + `proxy_port`(默认 8096) | 插件自带 `/redirect` 端点（走 MP 3000 端口），无独立代理端口 | 采用 p123strmhelper 的「自托管 302」思路，更轻量、无需占用端口、链接不失效 |
| `requirements.txt` 含 aiohttp/pyyaml/watchdog/p123client | 仅 `requests`/`pathspec`/`cachetools` | aiohttp 不需要（用 MP 自带 FastAPI）、pyyaml 不需要（用 Vuetify 表单）、watchdog/p123client 按需剔除 |
| `strm_mode` HTTPStrm/AlistStrm 二选一 | 统一为 redirect URL（自托管 302 已涵盖两种解析） | redirect 端点播放时实时 FsGet 取 raw_url，统一处理 |
| `moviepilot_api_token`/`emby_url`/`proxy_port` 配置项 | 移除 | 改用 `settings.API_TOKEN` 与 `MediaServerHelper`，避免重复配置 |
| 钉钉/Server酱 通知 | MP 内建消息渠道 | MP 内建已覆盖主流渠道，避免重复实现 |
| watchdog 文件监控 | 未实现 | MP 整理事件已足够触发，watchdog 冗余 |

## 参考项目

| 项目 | 用途 |
|---|---|
| [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) | 插件开发规范、Vuetify 表单范式 |
| [taosync](https://github.com/dr34m-cn/taosync) | 同步/队列/进度/排除架构（注：taosync 只做 AList 内部 copy，本插件上传用 AList /fs/put） |
| [p123strmhelper](https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/plugins.v2/p123strmhelper) | 自托管 302 STRM、整理事件处理、Emby 刷新 |
| [MediaWarp](https://github.com/AkimioJR/MediaWarp) | AlistStrm 解析（FsGet→raw_url/sign）、HEAD 放行、重定向跟随 |
| [embyreverseproxy](https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/plugins.v2/embyreverseproxy) | 反向代理架构参考（本插件未采用独立代理端口） |

## 局限与扩展点

- **WebDAV 云端类型**：表单可选但未实现，`WebdavClient` 占位 `NotImplementedError`，可后续基于 webdav3client 实现 PROPFIND/MKCOL/PUT。
- **full 同步删除远端**：当前 full 模式仅补传，删除远端多余文件逻辑预留（`_cleanup_remote_dir`），避免误删。
- **增量判定**：size-only（与 taosync 一致），同 size 不同内容不重传；如需更强可用 `fs_get` 的 `hash_info`。
- **断点续传**：内存队列，重启重跑（靠增量判定幂等）；可后续用 `plugin.save_data` 持久化任务表。
- **跨域 CORS**：浏览器播放器直链跨域 CDN 时可能触发 CORS；本插件无独立代理端口，必要时可加装 embyreverseproxy 式 crossOrigin 补丁或前置代理。

## 文件结构

```
cloudstrmhelper/
├── __init__.py            # 插件主类：元数据 + 生命周期 + 事件处理 + API + 表单
├── transfer_listener.py   # 整理完成事件过滤/分发
├── cloud_sync.py          # AList 客户端 + 上传/轮询 + 队列/进度
├── strm_generator.py      # STRM 生成 + Emby 刷新
├── proxy_handler.py       # /redirect 302 解析
├── requirements.txt       # requests / pathspec / cachetools
└── README.md
```
