# 架构说明

[English](architecture.md) | [简体中文](architecture.zh-CN.md)

QuantOS 是本地、确定性优先的处理链。跨层持久化边界采用typed、versioned、
fail-closed schema，并在下游消费前重新校验。

## 分层

1. **Market与Candidate：** 使用显式PIT cutoff查询canonical市场观察，由确定性
   anomaly与triage逻辑生成排序candidate。
2. **Evidence：** 公告、新闻与Web记录保留发布时间、采集时间、可知时间及来源。
3. **Attribution：** 确定性资格规则决定哪些Event Evidence可支持解释；STRICT
   不接受回顾性proxy。
4. **Knowledge Document：** 不可变、版本化背景资料拥有独立document、family、
   content与provenance身份。
5. **Retrieval：** 选择精确配置的词法索引；PIT不合格chunk在DF、avgdl和BM25前排除。
6. **Context Assembly：** 检索结果按versioned policy排序和限额，并物理分为
   historical与retrospective lane。
7. **Guarded Synthesis：** 结构化综合只接收已校验的Evidence与K-ref namespace；
   cache lookup前先校验输入，因果语言必须有Attribution Evidence。
8. **Daily Product：** v1表示legacy无Knowledge产品；v2把Knowledge context和
   structured synthesis绑定进产品身份。
9. **TimeSlice：** PRE_OPEN精确复用上一份Daily；POST_CLOSE复用目标日精确Daily。
10. **Orchestration与Manifest：** 持久化readiness、execution result、精确制品引用、
    安全失败和Knowledge operational state。
11. **Scheduler：** 交易窗口、lease、fencing、retry与stale takeover调用通用运行适配器，
    不理解检索语义。

## 核心不变量

```text
KnowledgeDocument != EventEvidence != AttributionEvidence
                  != RetrievedContext != LLMContext
```

- Knowledge retrieval不会授予Attribution资格。
- STRICT与RESEARCH是独立contract；retrospective context不能进入STRICT输出。
- 历史可见性使用 `available_at`，不能只看 `published_at`。
- PIT过滤先于排序统计。
- PIT关键naive datetime失败关闭。
- generated time、路径、hostname、mtime与duration属于observational字段；除非字节
  制品contract明确要求，否则不进入logical identity。

## Identity与Storage

身份链从document/chunk，经index、request、context、synthesis input、report、
artifact SHA、operational state到run identity。每层绑定影响该层结果的输入，排除
部署路径和运行观察字段。

Repository采用canonical serialization、identity revalidation、atomic publication、
append-only或collision-safe写入与corruption detection。Daily JSON/Markdown pair和
被引用制品需要联合校验。

## Failure semantics

Knowledge运行状态为闭合集：`NOT_CONFIGURED`、`EMPTY`、`READY`是soft state；
`UNAVAILABLE`、`STALE`、`CORRUPT`、`FAILED_INTEGRITY`、`FAILED`是hard state。
缺失、过期、字节损坏、语义不一致和内部异常保持不同分类。持久化原因只使用安全、
确定的reason code，不保存原始异常、provider内容或credential。

## Scheduler边界

Scheduler负责slot、due/grace window、claim、lease、fencing、retry与takeover；不负责
BM25、index version、K refs、ContextPolicy或retrieval。Scheduler幂等性不等于业务
执行具备exactly-once保证。
