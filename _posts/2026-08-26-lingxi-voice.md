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

## 数据模型（6张表）

- `users`：UUID主键、email/username唯一、role、quota_override(JSONB)
- `voices`：type(preset/cloned)、provider、provider_metadata(JSONB)、reference_audio_path、consent_id
- `consents`：audio_sha256、purpose、agreed_at、expires_at、revoked_at（声纹授权审计记录）
- `synthesis_tasks`：input_text、llm_processed_text、params(JSONB)、status、cached、provider_used
- `courseware_projects`：title、original_pptx_path、status(状态机)、progress
- `courseware_slides`：slide_index、original_text、edited_text、llm_processed_text、audio_path

## 测试与完成状态

**Python后端（主版本）：** 96个pytest通过（fakeredis + SQLite in-memory，无需真实服务），M1+M2全部完工，安全审计Critical清零，22项API契约检查通过。

**Java后端（参考实现）：** 123个JUnit通过，含MCP Server模块（`@Tool`暴露TTS/克隆为Agent可调用工具），功能为参考实现未做端到端联调。

**前端：** Vue 3 + TypeScript，vue-tsc类型检查通过，6个页面视图。

**部署：** Docker Compose五服务编排，GitHub Actions CI。

## 如实声明的已知限制

1. **单GPU串行推理**：RTX 4060仅够单请求推理，线上靠云API并发，本地Celery排队串行
2. **流式延迟**：官方150ms首包在A100上，4060上RTF约0.3-1.0
3. **WSL2/Docker硬依赖**：CosyVoice2官方只保Linux
4. **Circuit Breaker未接入调用点**：`record_failure`/`record_success`未在生产代码调用，Java版通过`@CircuitBreaker`注解解决此遗留

## GitHub

[wozwiiiii/lingxi-voice](https://github.com/wozwiiiii/lingxi-voice.git)
