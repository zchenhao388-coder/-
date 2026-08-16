# Milestone 6–11 工程路线

以下仅列工程实现顺序，不新增或修改已锁定的策略定义。

## Milestone 6 — 高标接力 Auction Engine

按既有 Permission、Validation、Authenticity、HVS/AQS 和 Grade 规则实现，并复用 Canonical Tick、Feature Engine、DQS、Replay 与 Compiler。

## Milestone 7 — 弱转强 Auction Engine

实现既有弱转强假设验证、真实性判定、评分与原因码；不得复制一套市场权限层。

## Milestone 8 — Cross-Setup Rank 与 Open Execution

接通三模式横向排序，以及 09:30–09:35 `ARMED → EXECUTE/WAIT/CANCEL/EXPIRE` 状态机；A 仍不等于无条件买入。

## Milestone 9 — Outcome、持久化与可重复 Replay

补齐原始/派生/决策/结果分层存储、回放清单、版本指纹和端到端 Decision Diff。

## Milestone 10 — Backtest 与 Calibration

实现无未来数据的滚动样本、阈值版本管理、样本外评估、漂移检查，并把 SEED 晋级为 CALIBRATED 或 RETIRED。

## Milestone 11 — 生产数据源、监控与应用编排

接入有许可和 SLA 的实时行情，完成字段语义验收、延迟/缺失监控、降级与熔断、盘前到盘后的应用编排；未经验证的数据能力不能自动放行执行。
