# 快速开始

[English](quickstart.md) | [简体中文](quickstart.zh-CN.md)

## 环境要求

- Python 3.10及以上
- 用于clone的Git
- 当前cache locking runtime建议使用POSIX环境

## 从源码安装

```bash
git clone https://github.com/LIN-LOUIS/QuantOS.git
cd QuantOS
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

QuantOS当前未发布到PyPI。

## 首次运行

```bash
quantos --version
quantos doctor
quantos demo
```

`quantos doctor` 是离线、只读的安装检查。`quantos demo` 使用
`SYNTHETIC_FIXTURE`，属于 **NOT REAL-HISTORICAL PERFORMANCE**；它不需要
API key，也不访问真实LLM或真实market/news provider。

## 验证后端

```bash
python -m pytest -q
python -m compileall -q src scripts
```

测试使用本地fake与fixture，不需要provider credential，也不会发起真实LLM请求。

## 运行Synthetic Evaluation

Evaluation session不可覆盖，请使用新的output directory。

```bash
python scripts/evaluate_v1.py --help
python scripts/evaluate_v1.py \
  --output-dir /tmp/quantos-v1-evaluation \
  --trading-days 10
```

该命令运行确定性的 `SYNTHETIC_FIXTURE` benchmark，只验证工程行为，不是
REAL_HISTORICAL或投资表现测试。

## 查看运行CLI

```bash
quantos health --help
python -m quantos --help
python scripts/run_quantos.py --help
python scripts/generate_daily_report.py --help
python scripts/generate_time_slice_report.py --help
python scripts/run_scheduler_once.py --help
python scripts/evaluate_v1.py --help
```

时间参数必须带时区offset，例如：

```bash
python scripts/run_quantos.py \
  --trade-date 2026-09-10 \
  --as-of-time 2026-09-10T18:00:00+08:00 \
  --run-type POST_CLOSE \
  --mode strict_live \
  --top-n 20 \
  --no-llm
```

该命令消费已经存在的canonical本地制品。若制品缺失、过期、损坏或语义不兼容，
系统会以结构化readiness/failure信息失败关闭；它不会下载或伪造真实市场日报。

真实市场日报需要provider/config、canonical本地制品以及对应的Evidence/Knowledge
准备；仅clone仓库不会生成真实A股日报。Daily生成同样依赖已准备的本地市场与
Evidence制品：

```bash
python scripts/generate_daily_report.py --help
```

Scheduler运行需要合法本地calendar artifact和runtime DB，请先查看真实参数：

```bash
python scripts/run_scheduler_once.py --help
```

## 可选Provider配置

Provider-capable路径从环境变量读取配置，请勿提交实际值。Synthetic evaluation与CI
不需要这些变量。相关变量名包括 `DEEPSEEK_API_KEY`、`TUSHARE_TOKEN`、
`TAVILY_API_KEY`、`EXA_API_KEY`以及 `QUANTOS_LLM_*` / `QUANTOS_KNOWLEDGE_*`。

当前默认公开验证路径完全local/provider-free，因此暂不提供 `.env.example`。
