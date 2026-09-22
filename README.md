# QuantOS

简体中文 | [English](README.en.md)

**面向中国 A 股的可审计金融研究工作台。**

QuantOS 将 Ask、结构化分析、数据来源、Provenance、Trace 与 PIT-safe
Historical Replay 放在同一个本地工作空间中。Python 负责事实计算、资格判定与
时间边界；语言模型只能解释已经验证的结构化上下文。

QuantOS 不是股票预测器，不是自动交易系统，也不提供收益保证。它解决的是另一类
问题：一项研究结论能否说明数据来自哪里、当时能看到什么、为什么得到这个答案，
以及哪些能力或历史资料当前不可用。

当前维护版本为 **v0.3.1**，支持 Python 3.10、3.11 和 3.12。

## 30 秒体验

完成一次 Workspace 构建后，一条命令即可启动完整的离线演示：

```bash
python -m pip install -e '.[dev]'
cd web && npm ci && npm run build && cd ..
quantos doctor --project-root .
quantos start --demo
```

启动器默认只绑定本机 loopback。若 8000 端口已占用，它会在有限范围内选择下一个
可用端口，等待 `/v1/health` 就绪后再打开浏览器。无图形环境可增加
`--no-browser`。

Demo Mode 使用明确标记的合成 fixture：

- 不需要 API Key；
- 不访问 Tushare、BaoStock、Tavily、DeepSeek 等 Provider；
- 不依赖真实市场数据；
- 不写入用户的真实数据目录；
- 不代表真实历史表现，也不构成投资建议。

## 产品预览

![QuantOS Demo Mode 总览](docs/screenshots/v0.3.0-overview.jpg)

![Ask 与结构化 Trace](docs/screenshots/v0.3.0-ask-trace.jpg)

![Analytics 与数据 Provenance](docs/screenshots/v0.3.0-analytics-provenance.jpg)

![PIT Replay 审计](docs/screenshots/v0.3.0-replay.jpg)

## QuantOS 能做什么

- **Observe：** 查看系统、数据与研究能力的真实可用状态；
- **Ask：** 从受控能力生成事实、引用、限制和 reason codes；
- **Why this answer：** 查看 intent、实体解析、QueryPlan、能力执行与 Trace；
- **Analytics：** 使用注册指标和维度运行有界查询，不开放任意 SQL；
- **Provenance：** 检查 dataset refs、provider、cutoff 与 registry version；
- **Reports：** 读取已经持久化的结构化研究报告；
- **Replay：** 区分严格 operational PIT 与 retrospective reconstructed 历史语义；
- **Availability：** 明确区分 READY、PARTIAL、DEGRADED、UNAVAILABLE、
  UNCONFIGURED 与 DISABLED。

完整体验路径是：

```text
Observe → Ask → Analyze → Inspect Provenance → Inspect Trace → Inspect Replay
```

可按 [60–90 秒 Demo 脚本](docs/demo-script.md)完成一次产品演示。

## 为什么需要可审计

历史研究很容易受到三类问题影响：把未来数据泄漏到过去，把背景知识误当作事件证据，
或让语言模型承担数值事实。QuantOS 在存储、身份解析、检索、报告、回放与产品层都
显式维护这些边界，并在无法证明时失败关闭。

- **Point-in-Time 安全：** 区分市场事件时间、数据可知时间与运行时间；
- **确定性优先：** Python 负责数字、资格、排序、身份和校验；
- **证据有边界：** Knowledge 不会自动获得 Attribution 资格；
- **缺失不等于零：** 数据不可用与真实值为零是不同状态；
- **制品可追溯：** Manifest、hash、引用和 Trace 支持复核与重放；
- **默认只读：** Research API 不会隐式 bootstrap、refresh 或访问 Provider。

## 快速开始

要求 Python **3.10 或更高版本**、Node.js 与 npm。项目尚未发布到 PyPI，当前
验证安装方式是从源码安装：

```bash
git clone https://github.com/LIN-LOUIS/QuantOS.git
cd QuantOS
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cd web && npm ci && npm run build && cd ..
quantos --version
quantos doctor --project-root .
quantos start --demo
```

不要把 `pip install quantos` 当作当前正式安装路径。

## Demo Mode

```bash
quantos start --demo
```

页面顶部会显示 `DEMO DATA` 和 `SYNTHETIC_FIXTURE`。演示数据只用于验证产品
工作流、PIT 边界、Trace 与 Provenance；它不用于衡量预测准确率、alpha、PnL、
Sharpe 或真实生产延迟。

## 使用真实数据

Provider 凭证只通过进程环境配置，不应写入仓库或 `.env` 提交。先检查环境，再执行
显式且有界的数据 bootstrap：

```bash
quantos doctor --project-root .
quantos data bootstrap security-master --provider tushare --project-root .
quantos data bootstrap market --provider tushare \
  --symbol 600519.SH --trading-days 5 --project-root .
quantos status --project-root .
quantos ask 600519.SH --question "最近一个交易日表现怎么样？"
```

Tushare 路径需要进程环境中的 `TUSHARE_TOKEN`。QuantOS 只记录凭证是否配置，
不会持久化凭证值。BaoStock 可在无 token 时建立 Security Master：

```bash
quantos data bootstrap security-master --provider baostock --project-root .
```

Bootstrap 是有界、append-only、幂等且带审计记录的。普通 Ask 只读取持久化的
Security Master 与市场数据，不会因为首次提问而在线调用 `stock_basic`。

## 历史 Replay

历史数据导入与离线 Replay 明确分离：

```bash
quantos replay import-market --provider tushare --symbol 600519.SH \
  --start 2026-06-01 --end 2026-06-30
quantos replay run --dataset-id DATASET_ID --symbol 600519.SH \
  --start 2026-06-01 --end 2026-06-30
quantos replay show CAMPAIGN_ID
quantos replay failures CAMPAIGN_ID
```

Replay 只读取 successful manifest 声明可见的数据，不会在运行途中联网补数据。
默认模式 `STRICT_OPERATIONAL_PIT` 回答“QuantOS 当时实际知道什么”。显式的
`RETROSPECTIVE_RECONSTRUCTED` 使用权威 effective-time 身份记录重建历史事实，
但不会放宽市场、Evidence、Knowledge 或 Report 的时间边界。

## 本地 Research API

```bash
quantos serve --project-root .
curl -s http://127.0.0.1:8000/v1/health
curl -s http://127.0.0.1:8000/v1/status
```

API 默认只监听 `127.0.0.1`，不提供任意 SQL、Python、Shell、表名或 Provider
访问。Workspace 只通过版本化 Research API 获取数据，不直接访问文件系统或
DuckDB。

## 安全边界

- 不执行真实金融交易；
- 不提供买卖建议或未来收益保证；
- 浏览器不接触 Provider credential；
- Demo Mode 不读取真实 credential，也不访问 Provider 网络；
- Research API 默认本机只读，无生产级认证；
- Provider 可用性和限流取决于外部服务；
- 历史 Evidence、Knowledge 与严格 operational replay 覆盖取决于已持久化数据。

## 架构

```text
Browser / Future Client
          ↓
QuantOS Workspace
          ↓
Versioned Research API
          ↓
AskService / Semantic Service / Resource APIs
          ↓
Persistent Security Master / Market / Evidence / Reports / Replay
          ↓
Structured Result + Availability + Provenance + Trace
```

详见[架构说明](docs/architecture.zh-CN.md)、[核心概念](docs/concepts.zh-CN.md)
与[快速开始](docs/quickstart.zh-CN.md)。

## 开发

前端开发模式仍可独立使用 Vite：

```bash
quantos serve --project-root .
cd web
npm install
npm run dev
```

浏览器通过相对 `/v1` 请求和 Vite proxy 访问 API。若 8000 端口已占用，可显式
覆盖开发代理目标：

```bash
quantos serve --project-root . --host 127.0.0.1 --port 8010
cd web
QUANTOS_API_TARGET=http://127.0.0.1:8010 npm run dev
```

验证命令：

```bash
python -m pytest
python -m compileall -q src scripts
cd web
npm test
npm run typecheck
npm run build
npm audit
```

QuantOS v0.3.x 处于 Product Discovery Feature Freeze。没有真实用户证据时，只
接受安全、数据丢失、崩溃、安装/发布阻塞和重大正确性修复。详见
[Feature Freeze](docs/FEATURE_FREEZE.md)。

## English

完整英文说明见 [README.en.md](README.en.md)。

## 许可证与免责声明

QuantOS 使用 Apache License 2.0，详见 [LICENSE](LICENSE)。本项目仅用于研究与
工程验证；所有输出均不构成投资建议、交易指令或未来收益保证。
