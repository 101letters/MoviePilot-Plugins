# 云端STRM整理助手 (CloudStrmHelper)

MoviePilot V2 插件：监听 MoviePilot 整理完成/入库完成消息，按顺序上传到 AList/OpenList，基于云端路径生成 STRM，并刷新 Emby/Jellyfin 入库。

> 版本：1.3.2

## 执行链路

```text
Phase 1 监听事件
  - SSE: GET /api/v1/system/message（只记录整理完成/入库完成事件）
  - 进程内 EventType.TransferComplete 兜底
  - 只记录事件与路径，不做文件操作
        |
        v
Phase 2 AList 同步
  - 按「本地路径#云端路径」上传映射计算 AList/OpenList 云端路径
  - SSE 如只记录到目录，在本阶段展开目录内媒体文件
  - 仅新增，远端已存在即跳过，永不删除远端文件
        |
        v
Phase 3 STRM 生成
  - 按「云端路径#本地 STRM 输出目录」映射生成 STRM
  - STRM 内容为 302 跳转 URL（默认 MoviePilot /redirect 模式）
  - STRM 覆盖模式可选「从不/总是」
        |
        v
Phase 4 媒体库刷新
  - 按路径映射刷新 Emby/Jellyfin
```

内部 `EventType.TransferComplete` 仍作为兜底入口，但同样只进入 Phase 1 记录，再按上述批次顺序执行。

## 三类路径/地址（务必区分）

1. **MoviePilot 内网地址**：生成 STRM 内的 `/redirect` 播放入口 URL，如 `http://192.168.31.6:3000`。
2. **AList/OpenList 地址**：上传文件、查询文件、`fs_get` 解析 `raw_url`/`sign`、构造 `/d` 下载地址，如 `http://192.168.31.6:5244/`。
3. **上传映射**：决定哪些本地媒体库路径会上传到哪个 AList/OpenList 云端路径，格式 `本地路径#云端路径`。
4. **STRM 映射**：决定云端路径对应的本地 `.strm` 输出目录，格式 `云端路径#本地STRM输出目录`。

## 默认配置

| 配置项 | 默认值 |
|---|---|
| MoviePilot 内网地址 | `http://192.168.31.6:3000` |
| 云存储类型 | `alist`（AList / OpenList） |
| AList/OpenList 地址 | `http://192.168.31.6:5244/` |
| 上传映射 | `/media/movies#/123云盘/影视/华语电影`、`/media/tv#/123云盘/影视/电视剧` |
| STRM 映射 | `/123云盘/影视/华语电影#/strm/test/华语电影`、`/123云盘/影视/电视剧#/strm/test/电视剧` |
| 同步模式 | 复制 |
| STRM 覆盖模式 | 从不 |
| STRM URL 模式 | `moviepilot_redirect`（推荐） |
| 解析最终直链 | 开 |
| 直链缓存时间 | `120` 秒 |
| HEAD 探测策略 | `ok`（兼容模式） |
| 并发上传数 | `3` |
| 媒体扩展名 | `mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v` |
| 排除规则 | `*.tmp`、`**/.DS_Store`、`/sample/**` |
| 事件路径过滤 | `/media/movies`、`/media/tv` |
| 媒体服务器路径映射 | `/media#/data` |

## 路径映射

上传映射用一行一个配置，格式是：

```text
本地媒体库路径#AList/OpenList 云端路径
```

插件只会处理这些本地根目录下的媒体文件，并把本地相对路径追加到对应云端路径后面：

```text
/media/movies#/123云盘/影视/华语电影
/media/movies/Foo/Foo.mkv
-> /123云盘/影视/华语电影/Foo/Foo.mkv

/media/tv#/123云盘/影视/电视剧
/media/tv/Show/S01E01.mkv
-> /123云盘/影视/电视剧/Show/S01E01.mkv
```

STRM 映射也用一行一个配置，格式是：

```text
AList/OpenList 云端路径#本地 STRM 输出目录
```

示例：

```text
/123云盘/影视/华语电影#/strm/test/华语电影
/123云盘/影视/电视剧#/strm/test/电视剧
```

对应生成：

```text
/123云盘/影视/华语电影/Foo/Foo.mkv
-> /strm/test/华语电影/Foo/Foo.strm

/123云盘/影视/电视剧/Show/S01E01.mkv
-> /strm/test/电视剧/Show/S01E01.strm
```

兼容旧字段 `alist_target_path` / `local_strm_paths` / `local_media_path` / `strm_output_path`：如果没有新映射字段，插件会按旧配置自动推导上传映射和 STRM 映射。

## STRM URL 模式

### 推荐模式：MoviePilot 302（`moviepilot_redirect`，默认）

STRM 文件内容固定指向 MoviePilot 插件 `/redirect`：

```text
http://<MoviePilot内网地址>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<API_TOKEN>&path=<AList/OpenList虚拟路径>
```

播放时插件 `/redirect` 端点实时向 AList/OpenList 获取 `raw_url` 或 `sign` 下载地址并 302 跳转，避免直链过期。这个模式下 MoviePilot 只参与一次 URL 解析和 302 跳转，不代理视频数据；但外网客户端必须能访问 MoviePilot `/redirect`。

### 实验模式：AList/OpenList 直链（`alist_direct`，不默认启用）

STRM 内容直接写入 AList/OpenList `/d/<path>?sign=<sign>` 下载地址（不写 `raw_url`，因 `raw_url` 可能过期）。取 `sign` 失败时仅 warning，生成无 `sign` 的 `/d/` 地址，不中断 STRM 生成。

这个模式绕过 MoviePilot，但不一定绕过 AList/OpenList 服务器：是否由 OpenList 再跳转到云盘厂商 CDN，取决于具体存储驱动和分享/签名能力。

### 实验模式：云盘 raw_url 直链（`cloud_raw_url`，不默认启用）

STRM 内容直接写入 AList/OpenList `fs_get` 返回的 `raw_url`；如果开启“解析最终直链”，会先跟随上游重定向，尽量写入最终 CDN URL。

这是唯一会尽量绕过 MoviePilot 和 AList/OpenList 数据流量的 STRM 模式。但 `raw_url`/CDN URL 通常有过期时间，过期后需要重新生成 STRM；如果 AList/OpenList 没返回 `raw_url`，插件会 warning 并回退到 `/d/<path>?sign=...`。

风险：直链可能受过期时间、权限、客户端 UA、跨域和云盘策略影响，仅用于测试。

## 轻量 302 与完整前置代理的区别

本插件内置 `/redirect` 是**轻量 302**：只负责 STRM 播放入口重定向，**不代理整个 Emby/Jellyfin**，不修改 Web 前端，不启动独立代理端口。

如果使用 Emby Web 播放遇到 CORS/crossOrigin 问题，这是完整前置反向代理才能彻底解决的问题（参考 embyreverseproxy / MediaWarp）。本插件优先保证 Infuse、Fileball、Emby App、Jellyfin App 等客户端。

### `/redirect` 302 可靠性增强（v1.3.0）

- **缓存**：按 `(path, UA 哈希, 解析模式)` 缓存最终 URL，TTL 可配（默认 120s），避免缓存爆炸。
- **in-flight 请求合并**：同 key 并发只解析一次，避免起播时多请求重复打 AList。
- **失败负缓存**：坏路径短 TTL（30s）内直接 502，避免疯狂请求。
- **HEAD 探测策略**（`head_probe_mode`）：
  - `ok`（默认兼容）：HEAD 返回 200，不跳转，兼容 Infuse/Fileball 先 HEAD 探测。
  - `redirect`（严格）：HEAD 同 GET 返回 302。
  - `resolve`（诊断）：HEAD 解析目标 URL 但返回 200，header 附带脱敏 `X-Resolved-Url`（仅 host）。
- **最终 URL 解析**（`resolve_final_url`）：HEAD 跟随上游重定向取最终 URL（≤10 跳，循环检测，多策略超时重试，HEAD 失败回退 GET Range `bytes=0-0`），最终失败回退原始 URL 不中断播放。
- **日志脱敏**：日志不打印带 `sign`/`token` 的完整 URL，只保留 `scheme://host/path` 与 query key。
- **响应头**：302 时加 `Cache-Control: no-store`、`X-CloudStrm-Mode`，不泄露真实 Token。

## UI

配置页按以下顺序展示 6 个卡片：

1. 基础设置（启用、立刻全量同步、任务完成通知、上传并发数）
2. 播放入口设置（MoviePilot 内网地址、STRM URL 模式、解析最终直链、直链缓存时间、HEAD 探测策略）
3. 云端存储设置（云存储类型、AList/OpenList 地址、Token）
4. 上传与 STRM 路径映射（上传映射、STRM 映射）
5. 同步与过滤（同步模式、STRM 覆盖模式、媒体扩展名、排除规则、事件路径过滤）
6. 媒体服务器刷新（刷新开关、媒体服务器选择、路径映射）

同步模式：

- `复制`：上传到 AList/OpenList 后保留本地源文件。
- `移动`：上传且 STRM 生成成功后删除本地源文件；不会删除远端文件。

STRM 覆盖模式：

- `从不`：已有 STRM 直接跳过。
- `总是`：重新写入已有 STRM。

## 首页统计面板

插件首页展示：

- 累计上传数量
- 最近上传时间
- 累计生成 STRM 数量
- 最近生成 STRM 时间

以及两个列表：

- 最近上传列表（文件名 | 状态 | 大小 | 时间 | 云端路径）
- 最近生成 STRM 列表（文件名 | 状态 | 时间 | STRM 路径 | 云端路径）

状态：上传 `已上传`/`远端已存在`；STRM `新生成`/`已存在/已跳过`。统计通过插件数据持久化，重启后保留，并兼容旧 `recent_files` 结构迁移到 `recent_strms`。

## 安装

把 `plugins.v2/cloudstrmhelper/` 放入 MoviePilot 插件仓库，或复制到 MoviePilot 本地插件目录 `config/plugins/cloudstrmhelper/`，重启 MoviePilot 后安装启用。

依赖见 `requirements.txt`：

```text
requests
pathspec
cachetools
```

## 验证

1. 配置 AList/OpenList 地址、Token、上传映射、STRM 映射、媒体服务器路径映射。
2. 启用插件，观察日志出现 `【SSE监听】连接 MoviePilot 消息流`。
3. 让 MoviePilot 完成一次整理入库，日志应按顺序出现：
   - `Phase 1 完成`
   - `Phase 2 开始`
   - `Phase 2 完成`
   - `Phase 3 开始`
   - `Phase 3/4 完成`
4. 检查上传映射对应的 AList/OpenList 云端目录新增媒体文件。
5. 检查 STRM 输出目录新增 `.strm`，内容为 302 URL（推荐模式指向 `/redirect`）。
6. 检查 Emby/Jellyfin 是否扫描到新条目并能通过 302 播放。
7. Infuse/Fileball 的 HEAD 探测应按 `head_probe_mode` 返回（默认 200），GET 才 302。

也可以在配置中勾选「立刻全量同步」，或调用 `/api/v1/plugin/CloudStrmHelper/sync_now` 触发一次全量同步。

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/redirect` | `GET/HEAD` | STRM 播放用 302 跳转，参数 `apikey`、`path` |
| `/status` | `GET` | 查看同步队列状态 |
| `/diagnose` | `GET` | 查看脱敏配置、模块状态、302 状态、路径映射和统计 |
| `/diagnose?probe=true` | `GET` | 在诊断基础上只读探测 AList/OpenList Token/地址/fs_get（不输出 raw_url/sign 完整内容） |
| `/sync_now` | `GET/POST` | 手动触发一次全量同步 |

`/diagnose` 输出的 302 相关字段：`strm_url_mode`、`resolve_final_url`、`redirect_cache_ttl`、`head_probe_mode`、`redirect_cache_size`、`redirect_error_cache_size`。

## 安全约束

- 不删除远端文件。
- 不覆盖远端已有文件。
- STRM 是否覆盖由「STRM 覆盖模式」控制。
- Phase 1 不做任何文件系统或 AList 操作。
- 每个批次先完成 AList 同步，再生成 STRM 和刷新媒体库。
- Token 脱敏展示，日志不输出带 sign/token 的完整 URL。
