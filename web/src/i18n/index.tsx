import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export type Locale = "zh-CN" | "en-US";

const STORAGE_KEY = "quantos.locale";

const messages: Record<Locale, Record<string, string>> = {
  "en-US": {
    "nav.overview": "Overview", "nav.ask": "Ask", "nav.analytics": "Analytics", "nav.reports": "Reports", "nav.replay": "Replay",
    "shell.subtitle": "Research Workspace", "shell.boundary": "Research API is the only data boundary.", "shell.audit": "AUDITABLE FINANCIAL RESEARCH",
    "shell.checking": "CHECKING", "shell.loading": "Loading workspace module…", "shell.feedback": "Feedback",
    "runtime.unavailable": "QuantOS Research API is unavailable.", "runtime.retryHint": "Check the Preview operator, then retry the bounded health request.", "runtime.retry": "Retry API",
    "runtime.demo": "DEMO DATA", "runtime.demoDetail": "Synthetic fixture data · No live provider calls · For research/product demonstration · Not investment advice.",
    "runtime.local": "LOCAL PERSISTED DATA",
    "overview.kicker": "PUBLIC PREVIEW", "overview.title": "Auditable AI Financial Research Workspace",
    "overview.description": "Ask grounded questions, inspect evidence and provenance, and review point-in-time replay without hidden data access.",
    "overview.start": "Start Research", "overview.report": "View Example Report", "overview.health": "PROCESS HEALTH",
    "overview.healthDetail": "Versioned {version} process is responding through the Research API boundary.",
    "overview.capabilities": "What this preview can do", "overview.capabilitySubtitle": "Capability states come directly from QuantOS. Unavailable data is never presented as zero.",
    "overview.technical": "Technical status", "overview.technicalHint": "Provider configuration and health metadata",
    "overview.noProviders": "No provider health records are exposed.", "overview.why": "Why?",
    "status.READY": "Ready", "status.PASS": "Pass", "status.PARTIAL": "Partial", "status.DEGRADED": "Degraded", "status.UNAVAILABLE": "Unavailable", "status.UNCONFIGURED": "Not configured", "status.DISABLED": "Disabled", "status.FAILED": "Failed",
    "status.readyHelp": "This capability is available for the current dataset and boundary.",
    "status.partialHelp": "A usable result is available, with explicit limitations.",
    "status.degradedHelp": "The capability is operating with reduced coverage or reliability.",
    "status.unavailableHelp": "Required persisted data or capability is unavailable. QuantOS does not invent a result.",
    "status.unconfiguredHelp": "This optional capability has not been configured for the preview.",
    "status.disabledHelp": "This capability is intentionally disabled in the current runtime.",
    "cap.evidenceUnavailable": "Public Preview does not connect an external evidence provider. QuantOS therefore does not fabricate external sources.",
    "ask.kicker": "GROUNDED ASK", "ask.title": "Ask, then inspect why", "ask.description": "Answers are separated into facts, references, limitations, and execution lineage.",
    "ask.security": "Security", "ask.question": "Research question", "ask.offline": "Market-only offline context", "ask.submit": "Ask QuantOS", "ask.loading": "Building grounded answer…",
    "ask.examples": "Try an example", "ask.example1": "Show the latest market performance.", "ask.example2": "What is the latest market closing price?", "ask.example3": "Show the latest market price and trading volume.",
    "ask.why": "Why this answer?", "ask.traceTitle": "Research Trace", "ask.facts": "Facts", "ask.availability": "Availability & limitations", "ask.references": "Grounding references",
    "analytics.kicker": "SEMANTIC ANALYTICS", "analytics.title": "Numbers with provenance", "analytics.description": "Build a bounded query from registered metrics and dimensions. There is no SQL surface.", "analytics.metric": "Metric", "analytics.dimension": "Dimension", "analytics.security": "Security", "analytics.start": "Start date", "analytics.end": "End date", "analytics.sort": "Sort direction", "analytics.limit": "Row limit", "analytics.run": "Run analytics", "analytics.running": "Running bounded query…", "analytics.provenance": "Data provenance",
    "reports.kicker": "REPORT ARCHIVE", "reports.title": "Generated research, read in context", "reports.description": "Browse existing report artifacts. Opening this page never generates or refreshes a report.", "reports.date": "Date", "reports.security": "Security", "reports.apply": "Apply filters", "reports.list": "Daily intelligence reports", "reports.detail": "Report detail",
    "replay.kicker": "HISTORICAL REPLAY", "replay.title": "Time semantics made visible", "replay.description": "Inspect frozen campaigns, PIT counters, determinism, and structured failure records.", "replay.mode": "Replay mode", "replay.all": "All modes", "replay.campaigns": "Replay campaigns", "replay.audit": "Replay campaign audit",
    "guide.title": "A three-step research flow", "guide.ask": "1. Ask", "guide.askText": "Start with a grounded market question.", "guide.inspect": "2. Inspect", "guide.inspectText": "Review facts, provenance, limitations, and why the answer was produced.", "guide.replay": "3. Replay", "guide.replayText": "Check what was visible at a historical cutoff.", "guide.dismiss": "Got it",
    "feedback.title": "Preview feedback", "feedback.helpful": "Was this preview useful?", "feedback.yes": "Useful", "feedback.no": "Needs work", "feedback.notes": "Optional feedback", "feedback.placeholder": "Describe what helped or what was unclear. Do not include personal information.", "feedback.copy": "Copy Feedback", "feedback.copied": "Copied", "feedback.privacy": "Feedback stays in your browser until you copy it. QuantOS does not collect identity or PII.",
    "common.close": "Close", "common.auditDetail": "AUDIT DETAIL", "common.loading": "Loading research context…", "common.retry": "Retry", "common.copyError": "Copy error details", "common.noRecords": "No matching records.",
    "reason.ATTRIBUTION_INSUFFICIENT": "Causal explanation unavailable: current evidence does not meet attribution policy.", "reason.EVIDENCE_UNAVAILABLE": "Evidence history is unavailable for this boundary.", "reason.KNOWLEDGE_TEMPORAL_UNVERIFIED": "Knowledge has no verified historical availability time.", "reason.REPORT_NOT_VISIBLE": "No report is visible at the selected cutoff.", "reason.MARKET_DATA_UNAVAILABLE": "Persisted market data is unavailable.", "reason.NO_DATA": "The dataset is available, but no records matched.", "reason.OK": "The requested grounded result is available.", "reason.default": "QuantOS returned this deterministic reason code.",
  },
  "zh-CN": {
    "nav.overview": "总览", "nav.ask": "提问", "nav.analytics": "分析", "nav.reports": "报告", "nav.replay": "回放",
    "shell.subtitle": "研究工作台", "shell.boundary": "Research API 是唯一的数据边界。", "shell.audit": "可审计金融研究",
    "shell.checking": "检查中", "shell.loading": "正在加载工作区模块…", "shell.feedback": "反馈",
    "runtime.unavailable": "QuantOS Research API 当前不可用。", "runtime.retryHint": "请检查 Preview 运行状态，然后重试受控健康检查。", "runtime.retry": "重试 API",
    "runtime.demo": "演示数据", "runtime.demoDetail": "合成 fixture 数据 · 不调用实时 Provider · 仅用于产品与研究演示 · 不构成投资建议。",
    "runtime.local": "本地持久化数据",
    "overview.kicker": "公开预览", "overview.title": "可审计的 AI 金融研究工作台",
    "overview.description": "基于结构化事实提问，检查证据与数据来源，并在明确的时间边界下复核历史研究。",
    "overview.start": "开始研究", "overview.report": "查看示例报告", "overview.health": "进程健康",
    "overview.healthDetail": "版本化 {version} 服务已通过 Research API 边界响应。",
    "overview.capabilities": "当前预览能做什么", "overview.capabilitySubtitle": "能力状态直接来自 QuantOS；不可用的数据不会被显示为零。",
    "overview.technical": "技术状态", "overview.technicalHint": "Provider 配置与健康元数据",
    "overview.noProviders": "当前未公开 Provider 健康记录。", "overview.why": "为什么？",
    "status.READY": "可用", "status.PASS": "通过", "status.PARTIAL": "部分可用", "status.DEGRADED": "能力降级", "status.UNAVAILABLE": "不可用", "status.UNCONFIGURED": "未配置", "status.DISABLED": "已禁用", "status.FAILED": "失败",
    "status.readyHelp": "该能力在当前数据与时间边界下可用。",
    "status.partialHelp": "已有可用结果，同时存在明确限制。",
    "status.degradedHelp": "该能力可运行，但覆盖或可靠性有所降低。",
    "status.unavailableHelp": "所需持久化数据或能力不可用；QuantOS 不会编造结果。",
    "status.unconfiguredHelp": "该可选能力未在当前预览环境中配置。",
    "status.disabledHelp": "该能力在当前运行模式中被明确禁用。",
    "cap.evidenceUnavailable": "公开预览未连接外部证据 Provider，因此 QuantOS 不会伪造外部来源。",
    "ask.kicker": "基于事实的提问", "ask.title": "先提问，再检查为什么", "ask.description": "回答按事实、引用、限制和执行链路分层展示。",
    "ask.security": "证券", "ask.question": "研究问题", "ask.offline": "仅使用离线市场上下文", "ask.submit": "向 QuantOS 提问", "ask.loading": "正在生成可核验回答…",
    "ask.examples": "试试示例问题", "ask.example1": "最近一个交易日表现怎么样？", "ask.example2": "最近一个交易日的收盘价是多少？", "ask.example3": "最近一个交易日的价格和成交量是多少？",
    "ask.why": "为什么得到这个答案？", "ask.traceTitle": "研究 Trace", "ask.facts": "事实", "ask.availability": "可用性与限制", "ask.references": "事实与引用",
    "analytics.kicker": "语义分析", "analytics.title": "每个数字都有来源", "analytics.description": "从注册指标与维度构建有界查询；系统不提供任意 SQL 入口。", "analytics.metric": "指标", "analytics.dimension": "维度", "analytics.security": "证券", "analytics.start": "开始日期", "analytics.end": "结束日期", "analytics.sort": "排序方向", "analytics.limit": "行数上限", "analytics.run": "运行分析", "analytics.running": "正在运行有界查询…", "analytics.provenance": "数据来源",
    "reports.kicker": "报告归档", "reports.title": "在上下文中阅读研究报告", "reports.description": "浏览已有报告制品；打开此页面不会生成或刷新报告。", "reports.date": "日期", "reports.security": "证券", "reports.apply": "应用筛选", "reports.list": "每日研究报告", "reports.detail": "报告详情",
    "replay.kicker": "历史回放", "replay.title": "让时间语义清晰可见", "replay.description": "检查冻结的 Campaign、PIT 计数、确定性与结构化失败记录。", "replay.mode": "回放模式", "replay.all": "全部模式", "replay.campaigns": "回放 Campaign", "replay.audit": "回放 Campaign 审计",
    "guide.title": "三步完成一次研究", "guide.ask": "1. 提问", "guide.askText": "从一个可核验的市场问题开始。", "guide.inspect": "2. 检查", "guide.inspectText": "查看事实、来源、限制，以及答案的生成依据。", "guide.replay": "3. 回放", "guide.replayText": "检查历史时点真正可见的信息。", "guide.dismiss": "知道了",
    "feedback.title": "预览反馈", "feedback.helpful": "这次预览对你有帮助吗？", "feedback.yes": "有帮助", "feedback.no": "需要改进", "feedback.notes": "可选反馈", "feedback.placeholder": "告诉我们哪里有帮助或哪里不清楚，请勿填写个人信息。", "feedback.copy": "复制反馈", "feedback.copied": "已复制", "feedback.privacy": "反馈仅保存在浏览器中，直到你主动复制；QuantOS 不收集身份或个人信息。",
    "common.close": "关闭", "common.auditDetail": "审计详情", "common.loading": "正在加载研究上下文…", "common.retry": "重试", "common.copyError": "复制错误详情", "common.noRecords": "没有匹配记录。",
    "reason.ATTRIBUTION_INSUFFICIENT": "当前证据未达到归因政策要求，因此无法提供因果解释。", "reason.EVIDENCE_UNAVAILABLE": "当前时间边界下的历史证据不可用。", "reason.KNOWLEDGE_TEMPORAL_UNVERIFIED": "知识内容缺少可核验的历史可用时间。", "reason.REPORT_NOT_VISIBLE": "所选截止时间下没有可见报告。", "reason.MARKET_DATA_UNAVAILABLE": "持久化市场数据不可用。", "reason.NO_DATA": "数据集可用，但没有记录符合查询条件。", "reason.OK": "已获得基于事实的结果。", "reason.default": "QuantOS 返回了此确定性原因代码。",
  },
};

type I18nValue = { locale: Locale; setLocale: (value: Locale) => void; t: (key: string, variables?: Record<string, string>) => string };
const I18nContext = createContext<I18nValue | null>(null);

function initialLocale(): Locale {
  const saved = window.localStorage.getItem(STORAGE_KEY);
  if (saved === "zh-CN" || saved === "en-US") return saved;
  return window.navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(initialLocale);
  const value = useMemo<I18nValue>(() => ({
    locale,
    setLocale(next) { window.localStorage.setItem(STORAGE_KEY, next); updateLocale(next); },
    t(key, variables = {}) {
      let message = messages[locale][key] ?? messages["en-US"][key] ?? key;
      for (const [name, replacement] of Object.entries(variables)) message = message.replaceAll(`{${name}}`, replacement);
      return message;
    },
  }), [locale]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("I18nProvider is missing");
  return value;
}

export function statusHelpKey(status: string): string {
  const normalized = status.toUpperCase();
  if (normalized === "PARTIAL") return "status.partialHelp";
  if (normalized === "DEGRADED") return "status.degradedHelp";
  if (normalized === "UNCONFIGURED") return "status.unconfiguredHelp";
  if (normalized === "DISABLED") return "status.disabledHelp";
  if (normalized === "UNAVAILABLE" || normalized === "FAILED") return "status.unavailableHelp";
  return "status.readyHelp";
}
