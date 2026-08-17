# A股超短决策系统 V0.1 — M6–M11 开发与验收报告

生成日期：2026-08-16  
范围：严格按 `docs/ROADMAP_M6_M11.md` 顺序实现和验证 Milestone 6–11，不重构、不重新解释已验收的 Milestone 1–5。

## 1. Executive Summary

- M6 高标接力 Auction Engine：工程实现与专项测试通过。
- M7 弱转强 Auction Engine：工程实现与专项测试通过。
- M8 Cross-Setup Rank 与 Open Execution：工程实现与专项测试通过。
- M9 Outcome、持久化与可重复 Replay：工程实现与专项测试通过。
- M10 Backtest 与 Calibration：工程实现与专项测试通过。
- M11 生产数据源、监控与应用编排：供应商无关的实时适配边界、强制验收门、监控、熔断和日内编排已实现并通过测试；但仓库没有真实持牌供应商、许可/SLA 证据或完整交易日字段验收材料，因此真实生产数据接入仍为外部阻塞项。

最终测试结果：147 Passed，0 Failed，0 Skipped。

结论：当前代码可以安全用于研究、Replay、Mock E2E、校准框架和 Shadow 编排验证；不能宣称已具备真实行情驱动的实盘执行能力。未发现仍存的已知代码级 P0 或影响策略正确性的 P1；真实供应商未接入是生产启用的 Blocking Issue，当前通过 fail-closed 机制阻止误执行。

## 2. Milestone 6 — 高标接力 Auction Engine

状态：PASS

实现内容：

- 新增高标接力独立 Setup Engine，并复用既有 `AsOfMarketView`、Feature Engine、DQS、Compiler 和持久化 HardCancel。
- 实现既定的高标生态、领导力状态、HVS/AQS、真实性和 FakeStrong 原因码。
- 初始 AQS 权重保持为配置中的 `SEED`，未写死在策略代码。
- 高分不能绕过生态证伪、领导力丢失、DQS、minimum-sample 或 Compiler Gates。

专项测试：11/11 Passed。进入 M7 前全量回归：94/94 Passed。

主要文件：

- `auction/high_board.py`
- `config/thresholds/high_board.yaml`
- `tests/test_high_board.py`

## 3. Milestone 7 — 弱转强 Auction Engine

状态：PASS

实现内容：

- 支持既定三类弱转强状态，不新增第四类策略定义。
- 实现 WeaknessResolved、IdentityRecovery、AQS/HVS、真实性与五类假修复标志。
- 不可修复、身份未恢复、孤立修复、流动性不足、后段衰减等条件不能依靠分数越权。
- 复用既有 Permission 和 Compiler，不复制市场权限层。

专项测试：11/11 Passed。进入 M8 前全量回归：105/105 Passed。

主要文件：

- `auction/weak_to_strong.py`
- `config/thresholds/weak_to_strong.yaml`
- `tests/test_weak_to_strong.py`

## 4. Milestone 8 — Cross-Setup Rank 与 Open Execution

状态：PASS

实现内容：

- 三模式结果按 NightPlan 的 primary setup 归属进行横向排序，其他 setup 只作为辅助证据。
- 账户候选状态只读取 Compiler 产出的 `DecisionTrace`，不能直接相信 Setup Engine 的推荐等级。
- 实现 09:30–09:35 的 `ARMED → EXECUTE/WAIT/CANCEL/EXPIRE` 状态机和结构性时间阶段。
- A 的含义保持为 ARMED，不等于 BUY；C/DROP 不生成开盘执行计划。
- DQS BROKEN 和 setup-specific HardCancel 使用既有持久化机制，当日不可恢复。
- 开盘行情和 peer 数据继续由 `AsOfMarketView` 过滤，不能读取未来 tick。

专项测试：13/13 Passed。进入 M9 前全量回归：118/118 Passed。

主要文件：

- `execution/cross_setup.py`
- `execution/open_execution.py`
- `tests/test_cross_setup_open_execution.py`

## 5. Milestone 9 — Outcome、持久化与可重复 Replay

状态：PASS

实现内容：

- Raw、Derived、Decision、Outcome 使用分层追加写存储。
- Replay Manifest 保存原始数据、配置和代码版本的内容指纹，内容变化时拒绝伪装成同一次可验证回放。
- Manifest identity 为确定性内容身份，不写入本地绝对路径。
- Decision Diff 扩展到等级、取消和触发状态。
- Outcome Engine 仅从 point-in-time view 取数，提供结构化结果标签及 FP/FN 诊断。

专项测试：7/7 Passed。进入 M10 前全量回归：125/125 Passed。

主要文件：

- `storage/jsonl.py`
- `replay/manifest.py`
- `replay/diff.py`
- `backtest/outcome.py`
- `config/thresholds/outcome.yaml`
- `tests/test_outcome_persistence_replay.py`

## 6. Milestone 10 — Backtest 与 Calibration

状态：PASS

实现内容：

- Walk-forward 窗口严格按时间顺序划分，不使用随机切分。
- 训练窗口只能使用在验证 cutoff 前已经可获得的标签；标签字段禁止进入特征。
- Trainer 只能接触训练集，forward 样本保持未触碰。
- 实现漂移检查、样本外指标和阈值版本存储。
- 阈值只允许按证据从 `SEED` 晋级为 `CALIBRATED` 或转为 `RETIRED`；测试没有用合成样本修改仓库内真实配置状态。
- 保持既定校准优先级：单参数、交互项、权重。

专项测试：8/8 Passed。进入 M11 前全量回归：133/133 Passed。

主要文件：

- `backtest/walk_forward.py`
- `backtest/calibration.py`
- `config/thresholds/calibration.yaml`
- `tests/test_backtest_calibration.py`

## 7. Milestone 11 — 生产数据源、监控与应用编排

状态：ENGINEERING PASS / LIVE VENDOR BLOCKED

实现内容：

- 定义供应商无关的 `RealtimeAdapter` 连接、订阅、流式行情、最新值、健康检查和关闭接口。
- `ExecutionDataSourceGuard` 在传输层连接前检查：数据源身份、执行许可、供应商许可、SLA、合同有效期、竞价语义、exchange timestamp、完整交易日证据版本和所有 canonical core 字段的逐字段验收证据。
- 验收证据必须早于运行时点；重复字段证据、无时区运行时点、过期合同或证据版本不一致均失败关闭。
- `CanonicalAuctionMapper` 对缺失 enhanced 字段输出 `None`，不填 0。
- Eastmoney 名称被执行验收门结构性拒绝，且配置继续保持 `execution_enabled=false`。
- `FeedHealthMonitor` 只接受 `AsOfMarketView`，按可见 tick 评估延迟和核心字段缺失。
- DQS BROKEN 打开数据熔断；恢复需要配置数量的连续健康样本，并再次通过生产数据源认证。
- `ApplicationOrchestrator` 覆盖盘前、09:15、09:20、09:24:30、09:25、09:30–09:35、盘中空闲和盘后 checkpoint；同一交易日同一 checkpoint 幂等。
- 健康报告时点必须与当前 checkpoint 精确一致，旧报告或未来报告不能授权运行。
- 默认 Shadow Mode；`auto_order=true` 被结构性拒绝。Handler 只产生信号，不发送委托。

专项测试：14/14 Passed。最终全量回归：147/147 Passed。

主要文件：

- `adapters/base.py`
- `adapters/production.py`
- `monitoring/feed.py`
- `app/orchestrator.py`
- `config/data_sources.yaml`
- `config/system.yaml`
- `config/thresholds/monitoring.yaml`
- `tests/test_production_monitoring_orchestration.py`

## 8. Core Integrity Regression

以下已验收 invariant 在最终全量测试中持续通过：

| Invariant | 验证结果 |
|---|---|
| 策略时点数据统一经 `AsOfMarketView`，未来 tick/feature/observation 不可见 | PASS |
| 09:25 Compiler 保持 A ⇔ ARMED | PASS |
| 09:25 前不得产生最终 A/ARMED | PASS |
| Benchmark 或 AuctionSurprise minimum-sample 不足不得 A/ARMED | PASS |
| DQS DEGRADED 最高 B；BROKEN HardCancel/禁止执行 | PASS |
| HardInvalid/HardCancel 跨 Pipeline、Compiler、Open Execution 重建仍持久 | PASS |
| NightPlan context identity deterministic；等价重建相同、真实不同不继承 | PASS |
| Eastmoney 不作为 execution source，`f86` 不映射为 exchange timestamp | PASS |
| 新 Setup 通过既有 Compiler Gates，AQS/HVS 不能覆盖 Permission/Validation | PASS |
| Open Execution 的 A 仅表示 ARMED，不等于无条件买入 | PASS |

## 9. Test Evidence

执行命令：

```bash
python3 -B -m unittest tests.test_production_monitoring_orchestration -v
python3 -B -m unittest discover -s tests -v
```

最终结果：

- M11 专项：14 Passed，0 Failed，0 Skipped。
- 全部 regression：147 Passed，0 Failed，0 Skipped。
- `git diff --check`：通过。

测试覆盖的关键路径包括：三套 Setup Gate、未来数据防护、minimum-sample、DQS 降级/损坏、HardCancel 持久化、NightPlan identity、Cross-Setup、开盘状态机、Outcome point-in-time、Replay 指纹、Walk-forward 标签可得性、生产数据源认证、Eastmoney 禁行、熔断恢复、Shadow 编排和自动下单禁令。

## 10. Blocking Issues

### B-M11-01 — 未接入真实持牌实时行情供应商

仓库只有禁行门、canonical mapper、RealtimeAdapter 边界和关闭状态的供应商模板。缺少：

- 用户实际购买或获授权的数据源及凭据；
- 可核验的许可和 SLA 文档引用；
- 交易日 09:15–09:25 连续采样；
- virtual price、matched volume、matched amount 和 exchange timestamp 的供应商字段语义验收记录；
- 多交易日延迟、断流、重连、乱序和修订行为验证。

因此 `config/data_sources.yaml` 中 production template 保持 `configured=false`、`execution_enabled=false`。这是正确的失败关闭状态，不应为“让系统跑起来”而绕过。

## 11. Known P0 / P1

- 已知代码级 P0：无。
- 已知影响当前已实现策略正确性的 P1：无。
- 生产启用阻塞：存在，即 B-M11-01。它不会导致当前代码错误交易，因为认证门会在连接和编排阶段停止，但在完成真实数据源验收前不得进入实盘执行。
- 模型证据限制：M10 的机制已完成，但尚未使用真实、足量、严格 point-in-time 的历史数据把主要 `SEED` 阈值正式晋级为 `CALIBRATED`。这不是框架 Bug，但限制实盘有效性声明。

## 12. Final Acceptance Decision

- M6–M10：ACCEPTED。
- M11 工程代码：ACCEPTED。
- M11 真实供应商接入：BLOCKED，等待外部数据源和验收证据。
- 当前允许用途：研究、Mock/Replay、Backtest/Calibration framework、Shadow Mode。
- 当前禁止用途：真实行情驱动的自动执行或任何自动委托。

本轮到此停止，不继续扩展新策略或重新设计 M1–M5。
