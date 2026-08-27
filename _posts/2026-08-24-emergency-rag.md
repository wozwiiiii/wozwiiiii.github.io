---
layout: post
title: 应急RAG问答系统
description: 危化品应急管理场景，RAG + Text-to-Pandas Agent双路问答，三层沙箱防护LLM生成代码
date: 2026-08-24
period: 2026年6月-7月
permalink: /blog/2026/08/24/emergency-rag/
categories: [项目]
tags: [RAG, ReAct Agent, FastAPI, 沙箱安全, Chroma]
image: https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=emergency%20management%20AI%20Q%26A%20system%20RAG%20retrieval%20clean%20green%20tech%20illustration&image_size=landscape_16_9
---

## 项目背景

Datawhale秋训营"应急管理安全生产智能问答"赛题。数据为赛题官方提供，系统是赛题的工程化实现而非生产部署产品。

**解决什么问题：** 危化品应急管理场景下，管理人员需同时面对两类数据——17张危化企业Excel表（企业基础、仓库、8类特殊作业、SDS、工艺等）和22篇应急领域文档（GB30871-2022特殊作业规范、安全生产治本攻坚三年行动方案等）。传统"人工查数据+解读政策"存在监管难、响应慢瓶颈。

## 我负责的部分

从竞赛原型`advance/`到生产版`app/`的**全套工程化改造**，核心差异：

| 维度 | advance原型 | 生产版（我的改造） |
|---|---|---|
| 代码执行 | `exec()`直接执行+注入`os`模块 | AST白名单+子进程沙箱+超时强杀 |
| 索引 | Chroma内存客户端每次重建 | PersistentClient+构建分离+embedding缓存 |
| 表格数据 | 每次循环`pd.read_excel` | parquet预转换+沙箱内惰性加载 |
| 服务形态 | 脚本批处理answer.csv | FastAPI REST+认证+限流+探针 |
| 错误处理 | print继续 | 统一异常体系+JSON错误体+request_id |
| 意图路由 | 每题一个全量ReAct循环 | LLM单次分类+启发式降级 |
| 质量 | 无度量 | 18题评测集+A/B报告 |
| 测试 | 无 | 64项（含26条沙箱攻击载荷） |

## 技术架构

```
Client →[X-API-Key]→ FastAPI(认证+限流+request_id) → IntentRouter
  ├─ text   → Retriever(Chroma持久化, top-k+阈值) → Generator(拒答/引用来源)
  ├─ table  → TabularAgent(两阶段: LLM选表→ReAct分析) → Sandbox(三层防护) → Parquet
  └─ hybrid → 并行执行text+table → 融合LLM → 统一答案
```

## 文本RAG全链路（`app/rag/` 4个文件）

### 文档分块（`chunking.py`）

- 支持.docx/.pdf
- 按Heading样式识别标题层级（1-4级），维护标题栈生成`heading_path`（如"某文件 > 第一章 > 1.2节"）
- 超长section按段落二次切分，单段超1200字符硬切，相邻块保留150字符overlap
- docx表格按行拼接为正文保留结构

### 索引构建（`ingest.py`）

```
extract_file → chunk_blocks → embed_documents → Chroma持久化
```
- chunk_id由source+heading+index+text md5生成
- collection使用`hnsw:space: cosine`
- 单文件解析失败不阻断整体构建（记入`skipped`）
- embedding磁盘缓存（sqlite3 + WAL，key=md5(model:text)），增量构建近乎免费

### 检索（`retriever.py`）

- Chroma `PersistentClient`只读加载
- top-k召回（默认k=6）+ 相似度阈值过滤（默认0.35）
- **低于阈值的全部丢弃，全空时触发拒答而非编造**——应急安全场景硬要求

### 答案生成（`generator.py`）

```python
def generate_answer(question, chunks, llm):
    if not chunks:                      # 无证据直接拒答，不调LLM
        return REFUSAL_ANSWER, [], True
    context = _build_context(chunks)    # [1] 来源: xx | heading \n text
    msg = llm.chat_safe([...], temperature=0.1)
    head = (msg.content or "").strip()[:60]
    model_refused = any(m in head for m in _REFUSAL_MARKERS)  # 仅前60字判定
    return answer, sources, model_refused
```

system prompt强约束"只依据参考资料、末尾列引用编号[1][2]、资料不足明确说明"。

## 表格RAG：两阶段Text-to-Pandas ReAct（`app/tabular/agent.py`）

### 为什么用经典ReAct文本协议而非function calling

**真实实测依据**：硅基流动Qwen2.5-7B的`tool_calls`输出为不可解析伪JSON（乱码+重复token，单次26s+）；改文本协议后完全可控。

### 为什么两阶段（先选表再ReAct）

17张表全量schema约2.3万token，7B模型必超时。阶段一LLM选表（输出JSON，temperature=0，max_tokens=200）后，阶段二只为所选表生成紧凑画像，prompt缩小一个数量级。

### 自愈闭环

```
for i in range(1, max_iterations + 1):  # 最多8轮
    msg = llm.chat_safe(messages, temperature=0.1)
    m = _ACTION_RE.search(text)  # 正则解析 Thought/Action/Action Input
    if action == "finish": return TabularAnswer(...)
    observation = run_code(action_input)  # 沙箱执行
    messages.append({"role":"user","content": f"Observation:\n{observation}"})  # 错误回填自愈
```

沙箱执行错误traceback回填下一轮messages，LLM据此修正代码。最后一轮强制finish不再执行代码。

## 三层沙箱（核心差异点）

威胁模型：LLM生成代码可能被提示注入操纵，目标是任意文件读写/命令执行/secret读取/数据外传。**黑名单方案被实测绕过**（`np.memmap`任意文件读、`pd.io.common.os`赋值别名链读secret + `execve`命令执行），故改为能力白名单为主的三层防护。

### 第一层：AST静态校验（`validate_code`）

- 禁止`Import/ImportFrom/Global/Nonlocal`
- 禁止一切下划线属性（阻断`__class__/__globals__/__import__`逃逸链）
- 属性黑名单：`system/execve族/memmap/read_*/to_csv/urlopen`等60+原语
- **连续属性链深≤2**（挡`pd.io.common.os.environ`直链，正常pandas惯用法至多两级）
- 名字白名单：`pd/np/dfs` + 纯函数内建 + 异常类 + 已赋值变量

### 第二层：运行时白名单命名空间

子进程中的`pd`/`np`**不是模块本体**，是curated `SimpleNamespace`：
- pd仅暴露DataFrame/Series/merge/concat/pivot_table等
- np仅暴露array/mean/linalg(norm/inv/det)等
- **`pd.io`/`np.memmap`/`np.core`物理不存在**，赋值别名绕过必然AttributeError
- `__builtins__`同为白名单字典；即使AST层被完全绕过，可达能力面仍是封闭集合

### 第三层：进程隔离与资源限制

- `multiprocessing.get_context("spawn")`独立子进程，daemon=True
- 超时强杀（默认30s，`proc.terminate()` + `join(5)`）
- 输出截断（默认4000字符），表数据惰性加载

**验证**：26条攻击载荷（动态导入、dunder逃逸、numpy文件原语、赋值别名链、进程执行族）全部拦截；7条合法pandas/numpy分析正常执行；死循环被超时终止。

## 意图路由（`app/orchestrator/router.py`）

```python
def route(self, question):
    try:
        msg = self._llm.chat_safe([...], temperature=0.0, max_tokens=32)
        route = json.loads(content).get("route","")
        if route in VALID_ROUTES: return route
        raise ValueError(...)
    except Exception:
        fallback = _heuristic(question)  # 关键词启发式，永不抛异常
        return fallback
```

`_TABLE_HINTS`含"数量/多少/统计/列出/占比"；`_TEXT_HINTS`含"规定/要求/如何/标准/规范/依据"；同时命中走hybrid。

## hybrid容错融合（`app/orchestrator/pipeline.py`）

- 唯一异步入口`answer()`，单问题120s硬上限
- hybrid用`asyncio.gather`并行执行text+table双路，单路失败不阻断（survivor标`degraded`）
- 融合策略：两路都成功时调融合LLM（数据部分以表格路为准、政策部分以文本路补充）
- 融合失败再降级用表格路答案
- 出现拒答时以非拒答路为准

## 服务化与工程化

- **request_id中间件**：每请求生成12位hex，通过contextvars注入日志上下文
- **探针分离**：`/health`（liveness）+ `/ready`（readiness，检查chroma.sqlite3、parquet、LLM key）
- **LLM客户端收敛**：所有业务模块只允许通过统一客户端访问LLM；tenacity超时+指数退避重试
- **Docker多阶段构建**：builder层装依赖，运行层`python:3.12-slim` + 非root用户
- **索引构建与服务分离**：`scripts/build_index.py`独立CLI，解决竞赛原型"每次重启浪费数分钟与token"的核心缺陷

## 评测结果（Qwen2.5-72B，2026-08-17）

| 类别 | 题数 | 平均分 | 路由正确率 |
|---|---|---|---|
| text | 8 | 0.733 | 8/8 |
| table | 6 | 0.850 | 6/6 |
| hybrid | 2 | 0.850 | 2/2 |
| refuse | 2 | 0 → 1.000（修复后复测） | 不适用 |

意图路由18/18全对。首轮主要失分：refuse类模型已文本拒答但`refused`标志未置位（已修复，复测满分）；个别text题检索未命中目标文档。

## 如实声明的已知限制

1. **文件解析限制**：.wps与老.doc无法解析，22篇中15篇可解析，生成633分块
2. **限流状态在进程内存**：多实例需换Redis
3. **评测集为自建近似**：18题不代表官方真实得分
4. **Windows沙箱开销**：spawn有约1s开销
5. **威胁模型边界**：子进程仍以容器用户权限运行，对抗国家级攻击者需叠加OS级降权（seccomp/AppArmor）
6. **纯向量检索无BM25混合**：个别text题未命中目标文档，候选优化方向：rerank、HyDE、按法规名构建元数据过滤

## GitHub

[wozwiiiii/all-in-rag-myself-new](https://github.com/wozwiiiii/all-in-rag-myself-new.git)
