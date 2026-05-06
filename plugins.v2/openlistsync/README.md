# OpenListSync

MoviePilot V2 插件：定时自动同步 OpenList/AList 目录。

## 特性

- **三种同步模式**：仅新增、全同步（镜像）、移动
- **定时调度**：全局扫描间隔 + 每作业独立间隔
- **防并发**：同一作业同时只允许一个任务运行
- **排除规则**：支持 `fnmatch` 通配符（如 `*.tmp`, `@eaDir/**`）
- **任务历史**：JSON 文件持久化，可设置保留条数上限
- **REST API**：完整的作业/任务 CRUD + 手动触发 + 连通性测试
- **通知集成**：任务完成/失败时发送 MP 系统通知
- **Token 安全**：不输出到日志和 API 响应

## 同步模式

| 模式 | 名称 | 行为 |
|------|------|------|
| 0 | 仅新增 | src 有而 dst 没有或大小不同的 → copy |
| 1 | 全同步 | 模式 0 + 删除 dst 中 src 不存在的文件 |
| 2 | 移动 | src → copy → 验证 → 删除源文件 |

## 配置

- **启用插件**
- **OpenList 地址**（如 `http://192.168.1.100:5244`）
- **OpenList Token**（登录后获取）
- **发送通知**
- **全局扫描间隔**（秒，默认 60）
- **作业配置**（多行 JSON 文本框）
- **任务记录上限**（默认 100）

## 作业 JSON 格式

```json
[
  {
    "id": "job_001",
    "name": "电影同步",
    "src_dir": "/115/电影",
    "dst_dir": "/阿里云盘/电影",
    "sync_mode": 0,
    "exclude_rules": ["*.tmp", "@eaDir/**", "*.part", ".DS_Store"],
    "interval_minutes": 30,
    "enabled": true
  }
]
```

## API

所有接口路径前缀：`/api/v1/plugin/OpenListSync/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 插件状态 |
| GET | `/jobs` | 作业列表 |
| POST | `/jobs` | 创建作业 |
| GET | `/jobs/{id}` | 作业详情 |
| PUT | `/jobs/{id}` | 更新作业 |
| DELETE | `/jobs/{id}` | 删除作业 |
| POST | `/jobs/{id}/run` | 手动执行 |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情 |
| POST | `/test_connection` | 测试连通性 |

## 目录结构

```
plugins.v2/openlistsync/
├── __init__.py       ← MP 插件入口 + API 路由
├── client.py         ← OpenList API 封装
├── engine.py         ← 三种同步模式执行逻辑
├── job_manager.py    ← 作业 CRUD
├── task_manager.py   ← 任务记录持久化
├── scheduler.py      ← 后台线程定时调度
└── data/
    └── tasks.json    ← 任务历史存储
```
