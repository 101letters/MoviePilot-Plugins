# OpenList 文件转运插件

整理完成后自动触发 `TransferComplete` 内部事件，插件将任务入队并节流下发给 OpenList 内部 copy 任务系统执行传输。

## 核心模型

- MoviePilot 只负责触发事件
- OpenList 负责真正的文件复制
- 插件负责：入队、去重、节流、查重、下发任务、轮询状态、通知

## 当前默认规则

- MP 相对根路径：`/media`
- OpenList 源前缀：`/影视库`
- OpenList 目标前缀：`/目标目录/影视库`

例如：

- MP 整理后文件：`/media/华语电影/xxx/abc.mkv`
- 相对目录：`华语电影/xxx`
- OpenList 源目录：`/影视库/华语电影/xxx`
- OpenList 目标目录：`/目标目录/影视库/华语电影/xxx`
- 文件名：`abc.mkv`

## 去重与节流

- 事件去重：相同 `src_dir + dst_dir + name` 在时效内不会重复入队
- 目标查重：下发前会调用 `/api/fs/list` 检查目标目录是否已存在同名文件
- 节流：队列串行处理，可设置入队延迟与下发间隔

默认值：

- 入队延迟：5 秒
- 下发间隔：3 秒
- 去重时效：1800 秒

## OpenList 调用方式（参考 taosync）

- `Authorization: <token>`
- `POST /api/fs/mkdir` body: `{ "path": "/目标目录" }`
- `POST /api/fs/list` body: `{ "path": "/目标目录", "refresh": true }`
- `POST /api/fs/copy` body:

```json
{
  "src_dir": "/源目录",
  "dst_dir": "/目标目录",
  "overwrite": true,
  "names": ["文件名.mkv"]
}
```

- `POST /api/admin/task/copy/info?tid=...` 轮询任务状态
- 返回 JSON 中 `code == 200` 才算成功
