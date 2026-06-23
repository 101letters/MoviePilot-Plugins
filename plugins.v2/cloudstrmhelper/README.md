# 云端STRM整理助手 (CloudStrmHelper)

MoviePilot V2 插件：监听 MoviePilot 整理完成/入库完成消息，按顺序上传到 AList，基于云端路径生成 STRM，并刷新 Emby/Jellyfin 入库。

## 执行链路

```text
Phase 1 监听事件
  GET /api/v1/system/message
  只记录整理完成/入库完成事件与路径，不做文件操作
        |
        v
Phase 2 AList 同步
  本地媒体路径 -> AList 上传目标根目录
  SSE 如只记录到目录，在本阶段展开目录内媒体文件
  仅新增，远端已存在即跳过，永不删除远端文件
        |
        v
Phase 3 STRM 生成
  按“本地媒体库路径#STRM输出目录”映射生成 STRM
  STRM 内容为 MoviePilot /redirect 302 URL
  STRM 覆盖模式可选“从不/总是”
        |
        v
Phase 4 媒体库刷新
  按路径映射刷新 Emby/Jellyfin
```

内部 `EventType.TransferComplete` 仍作为兜底入口，但同样只进入 Phase 1 记录，再按上述批次顺序执行。

## 默认配置

| 配置项 | 默认值 |
|---|---|
| MoviePilot 内网地址 | `http://192.168.31.6:3000` |
| 云存储类型 | `alist` |
| AList 地址 | `http://192.168.31.6:5244/` |
| AList 上传目标根目录 | `/123云盘/影视/华语电影` |
| 本地与 STRM 路径映射 | `/media/movies#/strm/test/华语电影`、`/media/tv#/strm/test/电视剧` |
| 同步模式 | 复制 |
| STRM 覆盖模式 | 从不 |
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

STRM 内容格式：

```text
http://<MoviePilot内网地址>/api/v1/plugin/CloudStrmHelper/redirect?apikey=<API_TOKEN>&path=<AList路径>
```

播放时插件 `/redirect` 端点调用 AList `FsGet`，优先取 `raw_url`，否则构造 `/d/<path>?sign=...`，再返回 302。

## UI

配置页按以下顺序展示：

1. 基础设置
2. 云端（AList）设置
3. 本地与 STRM 路径
4. 同步与过滤
5. 媒体服务器设置

同步模式：

- `复制`：上传到 AList 后保留本地源文件。
- `移动`：上传到 AList 且 STRM 生成成功后删除本地源文件；不会删除远端文件。

STRM 覆盖模式：

- `从不`：已有 STRM 直接跳过。
- `总是`：重新写入已有 STRM。

插件首页展示实时统计：

- 已生成 STRM 数量（累计）
- 最近一次 STRM 生成时间
- 最近入库文件（文件名 + 时间）

统计通过插件数据持久化，重启后保留。

## 安装

把 `plugins.v2/cloudstrmhelper/` 放入 MoviePilot 插件仓库，或复制到 MoviePilot 本地插件目录 `config/plugins/cloudstrmhelper/`，重启 MoviePilot 后安装启用。

依赖见 `requirements.txt`：

```text
requests
pathspec
cachetools
```

## 验证

1. 配置 AList 地址、Token、AList 上传目标根目录、本地与 STRM 路径映射、媒体服务器路径映射。
2. 启用插件，观察日志出现 `【SSE监听】连接 MoviePilot 消息流`。
3. 让 MoviePilot 完成一次整理入库，日志应按顺序出现：
   - `Phase 1 完成`
   - `Phase 2 开始`
   - `Phase 2 完成`
   - `Phase 3 开始`
   - `Phase 3/4 完成`
4. 检查 AList 目标目录新增媒体文件。
5. 检查 STRM 输出目录新增 `.strm`，内容为 MoviePilot 302 URL。
6. 检查 Emby/Jellyfin 是否扫描到新条目并能通过 302 播放。

也可以在配置中勾选“立刻全量同步”，或调用 `/api/v1/plugin/CloudStrmHelper/sync_now` 触发一次全量同步。

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/redirect` | `GET/HEAD` | STRM 播放用 302 跳转 |
| `/status` | `GET` | 查看同步队列状态 |
| `/diagnose` | `GET` | 查看脱敏配置、模块状态、路径映射和统计 |
| `/diagnose?probe=true` | `GET` | 在诊断基础上只读探测 AList Token/地址 |
| `/sync_now` | `GET/POST` | 手动触发一次全量同步 |

## 安全约束

- 不删除远端文件。
- 不覆盖远端已有文件。
- STRM 是否覆盖由“STRM 覆盖模式”控制。
- Phase 1 不做任何文件系统或 AList 操作。
- 每个批次先完成 AList 同步，再生成 STRM 和刷新媒体库。
