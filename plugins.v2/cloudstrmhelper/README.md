# 云端STRM整理助手 (CloudStrmHelper)

MoviePilot V2 插件：监听 MoviePilot 整理完成/入库完成消息，按顺序上传到 AList/OpenList，基于云端路径生成 STRM，并刷新 Emby/Jellyfin 入库。

> 版本：1.3.0

## 执行链路

```text
Phase 1 监听事件
  - SSE: GET /api/v1/system/message（只记录整理完成/入库完成事件）
  - 进程内 EventType.TransferComplete 兜底
  - 只记录事件与路径，不做文件操作
        |
        v
Phase 2 AList 同步
  - 本地媒体路径 → AList 上传目标根目录
  - SSE 如只记录到目录，在本阶段展开目录内媒体文件
  - 仅新增，远端已存在即跳过，永不删除远端文件
        |
        v
Phase 3 STRM 生成
  - 按「本地媒体库路径#STRM输出目录」映射生成 STRM
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
3. **STRM 输出目录**：保存 `.strm` 给 Emby/Jellyfin 扫描，来自 `本地媒体库路径#STRM输出目录` 映射。

## 默认配置

| 配置项 | 默认值 |
|---|---|
| MoviePilot 内网地址 | `http://192.168.31.6:3000` |
| 云存储类型 | `alist`（AList / OpenList） |
| AList/OpenList 地址 | `http://192.168.31.6:5244/` |
| AList/OpenList 上传目标根目录 | `/123云盘/影视/华语电影` |
| 本地与 STRM 路径映射 | `/media/movies#/strm/test/华语电影`、`/media/tv#/strm/test/电视剧` |
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

AList 上传目标根目录就是同步上传到云端的根目录。插件会把本地媒体相对路径追加到这个根目录下：

```text
/media/movies/Foo/Foo.mkv
-> /123云盘/影视/华语电影/Foo/Foo.mkv

/media/tv/Show/S01E01.mkv
-> /123云盘/影视/华语电影/Show/S01E01.mkv
```

本地与 STRM 路径用一行一个映射配置，格式是：

```text
本地媒体库路径#STRM输出目录
```

示例：

```text
/media/movies#/strm/test/华语电影
/media/tv#/strm/test/电视剧
```

对应生成：

```text
/media/movies/Foo/Foo.mkv
-> /strm/test/华语电影/Foo/Foo.strm

/media/tv/Show/S01E01.mkv
-> /strm/test/电视剧/Show/S01E01.strm
```

兼容旧字段 `local_media_path` / `strm_output_path`：若旧配置存在而 `local_strm_paths` 不存在，自动迁移。

## STRM URL 模式

### 推荐模式：MoviePilot 302（`moviepilot_redirect`，默认）

STRM 文件内容固定指向 MoviePilot 插件 `/redirect`：

```text
http://<MoviePilot内网地址>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<API_TOKEN>&path=<AList/OpenList虚拟路径>
```

播放时插件 `/redirect` 端点实时向 AList/OpenList 获取 `raw_url` 或 `sign` 下载地址并 302 跳转，避免直链过期。

### 实验模式：AList/OpenList 直链（`alist_direct`，不默认启用）

STRM 内容直接写入 AList/OpenList `/d/<path>?sign=<sign>` 下载地址（不写 `raw_url`，因 `raw_url` 可能过期）。取 `sign` 失败时仅 warning，生成无 `sign` 的 `/d/` 地址，不中断 STRM 生成。

风险：可能受 `sign` 过期、权限、客户端 UA、跨域和云盘策略影响，仅用于测试。

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
3. 云端存储设置（云存储类型、AList/OpenList 地址、Token、上传目标根目录）
4. 本地与 STRM 路径映射
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

1. 配置 AList/OpenList 地址、Token、上传目标根目录、本地与 STRM 路径映射、媒体服务器路径映射。
2. 启用插件，观察日志出现 `【SSE监听】连接 MoviePilot 消息流`。
3. 让 MoviePilot 完成一次整理入库，日志应按顺序出现：
   - `Phase 1 完成`
   - `Phase 2 开始`
   - `Phase 2 完成`
   - `Phase 3 开始`
   - `Phase 3/4 完成`
4. 检查 AList/OpenList 目标目录新增媒体文件。
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
