# QuantOS

[English](README.en.md) | [简体中文](README.md)

**时间点安全、证据驱动、可审计的金融情报智能体后端**

QuantOS 是一个确定性优先、Point-in-Time Safe 的金融情报agent，用于构建
可审计、可复现、证据驱动的市场分析系统。它不是 AI 选股器、自动交易
机器人或订单执行系统。

## 为什么需要 QuantOS

历史分析若混入未来信息、把背景知识误作事件证据，或让大模型承担数值事实，
结论就难以复现和审计。QuantOS 在存储、检索、综合、产品、运行与回放各层
显式维护这些边界，并采用失败关闭策略。

## 设计原则

- **时间点安全（Point-in-Time Safety）：** 区分可知时间、市场事件时间和
  运行时间；未来数据在排序前即被排除。
- **确定性优先：** Python 负责市场事实、资格判定、身份、排序与校验；LLM
  仅生成受控解释。
- **证据归因：** 因果语言必须有合格的归因证据。检索到的知识不会自动成为证据。
- **闭合、版本化制品：** 逻辑身份绑定影响结果的输入；存储在损坏或冲突时失败关闭。
- **运行可审计：** Manifest、安全原因码、精确制品引用与 provenance 支持本地回放。

## 架构

```text
市场数据 → Candidate → Evidence → Attribution
        → Knowledge Retrieval → Knowledge Context
        → Guarded LLM Synthesis → Daily Intelligence
        → PRE_OPEN / POST_CLOSE → Operational Manifest → Scheduler
```

详见[架构说明](docs/architecture.zh-CN.md)与[核心概念](docs/concepts.zh-CN.md)。

## 核心能力

- 时区感知的市场/可知时间与显式 `as_of_time`
- 不可变的本地市场、证据、知识、上下文、报告与运行制品
- 精确索引选择与 PIT-safe 词法检索
- 历史知识与回顾性知识的物理分层
- Evidence/K-ref校验与受控结构化综合（Guarded Synthesis）
- Knowledge-aware Daily Intelligence v1/v2 与 TimeSlice 产品
- PRE_OPEN 精确复用上一份 Daily，POST_CLOSE 精确复用当日 Daily
- Readiness、失败Manifest、Scheduler租约、fencing与有限重试
- 支持回放和PIT指标的多日离线评估

## PIT 模型

`market_event_time` 表示被解释的市场事件时点；`RunContext.as_of_time` 表示
运行评估截止点。知识只有在 `available_at` 不晚于事件截止点时才属于历史可见。
仅仅 `published_at` 较早并不足够。PIT过滤发生在BM25统计与排序之前。

PIT关键输入拒绝naive datetime；表示同一时刻的aware datetime按时间语义规范化。

## Knowledge 不等于 Evidence

```text
KnowledgeDocument != EventEvidence != AttributionEvidence
                  != RetrievedContext != LLMContext
```

知识用于背景说明，不获得归因资格；只有知识而没有合格Evidence时，不允许输出因果结论。

## STRICT 与 RESEARCH

- `strict_live` 只使用在市场事件截止点前真实可知的信息。
- `research` 在显式corpus cutoff下允许额外的回顾性知识，但必须位于独立lane。

STRICT消费者会拒绝RESEARCH产品，不会静默删除回顾内容后伪造STRICT结果。

## V1 已支持

PRE_OPEN、POST_CLOSE、STRICT、RESEARCH、市场数据、Candidate、Evidence、
Attribution、本地canonical Knowledge、词法检索、Context组装、受控综合、
Daily/TimeSlice、运行清单（Operational Manifest）、确定性Scheduler与离线评估。

## 明确不包含

V1不包含INTRADAY Knowledge、semantic/vector retrieval、运行时索引重建、
自主检索规划、REST API、Web/Desktop UI、认证/RBAC、云部署、商业数据授权或交易执行。

QuantOS V1 当前仅公开金融情报后端。Web、Desktop与商业产品层暂不属于当前
公开后端范围。未来产品接口将根据发行策略决定以开源或商业组件形式提供。

## 安装

要求 Python **3.10及以上版本**。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

项目尚未发布到PyPI，请勿把 `pip install quantos` 当作正式安装命令。

## 快速开始

```bash
git clone https://github.com/LIN-LOUIS/QuantOS.git
cd QuantOS
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
quantos --version
quantos doctor
quantos demo
quantos status
```

`quantos demo` 使用 `SYNTHETIC_FIXTURE`，属于 **NOT REAL-HISTORICAL
PERFORMANCE** 的工程演示；它不需要API key，不访问真实LLM，也不访问真实
market/news provider。真实市场日报仍需要provider/config、canonical本地制品，
以及对应的Evidence/Knowledge准备；仅clone仓库不会生成真实A股日报。
详见[快速开始](docs/quickstart.zh-CN.md)。


## 真实工作流命令

```bash
quantos report --help
quantos report daily --help
quantos report pre-open --help
quantos report post-close --help
quantos scheduler once --help
```

`quantos doctor` 检查当前环境能否运行 QuantOS；`quantos status` 离线、只读地
检查当前 workspace 有哪些真实数据和产品可用。Doctor PASS 不代表 Daily READY。
真实 report 命令需要已准备的本地 canonical market data、Evidence、
Knowledge/config 以及相应 provider/config。缺少所需制品时会返回结构化原因并
失败关闭；不会自动下载、切换 demo 或生成 synthetic report。

## CLI

```bash
quantos doctor
quantos demo
quantos status
quantos health --help
python -m quantos --help
python scripts/run_quantos.py --help
python scripts/generate_daily_report.py --help
python scripts/generate_time_slice_report.py --help
python scripts/run_scheduler_once.py --help
python scripts/evaluate_v1.py --help
```

基础CI与默认evaluation只使用本地fake，不发起provider请求。

## 离线评估

**数据来源：`SYNTHETIC_FIXTURE` — NOT REAL-HISTORICAL PERFORMANCE（非真实历史表现）。**

`evaluation/v1-strict-10d/` 审计快照覆盖10个交易日、19次运行（10次
POST_CLOSE与9次eligible PRE_OPEN复用）、20个candidates、POST_CLOSE/PRE_OPEN
均100%完成、Evidence/Attribution覆盖率100%、fixture设计的Knowledge READY/EMPTY
各50%、synthesis成功率100%、PIT违规0、determinism mismatch 0。

> **这些指标来自确定性synthetic fixtures，仅验证工程行为，不代表真实金融表现。**
> 它们不衡量市场预测准确率、alpha、PnL、Sharpe或生产延迟。

## 仓库结构

```text
src/quantos/                  后端schema、逻辑、存储与runtime
scripts/                      稳定的本地后端和evaluation CLI
tests/                        离线单元、集成与系统测试
evaluation/v1-strict-10d/     已审计synthetic evaluation快照
ops/systemd/                  Scheduler服务示例
docs/                         架构、概念与快速开始
```

## 当前状态与路线图

本候选基于内部V1 core freeze reference `46038d95...`，不是production-ready
trading platform。后续可开展经授权数据的real-historical replay、发行打包强化、
在词法baseline之后评估semantic/vector retrieval，以及另行决定API/Web/Desktop策略。
路线图内容不属于当前V1已实现能力。

## 免责声明

QuantOS用于软件研究与工程验证。输出不构成投资建议、交易指令或未来收益保证。
使用者需自行负责数据权利、结果验证、风险控制与监管义务。

## License状态

QuantOS 采用 Apache License 2.0 许可。详见 [LICENSE](LICENSE)。
