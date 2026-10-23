---
layout: corridor-post
title: 灵犀语伴 — AI语音应用
description: 面向教师与教培机构的AI语音课件制作系统，基于服务外包大赛全国三等奖项目重构
date: 2026-08-26
period: 2026年3月-4月
permalink: /blog/2026/08/26/lingxi-voice/
categories: [项目]
tags: [AI语音, TTS, FastAPI, CosyVoice2, Vue3]
---

## 项目背景

第16届中国大学生服务外包创新创业大赛，赛题A09"基于AI语音合成的教学声音处理软件"，企业命题方锐捷网络。团队"遥遥领先才队"获全国三等奖。

当前代码库为基于大赛文档的**重构版本**。原项目存在文档技术描述与模型实际机制不符、工程化底座缺失等问题，本次重构的核心增量在于工程化与安全加固。

**解决什么问题：** 现有AI配音工具（讯飞智作、魔音工坊、ElevenLabs等）只解决"文字变声音"，不深度适配教学场景——不理解PPT结构、不生成教学讲稿、不管理逐页音频、不处理数学/化学符号的语音转译。

## 架构总览

四进程架构：

```
浏览器（Vue 3 SPA）
  │ HTTPS
  ▼
Nginx（静态资源/反向代理，client_max_body_size 20M，limit_req 10r/s）
  ▼
FastAPI（port 8000） ──▶ Celery Worker（批量合成/克隆预处理）
  │                         │ progress → Redis → SSE
  ├── PostgreSQL 16
  └── Redis 7
       │
       └── TTSProvider 抽象
            ├── LocalCosyVoiceProvider → WSL2 GPU推理服务（RTX 4060, port 8001）
            └── SiliconFlowProvider    → 云CosyVoice API
```

## 技术选型理由

### CosyVoice2 作为TTS模型

选CosyVoice2-0.5B而非其他，基于三个可验证的技术事实：

1. **零样本克隆**：3-10秒参考音频直接prompt，不需要自建声纹提取管线（原项目文档误描述为MFCC/LPC提取，重构时纠正）
2. **instruct指令模式**：16种情感 + 11种风格，通过标记控制语气（`[laughter]`、`<strong>`等），适合教学场景的"严肃课堂""活泼讲解"切换
3. **开源可商用**：Apache-2.0协议，本地部署不依赖外部API

### 双Provider策略模式

`TTSProvider`抽象基类 + `LocalCosyVoiceProvider`（本地GPU）与`SiliconFlowProvider`（云API）双实现。这不是简单的"本地+云"切换，而是基于以下工程考量：

- **能力探测**：`ProviderCapabilities`驱动前端动态显示/隐藏控件——本地版情感选择器16项，云版只有8项，前端按实际能力渲染
- **降级链**：`TTS_PROVIDER_CHAIN`配置决定调用顺序，链路首个非熔断Provider被选中
- **熔断器**：CLOSED→连续失败≥5→OPEN（30s快速失败）→HALF_OPEN（探测`/health`）→成功CLOSED/失败OPEN，状态存Redis多实例共享

### SSE替代WebSocket

任务进度是单向推送（后端→前端）。SSE满足需求且更简单——自动重连、Nginx无特殊配置、走HTTP/1.1。WebSocket的双向能力在此场景是过度设计。

## 核心数据流

### 文本→语音（含LLM预处理管线）

```
用户输入文本 + 选择声音 + 设置参数
  ▼
Pydantic schema校验（字数/声音ID/参数范围/Provider能力匹配）
  ▼
规则引擎第一遍（多音字词典精确匹配 + 数学/化学符号正则检测）
  ├── 不需要LLM → 跳过，直接用规则结果
  └── 需要LLM → LLM Pipeline:
       ① 符号上下文感知转译（∂在微积分→"偏导数"，拓扑学→"边界算子"）
       ② 多音字歧义消解
       ③ CosyVoice2标记生成
       ④ Pydantic Schema校验 → 失败重试（最多2次）→ 仍失败则规则兜底
  ▼
Redis缓存检查 key=hash(文本+声音ID+参数+provider)
  ├── 命中 → 直接返回
  └── 未命中 → SETNX互斥锁
       ├── 未获得锁 → 自旋等待（max 500ms）→ 再查缓存
       └── 获得锁 → Provider合成 → 写缓存（TTL=24h±随机）→ 释放锁
```

**为什么规则兜底 + LLM裁决而非纯LLM：** 规则引擎快、确定、零成本，LLM解决规则搞不定的符号和歧义。纯文本直接跳过LLM省钱省延迟。输出经Pydantic Schema校验+重试，不是裸调API。

### 课件合成（M2）

```
用户上传PPTX → 安全链（ZIP magic bytes + defusedxml + 文件名UUID化）
  → python-pptx逐页提取文本 → 批量INSERT slides
  → 用户编辑 → 点"开始合成"
  → Celery task: synthesize_courseware.delay(project_id)

Worker内:
  → SELECT slides ORDER BY slide_index
  → async with Semaphore(3):  # 背压控制
       asyncio.gather(*[_synthesize_slide(s) for s in slides],
                       return_exceptions=True)  # 单页失败不阻塞批次
  → 每页: 选文本→LLM管线→缓存检查→Provider.synthesize()
  → Redis INCR progress → SSE推送 → 浏览器进度条
  → 完成: ZIP打包（manifest.json + 逐页MP3 + 原始PPTX）
```

## 防护措施

### 纵深防御（11层）

| 层 | 措施 | 拦截什么 |
|---|---|---|
| TLS | HTTPS强制，TLS 1.2+，HSTS | 中间人攻击 |
| CSP | `default-src 'self'; media-src 'self' blob:` | XSS |
| Cookie | SameSite=Lax; HttpOnly; Secure | CSRF |
| Nginx | 20M body limit，10r/s burst=5 | DoS |
| SQLi | SQLAlchemy 2.0参数化100% | SQL注入 |
| 文件名 | sanitize→UUID重命名 | 路径穿越 |
| 魔数 | 读文件头4字节 | Content-Type伪造 |
| 密码 | bcrypt(cost=12) | 明文泄露 |
| XXE | defusedxml替代lxml，禁DTD | PPTX内XXE注入 |
| ZIP炸弹 | `sum(file_size) <= 100MB` | Billion Laughs |

### 克隆接口的多层限流

| 层 | 维度 | 窗口 | 限额 |
|---|---|---|---|
| Nginx | IP | 1s | 10r burst 5 |
| API用户 | user_id | 1min | 30 |
| TTS接口 | user_id | 1min | 10 |
| TTS接口 | user_id | 1day | 200 |
| **克隆接口** | **user_id** | **1day** | **10（存DB）** |

克隆日限额存在DB而非Redis的原因：攻击者可用不同IP注册多账号刷克隆获取某人声纹数据——Redis flush清空后计数器归零，DB记录不可绕过。且同一`audio_sha256`禁止不同用户重复克隆。

### 缓存层韧性

| 问题 | 对策 |
|---|---|
| 缓存穿透 | Pydantic校验无效voice_id直接400；空结果也缓存(TTL=60s) |
| 缓存击穿 | SETNX互斥锁 + 自旋等待(max 500ms) |
| 缓存雪崩 | TTL随机化(base + random(0, 7200s)) |
| 内存溢出 | `allkeys-lru` |

## LLM 文本预处理三级降级与 Prompt 外置

教学文本中 90% 以上是普通文本，不需要 LLM 处理；只有多音字、数学符号、化学式等疑难内容才需要纠音。三级降级的思路是"能不调就不调，能少调就少调"：

1. **第一级（规则引擎）**：正则和规则检测，纯文本直接通过——零 LLM 成本、零延迟
2. **第二级（DeepSeek 纠正）**：命中疑难文本才调 LLM，temperature=0.1 追求确定性，JSON 结构化输出 + Pydantic 校验
3. **第三级（本地兜底）**：LLM 全部失败时，本地确定性替换表保证符号读音不丢，服务不中断

**Prompt 外置热更新**：Prompt 模板从代码剥离进数据库（版本化 + 60s 内存缓存 + 故障兜底）。不改代码、不重新部署即可热更新 Prompt/模型/参数，且版本/模型/参数三要素可归因——出了问题能查到当时用的是什么 Prompt。

## 熔断器：从"装了没接线"到真正生效

熔断器三态：闭合（正常放行）、断开（直接拒绝）、半开（放少量请求试探下游）。两个关键设计：

- **按异常类型区分**：业务错误（余额不足、参数错误）不是下游故障，不计入熔断；只有网络错误和 5xx 才计数。否则用户输错几个参数就能把整个服务熔断。
- **半开单请求试探**：半开状态原本全量放行，下游刚恢复就被打回断开（雪崩）。改为单请求试探，成功才恢复闭合。

**踩过的真缺陷**：熔断器状态机和单元测试全绿，但实际没有业务调用方——相当于装了保险丝但没接进电路，熔断根本不生效。这个问题的教训是：测试通过 ≠ 已接入业务路径。

**成本控制**：合成结果按哈希缓存（相同文本只合成一次），配防击穿锁——并发请求相同文本时只产生一次真实 API 调用，其余等待结果。

## 异步工程三题

**并发竞写**：多 worker 并发合成不同页时共享同一个数据库 session，出现"部分页随机失败"——单测复现不了，只有并发才触发。修复：每页独立 AsyncSession + Semaphore(3) 限并发。

**Celery × asyncio 事件循环冲突**：Celery worker 是同步的，而 SQLAlchemy 异步 ORM 需要事件循环；每任务新建循环会让模块级连接池绑定到旧循环而失效。修复：任务收尾的 finally 中，在本循环内 dispose 连接池归还连接。

**三层超时体系**：SDK 层（LLM 60s/TTS 30s）→ 单页层（120s）→ 任务层（1800s 软墙），每层超时都有 finally 四道防线保证状态收敛，防止任务永久卡死在"运行中"。配套 Celery Beat 每 10 分钟清理孤儿课件、过期音频与卡死项目。

## 安全机制补强（迭代期新增）

- **JWT 双 token 轮换**：刷新时 refresh token 立即轮换、旧 token 按剩余寿命拉黑——泄露窗口从最长 7 天缩到一次使用；登出双 token 都吊销
- **账号锁定防邮箱枚举**：连续失败 5 次锁 15 分钟，失败计数与锁定标记用两个 Redis key 分离，防止攻击者借"锁定状态是否存在"探测邮箱是否注册
- **Redis 双库分离**：db0 存可淘汰缓存，db1 存安全状态（黑名单/锁定）——否则缓存淘汰可能把黑名单 key 逐出，已登出的 token 复活
- **滑动窗口限流**：Redis sorted set 实现，取 X-Forwarded-For 最右侧识别真实 IP，防伪造头绕过
- **HMAC 签名 URL**：私有音频不暴露路径，HMAC-SHA256 签名临时 URL + 恒时比较（防时序侧信道）+ 路径穿越防护
- **上传校验链**：MIME 白名单 + magic bytes（防改后缀）+ ZIP 炸弹防御
- **SSE 认证**：一次性 ticket 认证，避免 JWT 进 URL 落入日志；进度推送从 1s 轮询改为 Redis pub/sub，延迟接近实时

## 产品判断：明确不做什么

完成 5 款竞品调研（讯飞智作/魔音工坊/Murf/ElevenLabs/腾讯智影）后，结论是不做通用配音——音色数量、商业资源、模型质量有难以追赶的壁垒，转而聚焦教学场景"语音 PPT"。产品化阶段按「优先级 = (失效概率 × 影响面) / 修复成本」重排需求，补齐熔断器半开探测、三层超时、账号锁定、月度配额、Prompt 外置、Prometheus 可观测性（四类指标埋点，asynccontextmanager 保证异常路径也记数）。

## 数据模型（7张表，SQLAlchemy 异步 ORM + Alembic 迁移）

- `users`：UUID主键、email/username唯一、role、quota_override(JSONB)
- `voices`：type(preset/cloned)、provider、provider_metadata(JSONB)、reference_audio_path、consent_id
- `consents`：audio_sha256、purpose、agreed_at、expires_at、revoked_at（声纹授权审计记录）
- `synthesis_tasks`：input_text、llm_processed_text、params(JSONB)、status、cached、provider_used
- `courseware_projects`：title、original_pptx_path、status(状态机)、progress
- `courseware_slides`：slide_index、original_text、edited_text、llm_processed_text、audio_path

## 测试与完成状态

**Python后端（主版本，2026-09-05 实测）：** 144项pytest通过（fakeredis + SQLite in-memory，无需真实服务），M1+M2全部完工，安全审计Critical清零，22项真实HTTP栈契约检查通过（前后端接口的唯一裁判）。

**Java后端（参考实现）：** 123个JUnit通过，含MCP Server模块（`@Tool`暴露TTS/克隆为Agent可调用工具），功能为参考实现未做端到端联调。

**前端：** Vue 3 + TypeScript，vue-tsc类型检查通过，6个页面视图。

**部署：** Docker Compose六服务编排（前端/API/Worker/Beat/PG/Redis，含 Celery Beat 定时清理），API 不暴露宿主机端口，Nginx 反代统一入口，GitHub Actions CI。

## 如实声明的已知限制

1. **单GPU串行推理**：RTX 4060仅够单请求推理，线上靠云API并发，本地Celery排队串行
2. **流式延迟**：官方150ms首包在A100上，4060上RTF约0.3-1.0
3. **WSL2/Docker硬依赖**：CosyVoice2官方只保Linux
4. **明确不做 MOS 主观评测**（需 20-30 人盲测，一人项目做不了）、不做订阅/支付（零付费用户前是纯浪费）、不做 OpenTelemetry（三服务单体调用路径极短，分布式追踪收益不抵复杂度）

## GitHub

[wozwiiiii/lingxi-voice](https://github.com/wozwiiiii/lingxi-voice.git)
