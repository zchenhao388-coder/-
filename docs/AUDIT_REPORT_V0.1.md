# A股超短决策系统 V0.1 工程审计报告

审计对象：GitHub 仓库 `https://github.com/zchenhao388-coder/-`

审计基准：`main` / `140098fc65e1bfbcbf3052b1b2e3df3e2254de32`

审计日期：2026-08-16

审计范围：Milestone 1–5、工程可运行性、Replay 防未来数据、七个竞价案例、Eastmoney 实验 Adapter。未开发 Milestone 6–11，未修改策略逻辑。

严重级别：

- `P0`：可能导致错误交易、未来数据泄漏或 Permission/执行约束越权。
- `P1`：可能导致模型或策略判断错误。
- `P2`：工程可靠性、可运行性或可观测性问题。
- `P3`：可维护性或使用体验问题。

## 1. Executive Summary

当前项目是一个可导入、可运行 21 个单元测试的 Milestone 1–5 原型库，但不是可安全实盘运行的完整系统，也不能证明已严格实现锁定的 V0.1 约束。

正确完成的基础包括：主要 Domain 枚举和模型、Canonical `AuctionTick`、Mock/Replay Adapter、DQS 基础检查、VirtualClock、按时间有序的 Post20MDD、五类 FakeStrong 标志、分层条件基准与 shrinkage 元数据、1进2 AQS 配置权重、09:25 Compiler 的名义 Gate 顺序，以及 Night Pool 外最高 `C_AUCTION_EMERGENT`、DQS BROKEN 禁止执行、MarketValidation 无效禁止执行。

但审计通过实际代码与反向运行复现了 5 个阻断问题：

1. `P0` 09:15 可直接得到 `A1 / ARMED`，违反 09:20 前仅侦察。
2. `P0` Replay 的时间边界没有强制传递到 Feature/Peer/Expectation；在虚拟时钟 09:21 时，可把原始全日数组直接交给 Feature Engine 并读出 09:25 的 EG。
3. `P0` AQS/HVS/Percentile Gate 失败时，最终 Grade 仍可能为 `A1`，只是 ExecutionState 为 `WAIT`，破坏“A即执行资格”的语义。
4. `P0` HardCancel/HardInvalid 只保存在单个 `DecisionCompiler` 实例内；进程重启后同日股票可恢复为 `ARMED`。
5. `P0` 缺失流动性字段时没有移除特征并重归一化；DQS 可为 DEGRADED，流动性按 0 分继续用原权重，实测仍得到 `A2 / ARMED`。

此外有多个 `P1`：EG 定义错误、RecoveryRatio 无回撤时返回 1.0、HealthyDisagreement 未要求 LateSlope 和资金确认、AuctionSurprisePercentile 只有接口没有实现、Eastmoney `f86` 被直接放入 `exchange_ts`、字段语义未验证等。

结论：**不建议现在进入 Milestone 6，也不能接实盘执行。应先修复所有 P0，再修复与特征定义及真实数据语义相关的 P1，并补齐端到端防未来数据测试。**

## 2. 已正确实现

### 工程与安全

- GitHub `main`、本地 `origin/main` 与审计目录 HEAD 一致，均为 `140098f`。
- 目录按 `config/`、`domain/`、`adapters/`、`expectation/`、`auction/`、`execution/`、`storage/`、`replay/`、`backtest/`、`monitoring/`、`app/`、`tests/` 分层，作为原型库结构基本合理。
- 未发现 `/Users/zch/...`、`/home/...` 或其他硬编码绝对路径。
- 未发现已提交的密钥、Token、Cookie、私钥、`.env`、缓存、虚拟环境、日志、数据库或原始行情文件。
- `.gitignore` 已覆盖 Python 缓存、测试缓存、虚拟环境、构建产物、环境变量文件、证书、日志、JSONL、SQLite/DB、`storage/raw/`、`data/`、编辑器文件以及 Codex/ChatGPT 本地元数据。

### Milestone 1 — Domain

- 已定义要求的 `SetupType`、`CandidateGrade`、`ValidationState`、`AuthenticityState`、`ExecutionState`、`DataQualityState`、`LeadershipState`。
- 已定义 `MarketContext`、`NightPlan`、`AuctionTick`、`CandidateAuctionState`、`SetupResult`、`OpenExecutionPlan`、`DecisionTrace`、`Outcome`、`DataQualityReport`。
- `ThresholdStatus` 已支持 `STRUCTURAL / SEED / CALIBRATED / RETIRED`，读取 RETIRED 阈值会拒绝。
- `ReasonCode` 已为 Compiler 主要 Gate 提供枚举值，不完全依赖自由文本。

### Milestone 2 — Data Layer

- Canonical `AuctionTick` 的 Core 与 Enhanced 字段存在。
- 缺失字段保留为 `None`，没有在模型或 Eastmoney 映射中填 0。
- `DataCapability`、`BaseAdapter`、`MockAdapter`、`ReplayAdapter`、`DataQualityService` 已实现。
- Compiler 对 `DQS = BROKEN` 会在 Score 前直接 `HARD_CANCELLED`。

### Milestone 3 — Replay

- `VirtualClock`、`FutureDataAccessError` 和可见 Tick 集合存在。
- 通过 `ReplayEngine.get_ticks()` 请求晚于虚拟时钟的数据会抛出异常。
- `STEP`、`CHECKPOINT`、`FULL_SPEED`、`REAL_TIME` 分支均有代码；STEP 和 CHECKPOINT 有基础测试。
- `Decision Diff` 能报告简单嵌套字段路径差异。

### Milestone 4 — Feature Engine

- 已输出 Pre20Peak、Pre20Decay、ESR、Post20Delta、Post20MDD、CloseLocation、RecoveryRatio、Late30/60 Delta 和 Slope、金额/成交量比值与历史/Peer 百分位、Theme/Global/Height Peer Rank。
- Post20MDD 使用运行高点后再计算后续低点，正确满足“先高后低”，不是简单 `max - min`。
- 已实现 `PRE20_MIRAGE`、`POST20_CONTINUOUS_DECAY`、`PRICE_WITHOUT_LIQUIDITY`、`ISOLATED_STRENGTH`、`LAST_SECOND_SPIKE` 五种标志。

### Milestone 5 — Night Expectation 与 1进2

- `BenchmarkKey` 有逐级放宽的层次键。
- 输出 Q10/Q25/Q50/Q75/Q90、金额/成交量分位数、sample_size、parent_sample_size、peer_similarity、shrinkage_weight、confidence。
- `available_at <= information_available_at` 的基础过滤已实现，并有排除未来 observation 的测试。
- 1进2 AQS 五项权重为 20/25/20/20/15，来自配置并标记 `SEED`，不是写死在评分公式中。
- Compiler 的代码 Gate 顺序为：NightPool → MarketPermission → SetupPermission → Regulatory → DQS → MarketValidation → CandidateValidation → Authenticity → SetupRequirements → AQS/HVS/Percentile → CrossSetupRank → PositionPermission。
- Permission DENY、Validation INVALID/HARD_INVALID、DQS BROKEN 和真实性失败不会被高 AQS 越过。
- 成功 A 路径输出 `ARMED`，没有直接输出 `BUY`。

## 3. 部分实现

- `P2` README 只给出测试和实验数据探针命令，没有新 Mac 从 clone、选择 Python、创建环境、安装、配置、运行 Replay、日志位置及常见错误的完整步骤。
- `P2` 项目从仓库根目录可直接运行测试，但不能可靠安装：`pyproject.toml` 没有 `[build-system]`、依赖声明、lockfile 或 console scripts；在全新 venv 中执行本地安装失败，报 `invalid command 'bdist_wheel'`。
- `P2` `requires-python = ">=3.9"` 范围过宽且未给出已验证版本矩阵；审计仅在 macOS Python 3.9.6 验证。
- `P3` 有测试入口，但没有统一 main/CLI、Replay CLI、配置装载入口或日志入口。`app/probe_data_source.py` 只是 Eastmoney 探针。
- `P3` `backtest/`、`monitoring/` 等部分目录只有包标记，不代表有可用功能。
- `P2` `config/system.yaml` 定义 decision/authenticity 时间，但实际 Feature/Compiler 不读取；配置文件后缀是 YAML，当前 loader 实际使用 JSON 解析，仅能读取 JSON 语法子集。
- `P2` ReasonCode 只在 Compiler 部分结构化；`DecisionTraceStep.reason`、DQS reasons、SetupResult flags 仍是字符串容器，缺少端到端类型约束。
- `P1` 分层基准存在 hierarchy 和 shrinkage，但没有“达到最小样本数再停”的逐级放宽规则；即使叶子只有 1 个样本也会使用叶子后 shrink。
- `P1` confidence 只是样本数与层级的公式，没有数据质量、时间衰减、分布稳定性或样本可用性证据。
- `P1` Historical/Peer liquidity 分别计算金额和成交量百分位；没有一个明确、可校验的综合 liquidity percentile 定义。
- `P1` `AuctionAmountRatio`/`AuctionVolumeRatio` 使用调用方传入历史数组的中位数，不是语义明确的 Yesterday 比值。
- `P2` DataCapability 是 Adapter 声明，不校验实际 Tick 完整性；Replay/Eastmoney 均可能声明 Core 字段可用而具体 Tick 为 None 或语义未验证。
- `P2` Decision Diff 可比较基础对象，但未形成可运行的历史 replay-to-decision 对比流程。
- `P2` REAL_TIME 直接调用真实 `sleep`，没有可注入 sleeper、倍速或中断策略；无法用于快速、稳定的自动化验证。
- `P2` CHECKPOINT 只选择调用时第一个 `>= clock.now` 的目标；调用方重复传入包含当前时间的完整 checkpoint 列表时可能停在同一 checkpoint。

## 4. 未实现

- `P0` 没有统一的 Replay/Decision orchestration 强制所有 Feature、Peer、Expectation 读取同一个 `as_of` 数据视图。
- `P0` 没有持久化的 HardInvalid/HardCancel 状态存储与恢复。
- `P0` 没有 09:20 前禁止最终真实性、09:25 前禁止最终 Grade/ARMED 的时间 Gate。
- `P1` `AuctionSurprisePercentile` 只有 Protocol，没有任何实现、历史分布存储或 as-of 数据构建流程。
- `P1` 没有可审计的 Night Expectation 历史样本构建器，无法保证 key 特征和 `available_at` 是当时信息而非事后回填。
- `P1` 没有完整的 Peer Engine；各类 peer population 由调用方直接传入。
- `P2` 没有从 NightPlan → Replay → Feature → Setup → Compiler → OpenExecutionPlan → Outcome 的正式应用入口。
- `P2` 没有日志、结构化审计事件、运行指标和异常告警入口。
- `P2` 没有生产数据源，也没有 09:15–09:25 完整交易日捕获和字段验证工具链。
- `P3` 没有 lockfile、CI、lint/type-check/coverage 配置或可发布包的完整 metadata。

## 5. Bug

### P0

- `P0` **Score Gate 失败仍保留 A Grade。** `execution/compiler.py:64-67` 在 AQS/HVS/Percentile 不通过时把 `trace.grade` 设为 `setup_result.grade_recommendation`。反向测试得到 `A1 / WAIT`。这使 A 不再唯一表示 ARMED/执行资格。
- `P0` **09:15 可输出 A1/ARMED。** Compiler 不检查 `decided_at`，Feature Engine 对只有 Pre20 数据且无 flag 的输入返回 AUTHENTIC。反向测试在 09:15 得到 `A1 / ARMED`。
- `P0` **HardCancel 重启失效。** `execution/compiler.py:30` 仅用进程内 `set` 保存状态。重新实例化 Compiler 后，同日同票从 `HARD_CANCELLED` 恢复为 `ARMED`。
- `P0` **trade_date 可绕过 sticky key。** HardCancel key 直接信任调用方 `trade_date`，没有与 `MarketContext.trade_date`、`decided_at` 进行一致性校验。
- `P0` **缺失流动性仍可执行 A。** 1进2 `_mean_percentiles()` 在整个特征组缺失时返回 0，AQS 仍按完整五项固定权重求和；DQS DEGRADED 不阻断。反向测试在金额和成交量均缺失时得到 liquidity=0、AQS=79、`A2 / ARMED`。

### P1

- `P1` **EG 定义错误。** `auction/features.py:59` 把 EG 直接设为实际 `gap_pct`；按锁定定义应为 Actual Auction − Conditional Expected Auction。当前只有 NormalizedEG 做了相对 Q50/IQR 处理。
- `P1` **RecoveryRatio 无有效回撤时错误为 1.0。** `auction/features.py:175` 对单调上涨或峰值后无下降返回 1.0；应为 N/A/None。七案例中的正常真强、Market Falsified、DQS BROKEN、Outside Pool 均出现该值。
- `P1` **CloseLocation 在无区间时返回 1.0。** 平坦序列没有可识别的位置，当前会被解释为收在最高位置。
- `P1` **HealthyDisagreement 条件不足。** `auction/features.py:207-208` 只检查 drawdown 和 recovery，没有强制 LateSlope、资金/流动性确认，也没有显式“承接”状态，因此价格反弹即可被识别为 HealthyDisagreement。
- `P1` **Pre20 Mirage 的结束参照可能早于 09:20。** `_value_at_or_before(09:20)` 在没有 09:20 Tick 时会使用更早的最后一笔，可能混淆缺数与真实撤退。
- `P1` **缺少流动性数据会使 PRICE_WITHOUT_LIQUIDITY 和 LAST_SECOND_SPIKE 静默不触发。** `liquidity_pct is not None` 是触发前提，未知数据没有转成 Unknown/降级真实性。
- `P1` **Eastmoney f86 被映射为 exchange_ts。** capability 已承认它只是 provider quote timestamp 且 exchange-origin 未被文档保证，但 `fetch_one()` 仍写入核心 `exchange_ts`，随后 DQS 会按交易所时间处理。

### P2/P3

- `P2` 全新 venv 安装失败，导致“clone 后安装运行”不可复现。
- `P2` DQS 的 broken 阈值使用 `<`；完整度恰好等于 0.50 时为 DEGRADED 而不是 BROKEN，边界语义没有测试或文档。
- `P2` Eastmoney `load()` 在 exchange_ts 为 None 时忽略 start/end 过滤直接 yield，可能把不可定位时间的快照送入下游。
- `P3` `pyproject.toml` 的 `[tool.unittest]` 不是标准 unittest 自动读取配置，不能代替测试入口脚本。

本轮没有修改上述 Bug；只新增本审计报告，避免“为了测试通过”改变策略代码。

## 6. Look-ahead / Replay 风险

- `P0` **Replay 边界可被直接绕开。** `ReplayEngine` 自己保护 `visible_ticks/get_ticks`，但 Feature Engine 接收任意 `Sequence[AuctionTick]` 且没有 `as_of`/clock。实测虚拟时钟停在 09:21，visible EG=2.0；把原始全日 ticks 直接传给 Feature Engine 后 EG=9.0（读取 09:25）。
- `P0` **全日数组仍驻留在 ReplayEngine `_ticks`，且原始调用方数组也继续可用。** 当前架构依赖调用者自律，不是“绝对无法读取未来数据”。
- `P0` **Peer population 无时间边界。** historical amounts/volumes、peer gaps 都由调用方传入，可能是全日或事后汇总数据。
- `P0` **Expectation 没有共享 VirtualClock。** 虽有 `available_at` 过滤，但 `information_available_at` 由调用方传入，无法阻止错误 as-of 或未来派生 key。
- `P0` **历史样本无 provenance。** `BenchmarkObservation.available_at` 只是一个字段，没有构建期校验，`next_gap_pct` 与条件 key 可由事后数据拼装；无法证明无 Look-ahead Bias。
- `P1` **预计算特征没有 as-of 元数据。** Setup Engine 只接收 Mapping，不能判断 NormalizedEG、PeerRank、AuctionSurprisePercentile 是否来自未来。
- `P2` 现有 future-access 测试只验证 `ReplayEngine.get_ticks(future)` 抛错，没有尝试从 Feature/Peer/Expectation 绕过，因此给出了过强的安全感。

## 7. Strategy Constraint Violations

- `P0` **09:15–09:20 只能侦察：违反。** Pre20-only Feature 返回 AUTHENTIC，Compiler 可在 09:15 产生 A1/ARMED。
- `P0` **A = ARMED/Execution Permission：部分违反。** 成功路径正确为 ARMED，但 Score Gate 失败可输出 A1/WAIT。
- `P0` **HardInvalid/HardCancel 当日不可恢复：仅单进程内成立。** 重启、故障恢复或更换 worker 后失效。
- `P0` **缺失特征应移除并重归一化：违反。** 整组缺失被记 0 分且固定权重保留。
- `P0` **所有组件统一防未来数据：违反。** Feature、Peer、Expectation 没有强制共享时间视图。
- `P1` **EG = Actual − Conditional Expected：违反。** 当前 EG 是 Actual gap。
- `P1` **HealthyDisagreement 必须回撤→承接→恢复→LateSlope→资金确认：违反。** 当前只验证回撤和恢复比例。
- `P2` **所有阈值配置化：部分违反。** 09:20、Late 30/60 秒、expectation score 的 50/25 变换、Validation 分数 100/50/25/0 等仍写死在策略代码；`config/system.yaml` 未被使用。
- `P2` **阈值状态分类：结构存在但使用不完整。** 当前实际阈值全部标为 SEED，没有任何 STRUCTURAL/CALIBRATED/RETIRED 实例来验证生命周期。
- 正确：Night Pool 外股票实测最终 `C_AUCTION_EMERGENT / WAIT`，不能进入 A/B 执行池。
- 正确：DQS BROKEN 实测 `DROP / HARD_CANCELLED`。
- 正确：MarketValidation INVALID 实测高 AQS 仍 `DROP / HARD_CANCELLED`。
- 正确：Compiler 的 Permission 与 Validation Gate 在 Score 前，未发现“Permission=False 但 AQS 高仍 ARMED”的代码路径。

## 8. Data Source Risks

`EastmoneySnapshotAdapter` 的类名、adapter_name、README 和 notes 均明确标为 experimental；当前配置 active source 是 mock，没有代码显式把它标成 production。问题在于当前没有配置装载/运行时 guard，因此“experimental”主要是说明文字，不能阻止误接执行链。

- `P0` 未验证 f86 却写入 canonical `exchange_ts`；在交易决策系统中，provider timestamp 与 exchange timestamp 混同会破坏 DQS、Replay 顺序和时点边界。
- `P1` f43 被假设为 virtual price，但注释也承认 phase-dependent；必须分别验证 09:15–09:20 和 09:20–09:25。
- `P1` f47 被假设为虚拟匹配量并乘 100（手→股），单位和竞价阶段含义未经实盘抓取验证。
- `P1` f48 被假设为虚拟匹配金额，尚未证明是直接字段、累计值还是由价格/量派生。
- `P1` f43/f60 派生 gap_pct 的复权、涨跌停、停牌、无报价状态语义未验证。
- `P1` 上交所、深交所、北交所映射与字段一致性未做交易日覆盖测试。
- `P2` 接口未文档化、无许可/SLA，存在 schema 变更、频率限制、延迟与封禁风险。
- `P2` Adapter 逐票串行抓取，没有批次时点一致性、重试、退避、速率限制、断线恢复或连续 09:15–09:25 采集。
- `P2` 原始 payload 虽放入 tick.raw，但探针没有调用 RawTickJsonlStore 永久落盘，不能形成字段验证证据链。
- `P2` f124 已请求但未使用；其含义和与 f86 的关系未验证。

交易日 09:15–09:25 必须验证：

1. f43 是否在两个竞价阶段持续代表可成交虚拟价，何时为空、冻结或切换语义。
2. f47 的匹配量含义、单位、是否允许随撤单下降、是否为累计成交量。
3. f48 的匹配金额含义、单位、与 f43×f47 的关系。
4. f86/f124 的来源、时区、粒度、单调性、更新频率、与本地 receive_ts 的延迟分布。
5. 一字涨停/跌停、无匹配、停牌、集合竞价取消单、开盘瞬间、异常响应和缺字段场景。
6. SH/SZ/BJ 多股票同时采样的一致性、请求频率上限与稳定性。

在完成这些验证前，Eastmoney Adapter 必须继续保持 `EXPERIMENTAL`，且不应参与任何可执行 A 的数据链。

## 9. Test Coverage Gaps

### 实际测试结果

从 GitHub 当前 HEAD 重新 clone 到全新临时目录后运行：

```text
Total:   21
Passed:  21
Failed:   0
Skipped:  0
Runtime: 0.002s
Python:  3.9.6
```

现有测试实际覆盖：Replay checkpoint 边界、直接 future get_ticks 拒绝、Decision Diff 基础路径、None 与 DQS BROKEN、Mock 过滤、Expectation future observation 过滤和 shrinkage 元数据、ordered MDD、Pre20 Mirage、Post20 decay、基础 HealthyDisagreement、历史/Peer 流动性分离、正常真强 A、DQS BROKEN、同进程 HardCancel sticky、Night Pool cap、Percentile Gate 标志、Market invalid override、Eastmoney 基础字段映射。

21 个测试全部通过，但多数是单函数/手工对象的快速测试；0.002 秒的总耗时也说明没有真实 I/O、全链路 replay、安装、持久化或长序列验证。

### 未覆盖或覆盖不足

- `P0` 没有 09:20 前不得 AUTHENTIC/REAL_STRONG/ARMED 的测试。
- `P0` 没有 Replay → Feature/Peer/Expectation 统一 as-of 防泄漏测试。
- `P0` 没有 Score Gate 失败时 Grade 绝不能为 A 的断言；现有测试只断言 WAIT。
- `P0` 没有 HardCancel 跨重启/跨 worker/持久化测试。
- `P0` 没有缺失特征移除与权重重归一化测试。
- `P0` 没有 trade_date 与 decided_at/context 不一致测试。
- `P1` 没有 EG = Actual − Conditional Expected 的测试。
- `P1` 没有 RecoveryRatio 无回撤为 None 的测试。
- `P1` HealthyDisagreement 测试没有验证承接、LateSlope 与资金确认，因而只证明了简化规则可运行。
- `P1` 没有 AuctionSurprisePercentile 的历史 as-of 实现测试。
- `P1` 没有 MarketPermission DENY、SetupPermission DENY、Regulatory HARD_INVALID、Candidate HARD_INVALID、SetupRequirements、CrossSetupRank、PositionPermission 的逐 Gate 测试。
- `P1` Outside Pool 测试通过手工替换 flag 模拟涨停，没有从 NightPlan 和真实竞价序列构造。
- `P2` 没有 FULL_SPEED、REAL_TIME、重复 CHECKPOINT、空 Tick、跨 ticker、乱序 receive_ts 的完整模式测试。
- `P2` 没有 Raw Storage、Outcome、OpenExecutionPlan、配置加载、日志、CLI、安装和全新 clone smoke test。
- `P2` Eastmoney 测试只使用固定 mock payload，不能验证真实交易日语义。
- `P3` 没有 coverage 报告、静态类型检查、lint 或 CI。

### 审计用七案例端到端运行

仓库没有正式端到端入口，因此审计脚本把现有真实类按 NightPlan → Benchmark → Replay checkpoints → Feature → OneToTwo → DQS → Compiler 串联。结果如下：

| Case | Grade | Execution | Validation | Authenticity | AQS | HVS | ReasonCodes |
|---|---|---|---|---|---:|---:|---|
| 1 正常真强 | A1 | ARMED | VALID | AUTHENTIC | 93.7500 | 100.0 | — |
| 2 09:20前假顶后撤退 | DROP | HARD_CANCELLED | VALID | SUSPICIOUS | 66.8333 | 40.0 | AUTHENTICITY_FAILED；flag=PRE20_MIRAGE |
| 3 09:20后持续衰减 | DROP | HARD_CANCELLED | VALID | FAKE_STRONG | 63.6667 | 0.0 | AUTHENTICITY_FAILED；flag=POST20_CONTINUOUS_DECAY |
| 4 回撤后重新承接 | A1 | ARMED | VALID | HEALTHY_DISAGREEMENT | 92.0000 | 90.0 | — |
| 5 个股强但 Market Falsified | DROP | HARD_CANCELLED | VALID | AUTHENTIC | 93.7500 | 100.0 | MARKET_VALIDATION_FAILED |
| 6 DQS BROKEN | DROP | HARD_CANCELLED | VALID | AUTHENTIC | 50.0000 | 100.0 | DATA_QUALITY_BROKEN |
| 7 Night Pool 外竞价涨停 | C_AUCTION_EMERGENT | WAIT | VALID | AUTHENTIC | 100.0000 | 100.0 | OUTSIDE_NIGHT_POOL |

需要注意：Case 4 只能证明当前简化条件返回 HealthyDisagreement，不能证明满足锁定的完整语义；所有 Case 在只输入 09:15 Tick 时都返回 AUTHENTIC，这是明确失败信号。

### 额外反向验证

```text
09:15 compile:             A1 / ARMED
虚拟时钟 09:21 visible EG: 2.0
同一时刻传全日数组 EG:      9.0
低 percentile score gate:  A1 / WAIT
金额/量缺失:                DQS DEGRADED, liquidity=0, AQS=79, A2 / ARMED
无有效回撤 RecoveryRatio:   1.0
HardCancel 后重建 Compiler: ARMED
```

## 10. Blocking Issues

进入 Milestone 6 或实盘前必须解决：

1. `P0` 建立唯一的 as-of 数据访问边界；Feature/Peer/Expectation 不能接收未经时间裁剪的任意全日集合。
2. `P0` 增加结构性时点 Gate：09:20 前真实性只能 UNKNOWN/SCOUTING，09:25 前不能给最终 A/ARMED。
3. `P0` 修复 Score Gate 失败仍保留 A Grade；建立 `A ⇔ ARMED` 的不变量测试。
4. `P0` 将 HardInvalid/HardCancel 持久化，并校验 trade_date/context/decided_at 一致性。
5. `P0` 对缺失特征执行组件移除与剩余权重重归一化；明确 DQS DEGRADED 对执行的限制。
6. `P0` 建立 Night Expectation 历史样本的 point-in-time 构建和 provenance 校验。
7. `P1` 修正 EG、RecoveryRatio 和 HealthyDisagreement 的锁定定义。
8. `P1` Eastmoney 保持完全隔离，不允许其未验证 timestamp/字段进入可执行链。

## 11. Non-blocking Issues

- `P2` 完整 Python packaging、build-system、依赖/lockfile 与新 Mac 安装流程。
- `P2` 正式 CLI、Replay 入口、配置入口、日志和可观测性。
- `P2` REAL_TIME 可测试化、CHECKPOINT 状态推进改进、Decision Diff 完整流程。
- `P2` DQS 边界和 capability 实际字段校验。
- `P2` CI、coverage、type-check、lint。
- `P3` README 使用指南、配置说明、架构图和故障排查。
- `P3` ReasonCode/Flag/DQS reason 的端到端强类型化。

这些问题不应先于 P0/P1 修复，也不应借机扩展 M6–M11 或新增指标。

## 12. 建议修复顺序

1. 先写失败测试固定 8 条硬规则，尤其是 Pre20、A⇔ARMED、缺失权重重归一化、跨重启 HardCancel、全组件 as-of。
2. 封闭 Replay 数据接口：用统一 `AsOfMarketView`/等价只读边界向 Feature、Peer、Expectation 提供数据，禁止外部全日数组直传；这属于工程约束修复，不改变策略理念。
3. 在 Compiler 增加结构性时间 Gate，修复 Score Gate 的 Grade 降级逻辑，并持久化 HardCancel/HardInvalid。
4. 修复缺失特征重新归一化及 DQS DEGRADED 规则，补齐所有 Permission/Validation/Rank/Position Gate 测试。
5. 按已锁定定义修正 EG、RecoveryRatio、HealthyDisagreement，不新增任何技术指标。
6. 建立 Night Expectation point-in-time 数据集生成、样本 provenance、最小样本逐级放宽和无未来数据测试。
7. 完成正式端到端 Replay 测试矩阵，再处理 packaging、CLI、日志和 CI。
8. 最后用完整交易日捕获验证 Eastmoney 字段；在验证通过前仍不得接生产执行。

只有 1–6 的 P0/P1 全部关闭、端到端回放无越权且审计测试通过后，才建议评估是否进入 Milestone 6。
