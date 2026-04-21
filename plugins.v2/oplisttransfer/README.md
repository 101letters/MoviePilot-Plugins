# OpenList 文件转运插件

监听 MoviePilot 的整理完成事件，将文件传输任务排队后交给 OpenList 内部 copy 执行。

## 设计原则

- MP 只负责触发
- OpenList 负责真正 copy
- 仅新增：目标目录已存在同名文件则跳过
- 用队列做节流，避免短时间大量调用 OpenList API

## 推荐配置风格

参考 taosync 的作业概念：

- 引擎：OpenList 地址
- 源目录：`/影视库/`
- 目标目录：`/123云盘/影视/`
- 作业名称：`影视库`
- 同步方法：仅新增
- 目标目录扫描缓存：使用
- 目标目录操作间隔：1 秒
- 源目录扫描缓存：不使用
- 源目录操作间隔：0 秒
- 排除项：按行填写

## 路径规则

从 MP 整理路径中截取 `/media` 后面的相对路径：

- MP 文件：`/media/华语电影/xxx/abc.mkv`
- 相对目录：`华语电影/xxx`
- OpenList 源目录：`/影视库/华语电影/xxx`
- OpenList 目标目录：`/123云盘/影视/华语电影/xxx`
- 文件名：`abc.mkv`

## 固定内部 API

插件内部固定使用：

- `/api/fs/list`
- `/api/fs/mkdir`
- `/api/fs/copy`
- `/api/admin/task/copy/info`

这些不对外展示，不需要手动修改。
