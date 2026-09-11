# 核心概念

[English](concepts.md) | [简体中文](concepts.zh-CN.md)

- **事件时间：** 被解释的市场事件时点。
- **可知时间：** 信息实际可被QuantOS获知的时间。
- **运行截止点：** 显式 `as_of_time`；不能反向改写事件时间。
- **Evidence：** 可能通过规则成为Attribution Evidence的事件记录。
- **Knowledge：** 从精确本地corpus/index检索的背景资料。
- **Historical lane：** 在市场事件截止点前可知的Knowledge。
- **Retrospective lane：** 仅RESEARCH可用、事件后但显式research cutoff前可知的Knowledge。
- **Guarded Synthesis：** 对引用、因果状态、schema和输入identity进行确定性校验的受控文本生成。
- **Product identity：** Daily或TimeSlice内容的逻辑身份。
- **Artifact identity：** 持久化bytes的SHA-256身份。
- **Operational state identity：** 被观察产品运行状态的身份。
- **Run identity：** 执行意图与上下文，而不是产品内容身份。
