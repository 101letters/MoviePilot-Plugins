# 云端STRM整理助手 (CloudStrmHelper)

MoviePilot V2 插件：监听 MoviePilot 整理完成/入库完成消息，按顺序上传到 AList/OpenList，基于云端路径生成 STRM，并刷新 Emby/Jellyfin 入库。

> 版本：1.5.7

## 执行链路

```text
Phase 1 监听事件
  - SSE（可选）: GET /api/v1/system/message（只记录整理完成/入库完成事件）
  - 进程内 EventType.TransferComplete 兜底
  - 只记录事件与路径，不做文件操作
        |
        v
Phase 2 AList 同步
  - 按「本地路径#云端路径」上传映射计算 AList/OpenList 云端路径
  - SSE 如只记录到目录，在本阶段展开目录内媒体文件
  - 常规同步仅新增，远端已存在即跳过，不删除远端文件
        |
        v
Phase 3 STRM 生成
  - 按「云端路径#本地 STRM 输出目录」映射生成 STRM
  - STRM 内容默认为 AList/OpenList `/d/<path>` 302 下载地址
  - STRM 覆盖模式可选「从不/总是」
        |
        v
Phase 4 媒体库刷新
  - 按路径映射刷新 Emby/Jellyfin
```

内部 `EventType.TransferComplete` 仍作为兜底入口，但同样只进入 Phase 1 记录，再按上述批次顺序执行。

## 三类路径/地址（务必区分）

1. **MoviePilot 内网地址**：插件 API、诊断接口、兼容旧 STRM 的 `/redirect` 播放入口，如 `http://192.168.31.6:3000`。
2. **AList/OpenList 地址**：上传文件、查询文件、`fs_get` 解析 `raw_url`/`sign`，也是默认 STRM `/d` 下载地址来源，如 `http://192.168.31.6:5244/`。
3. **上传映射**：决定哪些本地媒体库路径会上传到哪个 AList/OpenList 云端路径，格式 `本地路径#云端路径`。
4. **STRM 映射**：决定云端路径对应的本地 `.strm` 输出目录，格式 `云端路径#本地STRM输出目录`。

## 默认配置

| 配置项 | 默认值 |
|---|---|
| MoviePilot 内网地址 | `http://192.168.31.6:3000`（内部固定，UI 不暴露） |
| 云存储类型 | `alist`（AList / OpenList） |
| AList/OpenList 地址 | `http://192.168.31.6:5244/` |
| 上传映射 | `/media/movies#/123云盘/影视/华语电影`、`/media/tv#/123云盘/影视/电视剧` |
| STRM 映射 | `/123云盘/影视/华语电影#/strm/test/华语电影`、`/123云盘/影视/电视剧#/strm/test/电视剧` |
| 同步模式 | 复制 |
| STRM 覆盖模式 | 从不 |
| STRM URL 模式 | `alist_direct`（内部固定，UI 不暴露） |
| SSE 监听 | 关闭 |
| 解析最终直链 | 开（内部固定，UI 不暴露） |
| 直链来源策略 | `prefer_raw_url`（内部固定，UI 不暴露） |
| 直链缓存时间 | `120` 秒（内部固定，UI 不暴露） |
| HEAD 探测策略 | `ok`（兼容模式，内部固定，UI 不暴露） |
| Emby 302 前置代理 | 关闭 |
| Emby 原始地址 | `http://192.168.31.6:8096` |
| Emby 代理监听地址 | `0.0.0.0` |
| Emby 代理监听端口 | `8095` |
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

### 推荐模式：AList/OpenList `/d` 302（`alist_direct`，默认）

STRM 文件内容直接写入 AList/OpenList 下载地址：

```text
http://<AList/OpenList地址>/d/<AList/OpenList虚拟路径>?sign=<sign>
```

这个模式保留 `.mkv`、`.mp4` 等真实媒体后缀，Emby/Jellyfin 更容易识别可播放源；播放时由 AList/OpenList `/d` 再按存储驱动能力 302 到云盘直链。取 `sign` 失败时仅 warning，生成无 `sign` 的 `/d/` 地址，不中断 STRM 生成。

是否完全绕过 AList/OpenList 服务器上行，取决于具体存储驱动的 `/d` 行为：如果 `/d` 返回 302 到云盘/CDN，则播放数据不走服务器；如果驱动只能中转下载，则仍可能占用 OpenList/NAS 上行。

### 兼容模式：MoviePilot `/redirect`

历史版本可能已经生成了 MoviePilot 插件 `/redirect` 格式的 STRM：

```text
http://<MoviePilot内网地址>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<API_TOKEN>&path=<AList/OpenList虚拟路径>
```

插件仍保留 `/redirect` API 兼容旧 STRM，但配置页不再提供这个生成模式；保存配置时旧值 `moviepilot_redirect` 会迁移为 `alist_direct`。如果旧 STRM 在 Emby 里无法识别，在首页「最近生成 STRM 列表」对该条记录点「重新生成 STRM」即可改写为 `/d/` 地址。

MoviePilot `/redirect` 与 Emby 302 前置代理的直链来源由 `direct_link_mode` 控制：

- `prefer_raw_url`（默认）：优先返回 AList/OpenList `fs_get.raw_url`；没有 `raw_url` 时回退 `/d/<path>?sign=...`，并在开启“解析最终直链”时尽量跟随到云盘/CDN 最终地址。
- `raw_url_only`（严格零上行）：只允许返回云盘 `raw_url`。如果 AList/OpenList 没返回 `raw_url`，`/redirect` 直接 502，不回退 `/d`，避免实际播放流量走 OpenList/NAS 上行。
- `alist_download`（兼容）：总是返回 AList/OpenList `/d` 下载端点，适合 raw_url 不稳定或客户端必须走 OpenList 鉴权的场景；是否绕过 OpenList 数据流量取决于 `/d` 是否继续 302 到云盘。

### 实验模式：云盘 raw_url 直链（`cloud_raw_url`，不默认启用）

STRM 内容直接写入 AList/OpenList `fs_get` 返回的 `raw_url`；如果开启“解析最终直链”，会先跟随上游重定向，尽量写入最终 CDN URL。

这是唯一会尽量绕过 MoviePilot 和 AList/OpenList 数据流量的 STRM 模式。但 `raw_url`/CDN URL 通常有过期时间，过期后需要重新生成 STRM；如果 AList/OpenList 没返回 `raw_url`，插件默认会 warning 并回退到 `/d/<path>?sign=...`。如果同时把直链来源策略设为 `raw_url_only`，则不再回退 `/d`，而是让本次 STRM 生成失败。

风险：直链可能受过期时间、权限、客户端 UA、跨域和云盘策略影响，仅用于测试。

## Emby 302 前置代理（qmediasync 风格）

除轻量 `/redirect` 外，v1.5.0 新增独立 Emby 前置代理端口。启用后，客户端不再直连 Emby 原始端口，而是连接本插件代理端口，例如：

```text
Emby 原始地址: http://192.168.31.6:8096
代理监听:     http://192.168.31.6:8095
```

代理按 qmediasync 的核心思路处理：

1. 拦截 `/Items/{id}/PlaybackInfo`，回源 Emby 后检查 `MediaSources[].Path`。
2. 如果 Path 是本插件可识别的本地媒体路径、AList/OpenList 云端路径，或本地 `.strm` 文件内容指向云端路径/远程 URL，就把该源改成 DirectPlay/DirectStream，并设置 `DirectStreamUrl=/videos/{id}/stream?...`。
3. 拦截 `/videos/{id}/stream`、`/audio/{id}/stream`、`universal`、`master`、`main.m3u8`、`items/{id}/download` 请求。
4. 代理用 Emby PlaybackInfo 查到真实 Path，再复用插件的 AList/OpenList 解析逻辑拿 `raw_url` 或最终 CDN URL。
5. 成功解析时返回 302，让客户端直接访问云盘/CDN；解析失败、字幕、图片、管理接口等请求透明回源 Emby。

这个模式适合不想把 STRM 内容暴露给客户端、或想让 Emby App/Web 仍按正常 Emby 地址工作的场景。它不会代理视频数据；只有解析失败回源时才会走 Emby 原始播放链路。

可识别路径来源：

- Emby `MediaSources[].Path` 是本地媒体路径，且命中“上传映射”。
- Emby Path 本身就是 AList/OpenList 云端路径，且命中上传映射云端根、STRM 映射云端根或 `alist_target_path`。
- Emby Path 是 MoviePilot 进程可读取的 `.strm` 文件，文件第一行是云端路径或 `http/https` 远程 URL。
- `.strm` 第一行是远程 URL 时，代理会按“解析最终直链”配置跟随重定向，尽量把最终云盘/CDN URL 返回给客户端。

前置代理和轻量 `/redirect` 使用同一套 `direct_link_mode`、`resolve_final_url`、缓存和路径映射。`raw_url_only` 仍表示严格不回退 AList/OpenList `/d` 下载端点。

### `/redirect` 302 可靠性增强（v1.3.0）

- **直链来源策略**（`direct_link_mode`）：支持 `prefer_raw_url`、`raw_url_only`、`alist_download`，可在“兼容性”和“严格不走服务器上行”之间明确取舍。
- **缓存**：按 `(path, UA 哈希, 解析模式, 直链策略)` 缓存最终 URL，TTL 可配（默认 120s），避免缓存爆炸；若 URL query 中能识别到过期时间，会在过期前提前丢弃缓存。
- **in-flight 请求合并**：同 key 并发只解析一次，避免起播时多请求重复打 AList。
- **失败负缓存**：坏路径短 TTL（30s）内直接 502，避免疯狂请求。
- **HEAD 探测策略**（`head_probe_mode`）：
  - `ok`（默认兼容）：HEAD 返回 200，不跳转，兼容 Infuse/Fileball 先 HEAD 探测。
  - `redirect`（严格）：HEAD 同 GET 返回 302。
  - `resolve`（诊断）：HEAD 解析目标 URL 但返回 200，header 附带脱敏 `X-Resolved-Url`（仅 host）。
- **最终 URL 解析**（`resolve_final_url`）：HEAD 跟随上游重定向取最终 URL（≤10 跳，循环检测，多策略超时重试，HEAD 失败回退 GET Range `bytes=0-0`），最终失败回退原始 URL 不中断播放。
- **中文/空格路径兼容**：`/redirect` 会对 `path` 参数做 URL 解码，避免插件网关未解码时把 `%E4...` 当成字面云端路径。
- **/d 兜底**：兼容模式下如果 AList/OpenList `/api/fs/get` 失败，会回退生成 `/d/<path>` 下载地址；`raw_url_only` 严格模式仍会失败返回，避免走服务器下载端点。
- **日志脱敏**：日志不打印带 `sign`/`token` 的完整 URL，只保留 `scheme://host/path` 与 query key。
- **响应头**：302 时加 `Cache-Control: no-store`、`X-CloudStrm-Mode`、`X-CloudStrm-Link-Source`、`X-CloudStrm-Direct-Link`，不泄露真实 Token。

## UI

配置页按功能拆分为 4 个 Tab，每 Tab 一屏，避免长页面滚动查找：

1. **基础设置**：启用、任务完成通知、上传并发数、四个立即同步动作
2. **播放设置**：Emby 302 前置代理（启用开关、Emby 原始地址、代理监听地址、代理监听端口）
3. **路径设置**：云端存储设置（云存储类型、AList/OpenList 地址、Token）+ 上传与 STRM 路径映射（上传映射、STRM 映射）
4. **同步设置**：同步与过滤（同步模式、STRM 覆盖模式、媒体扩展名、排除规则、事件路径过滤）+ 媒体服务器刷新（刷新开关、媒体服务器选择、路径映射）

> STRM URL 模式、解析最终直链、直链来源策略、直链缓存时间、HEAD 探测策略等播放/302 相关参数已固定为内部默认值（`alist_direct` / `prefer_raw_url` / 缓存 120s / HEAD 返回 200 等），不再在设置页暴露；`raw_url` 实验模式也不对用户可见。这些值仍保留在代码内部逻辑中，`/redirect` 兼容端点与 Emby 302 前置代理照常工作。

同步模式：

- `复制`：上传到 AList/OpenList 后保留本地源文件。
- `移动`：上传且 STRM 生成成功后删除本地源文件；不会删除远端文件。

STRM 覆盖模式：

- `从不`：已有 STRM 直接跳过。
- `总是`：重新写入已有 STRM。

立即同步动作：

- `全量上传云端`：扫描全部候选媒体；云端父目录下已有同名文件时直接跳过，不触发上传接口，也不覆盖云端文件；成功后记录上传增量扫描基准。
- `增量上传云端`：有上传基准时只收集基准后新增/修改的媒体文件，再按远端目录缓存判断是否需要上传；没有基准时退回扫描全部候选。
- `全量生成 STRM`：扫描全部候选媒体，按当前 STRM 覆盖模式生成 `.strm`；成功后记录 STRM 增量扫描基准。
- `增量生成 STRM`：有 STRM 基准时只收集基准后新增/修改的媒体文件，再只生成本地缺失的 `.strm`；没有基准时退回扫描全部候选，不会因同步模式为“移动”而删除本地媒体文件。

## 首页统计面板

插件首页展示：

- 累计上传数量
- 最近上传时间
- 累计生成 STRM 数量
- 最近生成 STRM 时间

以及两个列表，每条记录右侧「操作」列只有一个「⋮」操作按钮，点击后展开下拉菜单显示该条记录可执行的操作（无需进配置页）：

- 最近上传列表（文件名 | 状态 | 大小 | 时间 | 云端路径 | 操作）
- 最近生成 STRM 列表（文件名 | 状态 | 时间 | STRM 路径 | 云端路径 | 操作）

最近上传列表的菜单项（按记录字段条件显示，无本地路径的记录不显示需要本地源的动作）：

- `重新上传`：先删除该条云端文件，再用本地源文件重新上传到 AList/OpenList 云端路径，并重新生成对应 STRM（仅 `已上传`/`远端已存在` 且有本地路径时可用）。
- `删除云端`：调用 AList/OpenList 接口删除该条云端文件，不动本地文件（warning 配色）。
- `删云端和本地`：先删除 AList/OpenList 云端文件，再删除上传映射本地根目录内的对应本地文件；云端删除失败会记录日志并提示，本地删除前校验路径在上传映射范围内（error 配色）。

最近生成 STRM 列表的菜单项：

- `重新生成 STRM`：调用后端 STRM 生成逻辑，针对该条记录按当前 STRM URL 模式重新写入 STRM 文件内容。
- `删除 STRM 文件`：删除本地 STRM 输出目录中的对应 .strm 文件，并从最近 STRM 列表中移除记录；删除失败提示错误并写入日志。

所有菜单项点击后通过 `POST /manual_action` API 在后台线程执行真实任务（重新上传/删除云端/删除本地/重新生成 STRM 均调用对应后端逻辑，非纯 UI 按钮），执行成功或失败都会通过 MoviePilot 消息渠道通知并在日志记录，统计列表随之刷新。破坏性操作（删除）用醒目配色 + 后端路径范围校验防误触；不再通过配置页保存触发手动动作。

状态：上传 `已上传`/`远端已存在`/`已删云端`/`已删本地`；STRM `新生成`/`已存在/已跳过`。统计通过插件数据持久化，重启后保留，并兼容旧 `recent_files` 结构迁移到 `recent_strms`。

每个列表标题右侧带有「清除上传历史」和「清除 STRM 历史」按钮，用于只清除插件记录的上传或 STRM 生成历史（**不删除任何真实文件**——不清除 OpenList/AList 云端文件、不清除本地媒体文件、不清除 .strm 文件）。清除操作不会影响当前正在执行的同步任务，仅移除统计数据中的最近记录列表。

> **操作区分提醒：**
> - 「清除上传历史」/「清除 STRM 历史」→ **只清除记录，不删任何文件**
> - 「删除云端」→ 真实删除 AList/OpenList 云端文件
> - 「删云端和本地」→ 真实删除云端文件 + 本地媒体文件
> - 「删除 STRM 文件」→ 真实删除本地 .strm 文件

## 安装

把 `plugins.v2/cloudstrmhelper/` 放入 MoviePilot 插件仓库，或复制到 MoviePilot 本地插件目录 `config/plugins/cloudstrmhelper/`，重启 MoviePilot 后安装启用。

依赖见 `requirements.txt`：

```text
requests
pathspec
cachetools
uvicorn
```

## 验证

1. 配置 AList/OpenList 地址、Token、上传映射、STRM 映射、媒体服务器路径映射。
2. 启用插件。SSE 监听默认关闭（UI 不再暴露此开关）；内部整理完成事件仍可兜底。如通过 API 手动开启 SSE，日志应出现 `【SSE监听】连接 MoviePilot 消息流`。若直接使用内部事件触发，确认日志出现 `Phase 1 完成`。
3. 让 MoviePilot 完成一次整理入库，日志应按顺序出现：
   - `Phase 1 完成`
   - `Phase 2 开始`
   - `Phase 2 完成`
   - `Phase 3 开始`
   - `Phase 3/4 完成`
4. 检查上传映射对应的 AList/OpenList 云端目录新增媒体文件。
5. 检查 STRM 输出目录新增 `.strm`，默认内容应以 AList/OpenList `/d/` 地址开头，并保留媒体文件后缀。
6. 检查 Emby/Jellyfin 是否扫描到新条目并能通过 302 播放。
7. 旧 `/redirect` STRM 或 Infuse/Fileball 的 HEAD 探测应按 `head_probe_mode` 返回（默认 200），GET 才 302。
8. 如启用 Emby 302 前置代理，把客户端服务器地址改为代理端口，访问 `/System/Info/Public` 应能正常回源，播放时代理日志应出现 `【Emby302代理】` 相关解析记录。

也可以在配置中勾选对应的立即同步动作；旧调用 `/api/v1/plugin/CloudStrmHelper/sync_now` 仍触发一次“全量扫描→增量上传→生成 STRM”的兼容同步。需要分离执行时可传 `action`：`upload_full`、`upload_incremental`、`strm_full`、`strm_incremental`。

手动全量/增量遇到大量文件时，插件会自动进入批量日志模式：

- 候选数超过 100 时，单文件入队、远端已存在跳过、上传完成等日志降级为 `debug`，避免 MoviePilot 日志面板被刷屏。
- 扫描、上传判定、上传队列等待、STRM 生成会按约 15 秒输出聚合进度，包含已处理数量、入队/跳过数量、等待/上传中/成功/失败数量和当前上传文件样本。
- 批次开始日志会带 `id` 和任务名；失败时结束日志会输出最多 10 条失败样本，便于定位具体云端路径和错误。

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/redirect` | `GET/HEAD` | STRM 播放用 302 跳转，参数 `apikey`、`path` |
| `/status` | `GET` | 查看同步队列状态 |
| `/diagnose` | `GET` | 查看脱敏配置、模块状态、302 状态、路径映射和统计 |
| `/diagnose?probe=true` | `GET` | 在诊断基础上只读探测 AList/OpenList Token/地址/fs_get（不输出 raw_url/sign 完整内容） |
| `/sync_now` | `GET/POST` | 手动触发同步；不带 `action` 为兼容全量扫描同步，`action` 支持 `upload_full`、`upload_incremental`、`strm_full`、`strm_incremental` |
| `/manual_action` | `POST` | 首页列表内单条操作，请求 body 为 JSON：`action`（`reupload`/`delete_remote`/`delete_remote_and_local`/`regenerate_strm`/`delete_strm`）、`local`、`remote`、`strm`；后台线程执行真实任务并返回 `{state, message}` |
| `/clear_upload_history` | `POST` | 清除最近上传历史记录（仅清除记录，不删除云端/本地文件） |
| `/clear_strm_history` | `POST` | 清除最近 STRM 生成历史记录（仅清除记录，不删除 .strm 文件） |

`/diagnose` 输出的 302 相关字段：`strm_url_mode`、`resolve_final_url`、`direct_link_mode`、`redirect_cache_ttl`、`head_probe_mode`、`redirect_cache_size`、`redirect_error_cache_size`、`emby_proxy_enabled`、`emby_proxy_running`、`emby_proxy_listen`。

## 安全约束

- 常规同步不删除远端文件；单条删除云端/本地操作由首页列表按钮触发，后端校验路径范围后才执行。
- 常规同步不覆盖远端已有文件；单条重新上传会先删除指定云端文件再上传。
- 单条删除本地文件会校验目标位于上传映射本地根目录内。
- STRM 是否覆盖由「STRM 覆盖模式」控制。
- Phase 1 不做任何文件系统或 AList 操作。
- 每个批次先完成 AList 同步，再生成 STRM 和刷新媒体库。
- Token 脱敏展示，日志不输出带 sign/token 的完整 URL。
