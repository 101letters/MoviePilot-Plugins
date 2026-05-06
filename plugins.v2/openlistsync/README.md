# OpenListSync

MoviePilot V2 插件：事件驱动的 OpenList/AList 目录同步工具。

## 特性

- **三种同步模式**：仅新增（0）、全同步/镜像（1）、移动（2）
- **事件驱动**：监听 `TransferComplete` 事件，整理完成后即时同步新增文件
- **立即同步**：支持手动触发全量扫描（按配置模式执行）
- **异步队列**：串行执行 + 自动重试 + 去重防重复
- **排除规则**：支持 `fnmatch` 通配符（如 `*.tmp`, `@eaDir/**`）
- **任务记录**：JSON 文件持久化，线程安全，自动裁剪历史
- **REST API**：完整的作业/任务 CRUD + 手动触发 + 全量同步 + 连通性测试
- **通知集成**：任务完成/失败时发送 MP 系统通知
- **Token 安全**：不输出到日志和 API 响应

## 同步模式

| 模式 | 名称 | 行为 |
|------|------|------|
| 0 | 仅新增 | src 有而 dst 没有或大小不同的 → copy |
| 1 | 全同步（镜像） | 模式 0 + 删除 dst 中 src 不存在的文件 |
| 2 | 移动 | copy → 验证 → 删除源文件 |

## 工作原理

```
MoviePilot 整理完成 → TransferComplete 事件
        ↓
解析事件数据（target_path, file_list_new）
        ↓
匹配 src_dir 前缀 → 找到对应作业
        ↓
入队 → 异步后台执行（不阻塞整理流程）
        ↓
按作业 sync_mode 同步：增量/镜像/移动
        ↓
任务记录 + 通知（可选）
```

**不再使用轮询** — 无定期扫描，不浪费系统资源。

## 配置

- **启用插件**
- **OpenList 地址**（如 `http://192.168.1.100:5244`）
- **OpenList Token**（登录后获取）
- **发送通知**
- **作业配置**（多行 JSON 文本框）
- **任务记录上限**（默认 100）

## 作业 JSON 格式

```json
[
  {
    "id": "movie_sync",
    "name": "电影同步",
    "src_dir": "/115/电影",
    "dst_dir": "/阿里云盘/电影",
    "sync_mode": 0,
    "exclude_rules": ["*.tmp", "@eaDir/**", "*.part", ".DS_Store"],
    "enabled": true
  }
]
```

> 事件驱动模式下不再需要 `interval_minutes`，同步由 TransferComplete 事件触发。
> 如需手动全量扫描，调用 `POST /jobs/{id}/sync`。

## API

所有接口路径前缀：`/api/v1/plugin/OpenListSync/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 插件状态 + 队列运行状态 |
| GET | `/jobs` | 作业列表 |
| POST | `/jobs` | 创建作业 |
| GET | `/jobs/{id}` | 作业详情 |
| PUT | `/jobs/{id}` | 更新作业 |
| DELETE | `/jobs/{id}` | 删除作业 |
| POST | `/jobs/{id}/run` | 手动执行（事件模式，需传 `event_path`） |
| POST | `/jobs/{id}/sync` | **立即全量同步**（按作业配置模式执行） |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情 |
| POST | `/test_connection` | 测试 OpenList 连通性 |

## 目录结构

```
plugins.v2/openlistsync/
├── __init__.py       ← MP 插件入口 + API 路由 + 事件监听
├── client.py         ← OpenList API 封装
├── engine.py         ← 三种同步模式 + 事件同步
├── job_manager.py    ← 作业 CRUD
├── task_manager.py   ← 任务记录持久化
├── queue.py          ← 异步队列 + 重试 + 去重
└── data/
    └── tasks.json    ← 任务历史存储
```
