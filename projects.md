---
layout: page
title: 项目
description: 三个项目的背景、我做了什么、用了什么、结果如何
permalink: /projects/
---

三个项目都使用 AI 工具辅助开发，代码和文档都放在 GitHub 上，每篇拆解文章里有更细的技术说明。

---

## 1. 灵犀语伴 — AI 语音课件应用（2026年3月-4月）

![灵犀语伴](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=AI%20voice%20technology%20PPT%20courseware%20generation%20clean%20blue%20tech%20illustration&image_size=landscape_16_9)

**技术栈：** Python, FastAPI, CosyVoice2, Celery, Redis, PostgreSQL, Vue 3, Docker

**一句话：** 帮老师把 PPT 变成带配音的有声课件，支持声音克隆和逐页试听。

**背景/任务：** 教学场景里老师做配音课件负担重。大赛阶段（团队 4 人）我负责后端和语音合成；赛后我一个人按企业规范把项目完整重构了一遍。

**我做了什么：**

- 完成 CosyVoice 的选型、本地部署和性能测试，支撑大赛作品提交。
- 重构后端：用 FastAPI 做分层、JWT 双 Token 登录；TTS 做了本地 / 云端两套实现，一套接口切换。
- 搭了一条 LLM 文本预处理管线，先跑规则（多音字词典、符号正则），规则处理不了的再交给 LLM 纠正读音。
- 用 Celery 做异步课件合成，SSE 推进度，完成后打包成 ZIP 下载；用 numpy 写了 SNR / 静音比 / 削波检测，上传音频不合格直接拒绝，不浪费算力。
- 补了测试和部署：96 个 pytest 用例、22 项接口契约检查、Docker Compose 五服务一键起。

**结果：** 大赛获全国三等奖（团队 4 人）；重构版后端测试全部通过，Docker 部署验证通过。

**GitHub：** [lingxi-voice](https://github.com/wozwiiiii/lingxi-voice.git) · **拆解：** [技术拆解](/blog/2026/08/26/lingxi-voice/)

---

## 2. deepsearch — 多智能体深度研究系统（2026年5月-6月）

![deepsearch](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=AI%20agent%20search%20system%20architecture%20security%20shield%20clean%20dark%20blue%20tech%20illustration&image_size=landscape_16_9)

**技术栈：** DeepAgents, LangGraph, FastAPI, MySQL, SQLite, Tavily, slowapi

**一句话：** 把一个开源教学版的深度研究 Agent 改造成能放心对外跑的版本，修掉 14 类安全问题。

**背景/任务：** 原版是开源教学项目，能跑但有明显问题：模型可以读服务器任意文件、SQL 能执行 DROP、没有认证和限流。我负责把它改到"敢给别人用"的程度。

**我做了什么：**

- 做了框架取舍：对比 AutoGen 和 LangGraph 直搭后，选 DeepAgents（子智能体 + todo 规划 + LangGraph 运行时），理由是 checkpointer 接口可替换，单机用 SQLite、以后扩容换 Postgres 不用改业务代码。
- 修路径穿越：所有文件操作限制在会话目录内，越界返回带纠正建议的提示，让模型自己改路径重试，不中断 Agent 循环。
- 修 SQL 风险：只放行 SELECT 类语句、表名走白名单、用 sqlglot 给没写 LIMIT 的查询自动补 LIMIT；数据库账号只授 SELECT 权限做兜底。
- 加认证和限流：API Key 认证、多租户目录隔离、按密钥限流、未配置密钥时默认拒绝服务（fail-closed）、给模型调用设硬上限防失控烧钱。

**结果：** 40 个文件、+3649 行工程增量，5 个 commit 交付；129 个单元测试 6 秒跑完，不依赖真实 LLM 和数据库；另写了任务队列分布式化的三阶段方案（ARQ + Postgres + Redis pub/sub），还没实现，文档里标了待做。

**GitHub：** [deepsearch-agent-try](https://github.com/wozwiiiii/deepsearch-agent-try.git) · **拆解：** [改造拆解](/blog/2026/08/25/deepsearch-agents/)

---

## 3. 应急RAG问答系统（2026年6月-7月）

![应急RAG](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=emergency%20management%20AI%20Q%26A%20system%20RAG%20retrieval%20clean%20green%20tech%20illustration&image_size=landscape_16_9)

**技术栈：** FastAPI, Chroma, pandas, ReAct Agent, bge-m3, Docker

**一句话：** 应急管理人员问政策能查文档回答，问统计能对着 17 张 Excel 写代码算出来。

**背景/任务：** 赛题要求同时回答两类问题：政策类（22 篇法规文档）和统计类（17 张危化企业 Excel 表）。我负责 RAG 和工程化部分。

**我做了什么：**

- 文本路：按标题层级分块、记录章节路径；检索加相似度阈值，分数不够直接拒答——应急场景宁可说"没查到"，不能编。
- 表格路：先让 LLM 从 17 张表里选出相关的 1-4 张，再生成紧凑表结构写 pandas 代码，把 prompt 从约 2.3 万 token 压下来（全量塞进去 7B 模型直接超时）。
- 写代码沙箱：AST 白名单 + 运行时命名空间隔离 + 子进程超时强杀三层防护，30 条攻击载荷（读文件、执行命令、逃逸链等）全部拦截。
- 工程化：接口加认证和限流、liveness/readiness 探针分开、request_id 全链路、embedding 加磁盘缓存避免重复花钱。
- 搭了 18 题 4 类评测集（含专门测拒答的 refuse 类），指标对齐竞赛公式。

**结果：** 18 题意图路由全对；refuse 类首轮模型嘴上拒答但程序没识别（已修复，复测满分）；64 项测试通过。已知限制（.wps 解析不了、限流状态在内存里）都写在 README 里。

**GitHub：** [all-in-rag-myself-new](https://github.com/wozwiiiii/all-in-rag-myself-new.git) · **拆解：** [工程化拆解](/blog/2026/08/24/emergency-rag/)
