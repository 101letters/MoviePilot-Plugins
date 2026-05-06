# OpenListCopy

MoviePilot V2 插件：监听整理完成事件，通过 OpenList/AList 内部 `POST /api/fs/copy` 把整理后的整个媒体文件夹复制到 123 云盘等目标存储。

## 特性

- 监听 `TransferComplete` 事件。
- 从 `transferinfo.target_diritem` 提取整理后的目标路径。
- 按 9 类默认路径映射生成 OpenList copy 参数。
- 复制整个媒体文件夹，不逐文件上传，不占用 MP 上传带宽。
- 目标已存在自动跳过。
- 事件回调只入队，后台单 worker 异步处理。
- 同 `src_dir + dst_dir + name` 去重。
- 失败自动重试，支持失败通知。
- 不删除本地文件。

## 默认映射

```json
[
  {"category":"外语电影","mp_dir":"/media/外语电影","src_dir":"/影视库/外语电影","dst_dir":"/123云盘/影视/外语电影"},
  {"category":"动画电影","mp_dir":"/media/动画电影","src_dir":"/影视库/动画电影","dst_dir":"/123云盘/影视/动画电影"},
  {"category":"华语电影","mp_dir":"/media/华语电影","src_dir":"/影视库/华语电影","dst_dir":"/123云盘/影视/华语电影"},
  {"category":"纪录片","mp_dir":"/media/纪录片","src_dir":"/影视库/纪录片","dst_dir":"/123云盘/影视/纪录片"},
  {"category":"国产剧","mp_dir":"/media/国产剧","src_dir":"/影视库/国产剧","dst_dir":"/123云盘/影视/国产剧"},
  {"category":"欧美剧","mp_dir":"/media/欧美剧","src_dir":"/影视库/欧美剧","dst_dir":"/123云盘/影视/欧美剧"},
  {"category":"日韩剧","mp_dir":"/media/日韩剧","src_dir":"/影视库/日韩剧","dst_dir":"/123云盘/影视/日韩剧"},
  {"category":"动漫","mp_dir":"/media/动漫","src_dir":"/影视库/动漫","dst_dir":"/123云盘/影视/动漫"},
  {"category":"综艺","mp_dir":"/media/综艺","src_dir":"/影视库/综艺","dst_dir":"/123云盘/影视/综艺"}
]
```

示例：

```text
MP 路径：/media/华语电影/"大"人物 (2019)/
OpenList copy:
{
  "src_dir": "/影视库/华语电影",
  "dst_dir": "/123云盘/影视/华语电影",
  "names": ["\"大\"人物 (2019)"]
}
```

## 配置

- 启用插件
- OpenList 地址
- 用户名/密码或固定 Token
- 目标已存在跳过
- 重试次数、重试间隔
- 9 类路径映射 JSON

