# OpenList 文件转运插件

整理完成后自动触发 `TransferComplete` 内部事件，调用 OpenList API 执行 **copy**，并通过 MoviePilot 通知系统发送结果通知。

## 功能

- 监听 MoviePilot 内部 `TransferComplete` 事件
- 支持电影 / 剧集分类目标目录
- 支持本地路径到 OpenList 路径映射
- 支持自动创建目标目录
- 支持通知
- 支持可视化配置

## 说明

`TransferComplete` 是 MoviePilot 宿主内部事件，不属于公开 REST API，所以你在 API 文档里看不到它，这属于正常现象。

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

### API 路径

默认：

- copy: `/api/fs/copy`
- mkdir: `/api/fs/mkdir`

如果你的 OpenList 版本接口不同，直接改配置即可。

## copy 请求兼容

插件会尝试几种常见 payload 格式调用 copy API，以兼容不同 OpenList / AList 风格接口。

## 通知内容

- 媒体名
- 源文件
- 源目录
- 目标目录
- 动作（copy）
- 执行结果
- 错误详情 / 返回信息
