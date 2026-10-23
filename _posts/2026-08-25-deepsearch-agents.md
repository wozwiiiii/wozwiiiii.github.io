---
layout: corridor-post
title: deepsearch — 深度探索系统
description: 基于开源教学项目做生产化改造，覆盖路径穿越、SQL任意执行、横向越权等14类安全问题
date: 2026-08-25
period: 2026年5月-6月
permalink: /blog/2026/08/25/deepsearch-agents/
categories: [项目]
tags: [Agent, LangGraph, 安全, FastAPI, 生产化改造]
---

## 项目背景

**诚实表述：** 本仓库基于开源教学项目deepsearch-agents（MIT协议，原始教学代码来自[didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)）。正确定位是：**基于开源教学项目做了生产化改造**，覆盖路径穿越、SQL任意执行、横向越权、无认证、租户串台、无限流等14类问题，累计30笔提交、87个文件、+15165/−580行生产化改造，220个回归测试，而不是把整个项目说成从零自研。

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

- 双层预算熔断：单任务 80 次调用 + 150 万 token，会话级 450 万 token 跨任务累计——提示注入或规划失控不会再空转烧钱
- 超限抛异常由`run_deep_agent`捕获并经monitor告知前端

**Checkpointer持久化：**

| 阶段 | 实现 | 用途 |
|---|---|---|
| 原版 | `InMemorySaver` | 重启丢全部上下文 |
| 已落地 | `AsyncSqliteSaver` | 持久化到checkpoints.sqlite3 |
| Postgres 版 | `AsyncPostgresSaver` | 多副本水平扩容，与内存版/SQLite 版可切换，接口不变 |

**最终回答守卫拦截早停：** 主智能体可能在子任务未完成时提前输出最终回答（搜索还没跑完就说"根据以上信息……"）。守卫用三条件 AND 高精度判据拦截：是否所有子任务完成、是否有足够信息、是否达到最小执行轮数。实战 7 次触发 7 次救回、零误伤——没有误拦过正常的最终回答。

**ContextVar 隐式上下文传递：** 向深层工具隐式注入会话目录与路由键，每次任务动态拼装工作环境指令；异步环境中任务隔离，不需要每层函数显式传参。

**多租户隔离根：** 复合键`{user_id}-{thread_id}`是task表主键、WS路由键、checkpointer thread_id的统一根。user_id限纯小写字母数字保证复合键无歧义（若允许连字符，user_id="a-b" + thread_id="c" 会与 user_id="a" + thread_id="b-c" 冲突）。跨租户越权经专项测试验证均 404（bob 持自己密钥 + 相同 thread_id 访问 alice 文件，列表与下载均 404）。

## 任务出进程与可靠性设计

**任务出进程：** Agent 执行从 API 进程内迁出到独立 worker——ARQ Redis 队列 + PostgreSQL 任务表 + 独立 worker 进程。API 只负责接收请求和推送进度，长任务不再阻塞事件循环。保留 inline / redis 双模式开关，默认 inline 零依赖（开发测试用），生产用 redis。

- **状态机三层守卫**：终态不可覆盖、worker 代际校验、FOR UPDATE 原子取消，防并发竞态
- **事件回放断线不丢结果**：事件先落库（自增 seq 序号）再推送，WebSocket 断线重连按 last_seq 差量补发——相当于视频网站的"续播"
- **600 秒任务硬超时**：asyncio.wait_for，防止任务永久"运行中"

**线程池耗尽真因定位：** 最初怀疑同步工具阻塞事件循环，写并发探针实测证伪；继续排查发现真因是线程池耗尽——协程可以取消，但底层线程在执行阻塞调用时不可中断，超时任务占满线程池后新任务无法执行。用 SDK 原生超时参数修复，零新依赖。

## 评测体系与评测驱动迭代

**自建 45 条评测集**：SQL 事实问答（22 条）、任务路由（6 条）、网页检索（10 条）、多源综合（7 条）四类；判分 = 确定性子串判分 + LLM-as-judge 混合。

**四轮全量评测通过率 66.7% → 80.0% → 88.9% → 91.1%（30/45 → 41/45）**，其中路由、网页、多源三类任务达到满分。

- **污染样本单独标记**：限流或外部服务故障导致的评测失败单独标记留证、不计入结论——模型答错与环境故障必须区分，否则评测结论不可信
- **文件生成意图硬约束**：45 条 query 零误拒，合法请求放行双向验证
- **质量保障**：220 个 pytest 用例覆盖校验与隔离逻辑（mock 替换，无需真实 LLM/MySQL，本地约 10 秒跑完）；trace_id 结构化日志贯穿各层，生产路径 print 清零；P0–P3 四级缺陷清单近 30 项分批消化；两次代码审查发现并修复 7 个自身工程问题（UNION 漏补 LIMIT、非 ASCII 密钥 500、测试污染运行时目录等）

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

220个回归测试，本地约10秒跑完（mock掉`run_deep_agent`，纯打校验逻辑，不需要任何真实LLM/MySQL）。评测驱动迭代后测试规模从129扩至220：

| 测试文件 | 用例数 | 覆盖领域 |
|---|---|---|
| test_path_safety.py | 14 | 路径穿越防御 |
| test_sql_guard.py | 45 | SQL只读 + 自动LIMIT + UNION |
| test_api_security.py | 31 | 上传安全 + 会话隔离 + 回滚 |
| test_auth.py | 28 | API Key认证 + fail-closed + 非ASCII |
| test_rate_limit.py | 7 | slowapi限流 |
| test_checkpointer.py | 4 | SQLite持久化 + middleware透传 |
| **合计（评测迭代后扩至）** | **220** | — |

**完成状态：** 三批生产化改造（安全加固→认证与状态→限流与成本）+ 正式代码审查修复 + 任务出进程改造（ARQ/Postgres/事件回放/硬超时）+ 评测驱动四轮迭代全部落地。

## 如实声明的已知限制

1. **未部署上线**：项目代码完成，已在真实 LLM + MySQL 环境完成四轮全量评测，但未部署上线、无真实用户；下一步是部署上线与真实用户验证
2. **CI/CD**：`.github/workflows/ci.yml` 已提交（pytest + 前端 tsc 检查）；后端无Dockerfile
3. **内容安全**：ClamAV、模型输出审核、提示注入防护未做
4. **认证升级**：API Key适合个人/小团队，对外多用户需换OIDC+JWT

## GitHub

[wozwiiiii/deepsearch-agent-try](https://github.com/wozwiiiii/deepsearch-agent-try)
