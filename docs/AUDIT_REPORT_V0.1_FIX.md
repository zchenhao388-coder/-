# V0.1 Core Integrity Fix 验收报告

验收日期：2026-08-16

基线：GitHub `main` / `140098fc65e1bfbcbf3052b1b2e3df3e2254de32`

范围：只修复 `AUDIT_REPORT_V0.1.md` 中的 P0 与本轮明确要求的必要 P1；没有开发 Milestone 6–11，没有增加新策略或技术指标，没有改变 PES/LES、Regime、Phase、CRS、Permission 或1进2交易理念。

## 1. 结论

**READY FOR M6**

本轮列出的 P0 已全部关闭；没有发现仍会影响底层策略正确性的未关闭必要 P1。Milestone 1–5 已从“依赖调用者自律的可运行原型”修复为“核心时间边界、Gate、状态持久化和 point-in-time provenance 可由代码与测试强制”的工程底座。

该结论只表示可以安全进入 Milestone 6 的工程开发，不表示可以实盘：生产行情源尚未接入，Eastmoney 仍是实验探针，SEED 阈值尚未完成校准。

## 2. 失败测试基线

生产代码修改前先增加了 16 个 Core Integrity 契约测试。

当前 HEAD 初次运行结果：

```text
Total:  16
Passed:  0
Failed: 15
Errors:  1
```

失败项明确复现了：09:15 A/ARMED、Feature 原始全日数组入口、A1/WAIT、HardCancel 重启失效、日期绕过、缺失流动性记 0、DQS DEGRADED 仍 A、EG 定义错误、Recovery/CloseLocation 错误、简化 HealthyDisagreement、缺少 point-in-time Surprise/Builder、缺少 minimum sample 和 Eastmoney f86→exchange_ts。

随后才修改生产代码。

## 3. 已关闭 P0

### P0-1：竞价时间状态机

状态：已关闭。

修改文件：

- `domain/enums.py`
- `auction/phase.py`
- `auction/features.py`
- `auction/one_to_two.py`
- `execution/compiler.py`
- `app/pipeline.py`

实现：

- 09:15–09:20：`SCOUTING`，Feature Authenticity 强制为 `UNKNOWN`。
- 09:20–09:24:30：`VALIDATING`，允许中间真实性判断。
- 09:24:30–09:25：`FINALIZING`。
- 09:25 起：`FINAL`；09:25 前 Compiler 不产生最终 A1/A2 或 ARMED。
- 09:30 起：`OPEN_EXECUTION`。
- 时间边界是结构性代码，不是可调 SEED。
- 时点必须带时区，并统一转换为 `Asia/Shanghai` 后判定。

覆盖测试：

- `test_structural_auction_phase_boundaries_exist`
- `test_pre20_feature_authenticity_is_unknown`
- `test_pre20_and_pre25_cannot_arm_or_keep_a_grade`
- E2E Case 1 的 09:15/09:20/09:24:30/09:25 四 checkpoint。

### P0-2：唯一 point-in-time 数据边界

状态：已关闭。

修改文件：

- `market/asof.py`
- `market/peer.py`
- `replay/clock.py`
- `auction/features.py`
- `auction/one_to_two.py`
- `expectation/benchmark.py`
- `expectation/surprise.py`
- `app/pipeline.py`

实现：

- 新增只读 `AsOfMarketView`，统一提供 ticks、peer ticks、market context、historical features、expectation observations 和 precomputed features。
- Tick 必须同时满足 `exchange_ts <= as_of` 和 `receive_ts <= as_of`。
- Historical/Expectation/Precomputed 数据必须满足 `available_at <= as_of`，历史交易日必须早于当前交易日。
- Feature Engine 不再接受 `ticks`/peer arrays；只接受 `AsOfMarketView`。
- Peer Engine、Hierarchical Benchmark 和 Surprise Percentile 只接受 `AsOfMarketView`。
- Setup Engine 只接受带 ticker/trade_date/computed_as_of 的 `FeatureSnapshot`，未来快照抛 `FutureDataAccessError`。
- Replay 仍可内部持有完整序列，但策略层入口只生成按当前 VirtualClock 截断的 view。
- AsOfMarketView 不接受无时区 Tick；避免时区混用形成间接泄漏。

对抗测试结果：

```text
VirtualClock / AsOf = 09:21
visible actual gap = 2.0
09:25 tick          = 不可见
future feature      = 不可见
future snapshot     = FutureDataAccessError
future cutoff       = FutureDataAccessError
```

### P0-3：A 当且仅当 ARMED

状态：已关闭。

修改文件：

- `execution/compiler.py`
- `tests/test_compiler_gates.py`
- `tests/test_core_integrity_contract.py`

实现：

- Score Gate 失败时，A1/A2 recommendation 会降级为 B，不再出现 `A1 / WAIT`。
- Rank/Position/DQS cap 失败均返回非 A 与 WAIT。
- 最终成功路径只有 A1/A2 才 ARMED；B/C/DROP 均不能 ARMED。
- Compiler 末端执行显式 invariant：`is_A == is_ARMED`，违反即抛异常。
- A 仍只是执行资格，不是 BUY。

### P0-4：HardInvalid / HardCancel 持久化与身份一致性

状态：已关闭。

修改文件：

- `storage/hard_cancel.py`
- `domain/context.py`
- `execution/compiler.py`
- `app/pipeline.py`

实现：

- 使用 SQLite 持久化 `(trade_date, ticker, NightPlan context)`。
- 新建 Compiler、进程重启或更换 worker 后可重新读取 sticky 状态。
- Compiler 不再接受调用者伪造的 `in_night_pool` 或任意 context id；Night Pool 与 context 均从 `NightPlan` 推导。
- `trade_date`、`NightPlan.trade_date`、`MarketContext.trade_date` 与 `decided_at` 的上海交易日必须一致。
- NightPlan generated_at 不得晚于 information cutoff，cutoff 不得晚于 decided_at。

E2E Case 10：第一次 DQS BROKEN 触发 HARD_CANCELLED，重建 Pipeline/Compiler 后仍为 HARD_CANCELLED。

### P0-5：缺失特征重归一化与 DQS GradeCap

状态：已关闭。

修改文件：

- `domain/models.py`
- `auction/one_to_two.py`
- `execution/compiler.py`
- `market/dqs.py`

实现：

- SetupResult 的组件分数支持 `None`。
- 整组 Liquidity 缺失时 `LiquidityScore=None`，不再记 0。
- AQS 只对可用组件重归一化，并保存 `effective_weights`。
- Liquidity 缺失时实际权重为：Expectation 25%、Authenticity 31.25%、Relative 25%、Context 18.75%。
- DQS DEGRADED 最高 B；DQS BROKEN 直接 HARD_CANCELLED。
- 缺少可靠 exchange_ts 至少为 DEGRADED；经 AsOf 核心链时因 Tick 不可定位会成为 BROKEN。

E2E Case 9：Liquidity 全缺失 → DQS DEGRADED、LiquidityScore=None、最终 B/WAIT。

## 4. 已关闭必要 P1

### EG / NormalizedEG

修改文件：`auction/features.py`、`expectation/benchmark.py`。

```text
EG = ActualAuctionGap - ExpectedGapQ50
NormalizedEG = EG / max(ExpectedGapQ75 - ExpectedGapQ25, epsilon)
```

验证：ExpectedQ50=3%、Actual=5% 时，EG=2pct、NormalizedEG=1.0。

Expectation 带 ticker、trade_date、generated_at、information cutoff、benchmark key 与 source version；Pipeline 校验其不超过 NightPlan cutoff。

### RecoveryRatio / CloseLocation

修改文件：`auction/features.py`。

- 只有出现先高后低的有效回撤才计算 RecoveryRatio。
- 单调上涨、无回撤或 Peak=Trough 返回 None。
- Post20High=Post20Low 时 CloseLocation=None。
- RecoveryRatio 被限制在 0–1。

### HealthyDisagreement

修改文件：`domain/enums.py`、`auction/features.py`、`config/thresholds/one_to_two.yaml`。

完整条件现在要求：有效 Post20 回撤、Recovery 达标、LateSlope>0、回撤低点后 matched_amount 与 matched_volume 均增长、相对排名不低于配置阈值。

价格恢复但确认不全时输出 `HEALTHY_DISAGREEMENT_PENDING`，不能进入真实性 A Gate。

### AuctionSurprisePercentile

修改文件：`expectation/surprise.py`、`expectation/observations.py`。

- 使用历史相似 benchmark 的 Actual Auction Gap distribution。
- 仅使用 `observation.trade_date < current_trade_date` 且 `available_at <= Night information cutoff` 的样本。
- 当天或未来样本不会进入 percentile。

### Night Expectation provenance

修改文件：`expectation/observations.py`、`expectation/benchmark.py`。

Observation 保存：observation_id、trade_date、ticker、完整 `FeaturesAsOf(values/as_of/source)`、information_available_at、benchmark_key、benchmark_key_source、actual_next_auction_gap、label_available_at、source_version、金额与成交量标签。

约束：

- T 特征快照必须早于 Night cutoff。
- T+1 label 必须晚于 cutoff 才可用。
- label/outcome 字段不得进入 `features_as_of`。
- BenchmarkKey 必须能从保存的 T 特征快照逐字段复现。
- provenance 缺失或冲突时拒绝样本。

### Hierarchical Benchmark

修改文件：`expectation/benchmark.py`、`config/thresholds/expectation.yaml`。

- `minimum_local_sample_size=5`，状态为 SEED，并明确 calibration required。
- 当前层不足时逐级放宽，达到 minimum 后才使用。
- 所有层均不足时使用最宽可用分布并降低 confidence。
- prior_strength 和 insufficient confidence multiplier 均配置化。

### Eastmoney 隔离

修改文件：`adapters/base.py`、`adapters/eastmoney.py`、`config/data_sources.yaml`、`market/dqs.py`。

- f86 保存为 `provider_ts`，`exchange_ts=None`。
- capability 明确 `exchange_ts=false`、`auction_semantics_verified=false`。
- `execution_enabled=false`。
- 唯一允许用途：FIELD_PROBE、RAW_CAPTURE、SHADOW_RESEARCH。
- 无可靠 exchange_ts 不能形成可执行 A 数据链。

## 5. 逐 Gate 覆盖

新增 `tests/test_compiler_gates.py`，逐项验证：

- MarketPermission DENY
- SetupPermission DENY
- Regulatory HARD_INVALID
- Candidate HARD_INVALID
- DQS DEGRADED
- DQS BROKEN
- MarketValidation FALSIFIED
- CandidateValidation FALSIFIED
- Authenticity FAILED
- SetupRequirements FAILED
- AQS FAILED
- HVS FAILED
- Percentile FAILED
- CrossSetupRank FAILED
- PositionPermission DENY

所有 Hard Gate 均在 Score 之前停止；高 AQS 无法越权。

## 6. 正式 E2E Replay 矩阵

正式入口：`app/pipeline.py`。

测试入口：`tests/test_e2e_replay_matrix.py`。

链路：

```text
NightPlan
→ ReplayEngine / VirtualClock
→ AsOfMarketView
→ AuctionFeatureEngine
→ OneToTwoEngine
→ DataQualityService
→ DecisionCompiler
```

| Case | 实际验收结果 |
|---|---|
| 1 正常真强 | 09:15/09:20/09:24:30 均不得 A；09:25 A/ARMED |
| 2 Pre20 假顶后撤退 | PRE20_MIRAGE；不得 A |
| 3 Post20 持续衰减 | POST20_CONTINUOUS_DECAY；不得 A |
| 4 回撤后承接恢复 | 全部确认存在时 HEALTHY_DISAGREEMENT |
| 5 MarketValidation FALSIFIED | HARD_CANCELLED；不得 A |
| 6 DQS BROKEN | HARD_CANCELLED |
| 7 Night Pool 外涨停 | C_AUCTION_EMERGENT / WAIT |
| 8 AQS/HVS 不足 | 非 A / 非 ARMED |
| 9 Liquidity 全缺失 | DQS DEGRADED；最高 B |
| 10 HardCancel → restart | 重启后仍 HARD_CANCELLED |
| 11 09:21 未来快照 | FutureDataAccessError |
| 12 无有效回撤 | RecoveryRatio=None |

## 7. Packaging 与全新环境验收

修改文件：`pyproject.toml`、`README.md`。

- 增加 setuptools build-system。
- 明确项目无第三方 runtime dependencies。
- 增加 dev extra、package discovery 与配置文件 package-data。
- README 增加 clone → venv → pip upgrade → editable install → test 全流程。
- 推荐 Python 3.11，最低 Python 3.9。

验收方式：将当前工作树制作成临时干净 Git 快照，从该仓库重新 clone，创建全新 venv，升级 pip/setuptools/wheel，执行标准 `pip install -e .`，再运行全部测试。

结果：

```text
Python:  3.9.6
Install: PASS
Total:   77
Passed:  77
Failed:   0
Skipped:  0
```

## 8. 修改文件汇总

核心新增：

- `app/pipeline.py`
- `auction/phase.py`
- `domain/context.py`
- `market/asof.py`
- `market/peer.py`
- `expectation/observations.py`
- `expectation/surprise.py`
- `storage/hard_cancel.py`
- `config/thresholds/expectation.yaml`

核心修改：

- `auction/features.py`
- `auction/one_to_two.py`
- `execution/compiler.py`
- `expectation/benchmark.py`
- `market/dqs.py`
- `domain/enums.py`
- `domain/models.py`
- `domain/reason_codes.py`
- `adapters/base.py`
- `adapters/eastmoney.py`
- `replay/clock.py`
- `config/data_sources.yaml`
- `config/thresholds/one_to_two.yaml`
- `pyproject.toml`
- `README.md`

测试新增：

- `tests/test_core_integrity_contract.py`
- `tests/test_compiler_gates.py`
- `tests/test_point_in_time_integrity.py`
- `tests/test_e2e_replay_matrix.py`

既有 Feature、Expectation、Replay、1进2/Compiler 和 Adapter 测试已迁移到新 point-in-time API。

## 9. 未关闭问题

### P0

无已知未关闭 P0。

### 影响底层策略正确性的必要 P1

无已知未关闭项。

### 不阻止进入 M6、但阻止实盘的问题

- 没有经过许可、SLA 和完整竞价字段验证的生产行情源。
- Eastmoney 只能做 shadow research。
- SEED 阈值未完成历史校准和样本外验证。
- 尚无生产级日志、指标、告警、运行编排和故障恢复演练。
- REAL_TIME replay 仍直接依赖实际 sleep；当前完整性验收主要使用 FULL_SPEED/STEP/CHECKPOINT。
- 没有正式交易 CLI；按本轮要求留到后续 Milestone。

## 10. 进入 Milestone 6 的边界

可以进入 M6，但必须继续遵守：

1. 新模块只能读取 `AsOfMarketView`，不得重新引入原始全日数组入口。
2. 新 Setup 必须保持 `A ⇔ ARMED`，且不得越过既有 Gate。
3. 新 HardInvalid/HardCancel 必须写入同一持久化状态机制。
4. Eastmoney 不得转为执行源。
5. M6 开发不等于获得实盘资格；真实源验证和阈值校准仍需单独验收。
