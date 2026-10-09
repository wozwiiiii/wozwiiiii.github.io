---
layout: post
title: deepsearch — 深度探索系统
description: 基于开源教学项目做生产化改造，覆盖路径穿越、SQL任意执行、横向越权等14类安全问题
date: 2026-08-25
period: 2026年5月-6月
permalink: /blog/2026/08/25/deepsearch-agents/
categories: [项目]
tags: [Agent, LangGraph, 安全, FastAPI, 生产化改造]
image: /assets/img/posts/deepsearch-agents/cover.jpg
---

## 项目背景

**诚实表述：** 本仓库基于开源教学项目deepsearch-agents（MIT协议，原始教学代码来自[didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)）。正确定位是：**基于开源教学项目做了生产化改造**，覆盖路径穿越、SQL任意执行、横向越权、无认证、租户串台、无限流等14类问题，129个回归测试，而不是把整个项目说成从零自研。

**解决什么问题：** 把"用户自然语言问题"调度到多来源工具（公开网络、MySQL业务库、RAGFlow知识库、文件工具）协同检索，并实时回传执行进度，最终以Markdown/PDF交付报告。

## 架构总览

五层架构：

```
1. 用户交互层    React + Vite前端 | HTTP命令通道 | WebSocket进度通道
                    ↓ HTTP发命令，WebSocket回进度
2. API与实时通信层  FastAPI server.py | /api/task + active_tasks | ConnectionManager
                    ↓ 只做轻量调度，长耗时任务交给后台协程
3. DeepAgents调度层  main_agent.py: create_deep_agent + run_deep_agent(astream)
                    ContextVar: thread_id / session_dir | monitor.py
                    ↓ 主智能体判断信息缺口，子智能体只处理自己信息来源
4. 子智能体与工具层  网络搜索(Tavily) | 数据库查询(MySQL) | RAGFlow助手 | 文件交付工具
                    ↓
5. 数据源与产物层  公开网页 | MySQL业务库 | RAGFlow知识库 | output: MD/PDF
```

## 技术选型理由

### LangGraph 作为Agent编排框架

框架层面采用LangGraph，可由`langgraph-checkpoint-sqlite`包名与`AsyncSqliteSaver`类名直接佐证。

**选型理由**（文档原文："当初选可替换saver的回报"）：checkpointer接口可替换是选型时的重要考量。单机用SQLite，多副本时换Postgres（`langgraph-checkpoint-postgres`），接口不变只换saver实现，业务代码零改动。

### ARQ而非Celery做任务队列

项目强调`run_deep_agent`为async协程，并以此为理由选用ARQ——ARQ原生async、与FastAPI同事件循环模型；Celery的同步模型会让async Agent退化成线程池调用，丢掉并发优势。Temporal引入整套server过重，同样排除。

### fail-closed而非fail-open

未配置`API_KEYS`时业务接口默认返回**503**（原版fail-open裸奔）；仅显式`ALLOW_DEV_MODE=1`才进入开发模式；`ALLOW_DEV_MODE=true`等"看似开启"的值一律拒绝。忘配密钥时服务不能照常裸奔。

## 核心数据流

### 主流程

```
前端提交任务（query 1-10000字符，Pydantic校验）
  ▼
X-API-Key认证（compare_digest常数时间比较）→ 绑定Principal.user_id
  ▼
slowapi限流（按密钥SHA-256截断16位计配额，401不消耗配额）
  ▼
FastAPI后台协程 asyncio.create_task → run_deep_agent
  ▼
DeepAgents调度层:
  → 创建会话目录 output/user_{uid}/session_{tid}
  → ContextVar透传 thread_id / session_dir
  → astream异步流式执行，monitor上报 assistant/tool/result 三类事件
  → WebSocket实时回传进度
  ▼
Markdown/PDF交付（文件接口改为thread_id驱动，服务端拼接路径）
```

### Harness层面设计

**惰性初始化：** Agent改为惰性初始化（首次任务时建库建连接并缓存复用），避免模块导入即占连接。服务重启后同一复合thread_id自动恢复历史消息与执行状态。

**模型调用上限中间件：**

```python
ModelCallLimitMiddleware(run_limit=80, thread_limit=300, exit_behavior="error")
```

- 单次任务最多80次模型调用、单会话（跨重启累计）最多300次
- 超限抛异常由`run_deep_agent`捕获并经monitor告知前端
- **诚实声明边界**：是调用次数上限，不是token预算；token级熔断仍是待做项

**Checkpointer持久化：**

| 阶段 | 实现 | 用途 |
|---|---|---|
| 原版 | `InMemorySaver` | 重启丢全部上下文 |
| 已落地 | `AsyncSqliteSaver` | 持久化到checkpoints.sqlite3 |
| 设计稿 | `AsyncPostgresSaver` | 多副本水平扩容，接口不变 |

**多租户隔离根：** 复合键`{user_id}-{thread_id}`是task表主键、WS路由键、checkpointer thread_id的统一根。user_id限纯小写字母数字保证复合键无歧义。文档原话："复合键不动……这是多租户隔离的根，改它等于推倒重来。"

## 防护措施（14类问题修复）

### 路径穿越防御（`app/utils/path_utils.py`重写）

- 所有路径解析结果强制**收容（containment）**在`session_dir`内，越界抛`PathEscapeError`
- 绝对路径（Windows盘符/Unix根路径）一律拒绝
- 三个文件工具捕获`PathEscapeError`返回**引导性错误文本，模型可自我纠正重试，不中断Agent循环**
- 验证：`tests/test_path_safety.py` 14个用例

### SQL只读三件套（`app/tools/db_tools.py`）

```python
# 1. assert_readonly_sql: 剥离注释后仅放行SELECT/SHOW/DESCRIBE/EXPLAIN
#    多语句(stacked queries)、INTO OUTFILE/DUMPFILE、LOAD_FILE、DML/DDL一律拒绝
#    被拒SQL不建立数据库连接，直接返回错误文本

# 2. validate_table_name: 表名白名单 ^[A-Za-z0-9_]{1,64}$ + 反引号包裹

# 3. enforce_select_limit: sqlglot解析SELECT，无LIMIT自动追加LIMIT 1000
#    UNION等集合操作也补LIMIT；解析失败抛SQLSafetyError，不执行
```

验证：`tests/test_sql_guard.py` 45个用例。

**账号层兜底**：MySQL只读账号`deepsearch_ro@%`，仅`GRANT SELECT`。文档原文："工具层校验是第一道防线，数据库最小权限是第二道。"

### 横向越权 + 多租户隔离

- `/api/files`、`/api/download`原接受任意绝对路径 → 改为thread_id驱动，服务端拼接路径
- 目录结构改为`output/user_{uid}/session_{tid}`，天然隔离会话

### 限流（slowapi）

- `POST /api/task`限10/min，`POST /api/upload`限30/min
- 限流键按**密钥SHA-256截断16位**（明文不入限流状态与日志）
- 认证先于限流执行，401请求不消耗配额

### 代码审查修复四项

| # | 问题 | 修复 |
|---|---|---|
| R-1 | 上传无总量上限 | 请求级100MB + 文件数20，写盘前拒绝 |
| R-2 | 多文件非原子 | `_rollback_saved`：任一失败清理已写入全部文件 |
| R-3 | 非ASCII密钥触发TypeError→500 | 非ASCII密钥直接401 |
| R-4 | 测试写真实运行时目录 | autouse fixture指向tmp_path |

### 错误返回原版本机制汇总

| 场景 | 行为 |
|---|---|
| 路径越界 | 抛PathEscapeError，工具层捕获返回引导性文本，Agent自纠重试 |
| SQL校验失败 | 不建数据库连接，直接返回错误文本 |
| SQL解析失败 | SQLSafetyError，不执行 |
| 模型调用超限 | exit_behavior="error"抛异常，run_deep_agent捕获经monitor告知前端 |
| 多文件上传失败 | _rollback_saved清理已写入全部文件 |

## 测试与完成状态

129个回归测试，6秒全绿（mock掉`run_deep_agent`，纯打校验逻辑，不需要任何真实服务）：

| 测试文件 | 用例数 | 覆盖领域 |
|---|---|---|
| test_path_safety.py | 14 | 路径穿越防御 |
| test_sql_guard.py | 45 | SQL只读 + 自动LIMIT + UNION |
| test_api_security.py | 31 | 上传安全 + 会话隔离 + 回滚 |
| test_auth.py | 28 | API Key认证 + fail-closed + 非ASCII |
| test_rate_limit.py | 7 | slowapi限流 |
| test_checkpointer.py | 4 | SQLite持久化 + middleware透传 |
| **合计** | **129** | — |

**完成状态：** 三批生产化改造（安全加固→认证与状态→限流与成本）+ 一次正式代码审查修复全部落地。

## 如实声明的已知限制

1. **任务队列**：当前`asyncio.create_task`进程内执行，重启即丢。ARQ+Redis+Postgres设计稿已写但未实现
2. **可观测性**：现状只有`print`，没有结构化日志/指标/告警
3. **CI/CD**：`.github/workflows/ci.yml`已创建文件但未提交；后端无Dockerfile
4. **内容安全**：ClamAV、模型输出审核、提示注入防护未做
5. **认证升级**：API Key适合个人/小团队，对外多用户需换OIDC+JWT
6. **token级预算熔断**：当前只有调用次数上限，无token累计熔断

## GitHub

[wozwiiiii/hello-agents-my-build](https://github.com/wozwiiiii/hello-agents-my-build.git)
