# OpenList 文件转运插件

整理完成后自动触发 `TransferComplete` 内部事件，调用 OpenList API 执行 **copy**，并通过 MoviePilot 通知系统发送结果通知。

## 功能

- 监听 MoviePilot 内部 `TransferComplete` 事件
- 支持电影 / 剧集分类目标目录
- 支持本地路径到 OpenList 路径映射
- 支持自动创建目标目录
- 支持通知
- 支持可视化配置
- **OpenList/AList 调用方式参考 taosync**

## 说明

`TransferComplete` 是 MoviePilot 宿主内部事件，不属于公开 REST API，所以你在 API 文档里看不到它，这属于正常现象。

## OpenList 调用方式

当前实现参考 `dr34m-cn/taosync` 的 AList/OpenList 调用方式：

- 请求头：`Authorization: <token>`
- `mkdir`：`POST /api/fs/mkdir`，body：`{"path":"/目标目录"}`
- `copy`：`POST /api/fs/copy`，body：

```json
{
  "src_dir": "/源目录",
  "dst_dir": "/目标目录",
  "overwrite": true,
  "names": ["文件名.mkv"]
}
```

- 响应判定：HTTP 200 且 JSON `code == 200` 视为成功

## 推荐配置

### 路径映射

每行一条：

```text
/mnt/media=/
/mnt/media/电影=/movies_src
/mnt/media/剧集=/tv_src
```

含义：把 MoviePilot 整理后的本地路径前缀映射成 OpenList 里可识别的源路径前缀。

### 目标目录

- 电影目标根目录：`/movies`
- 剧集目标根目录：`/tv`
