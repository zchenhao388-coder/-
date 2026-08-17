# A股超短决策系统 V0.1

本仓库实现已锁定策略框架的 Milestone 1–10，并完成 Milestone 11 的供应商无关生产接入边界、数据源验收门、监控、熔断和日内编排：Domain、Canonical Data Layer、point-in-time Replay、三套 Auction Engine、09:25 Decision Compiler、09:30–09:35 Open Execution、Outcome/Persistence、可验证 Replay、Walk-forward Backtest 与 Calibration。

当前仍没有已配置并通过验收的持牌实时行情供应商。因此系统可用于研究、回放、校准框架和 Shadow 编排验证，但不是可直接实盘运行的交易客户端。

## 核心不变量

- Gate 顺序：Permission → Validation → Authenticity → Score → Rank → Execution。
- 09:15–09:20 为 `SCOUTING`，真实性只能是 `UNKNOWN`。
- 09:20–09:24:30 为 `VALIDATING`，09:24:30–09:25 为 `FINALIZING`；09:25 前不能生成最终 A 或 `ARMED`。
- `CandidateGrade` 为 A1/A2，当且仅当 `ExecutionState = ARMED`。A 不是 BUY。
- Night Pool 外候选最高为 `C_AUCTION_EMERGENT`。
- `DQS DEGRADED` 最高 B；`DQS BROKEN` 禁止执行。
- `HARD_CANCELLED` 按交易日、股票和 NightPlan/策略上下文持久化，当日不可恢复。
- 缺失字段使用 `None`；缺失评分组件从 AQS 中移除，剩余权重重新归一化。
- 核心引擎只通过 `AsOfMarketView` 读取 point-in-time 数据。
- 数值阈值来自 `config/thresholds/*.yaml`，并带 `STRUCTURAL/SEED/CALIBRATED/RETIRED` 状态。

## 新 Mac 从零运行

推荐 Python 3.11；最低支持版本是 Python 3.9。

```bash
git clone https://github.com/zchenhao388-coder/-.git a-share-short-trade-system
cd a-share-short-trade-system

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

如果 Mac 尚未安装 Python 3.11，可先通过 python.org 安装包或 Homebrew 安装，然后重新执行以上步骤。

## 运行全部测试

```bash
python -m unittest discover -s tests -v
```

关键测试入口：

- `tests/test_core_integrity_contract.py`：P0/P1 底层不变量。
- `tests/test_compiler_gates.py`：逐 Gate 越权防护。
- `tests/test_point_in_time_integrity.py`：未来数据与 observation provenance。
- `tests/test_e2e_replay_matrix.py`：四个 checkpoint 和 12 个正式 E2E 案例。
- `tests/test_high_board.py`：Milestone 6 高标接力规则与核心不变量。
- `tests/test_weak_to_strong.py`：Milestone 7 弱转强规则与核心不变量。
- `tests/test_cross_setup_open_execution.py`：Milestone 8 横向排序与开盘状态机。
- `tests/test_outcome_persistence_replay.py`：Milestone 9 Outcome、分层存储和可验证 Replay。
- `tests/test_backtest_calibration.py`：Milestone 10 Walk-forward 与阈值生命周期。
- `tests/test_production_monitoring_orchestration.py`：Milestone 11 数据源验收、监控、熔断和编排。
- `tests/test_mootdx_adapter.py`：Mootdx Primary Probe、原始落盘和 fail-closed 边界。
- `tests/test_tencent_source_validation.py`：腾讯 Secondary Probe 与结构化跨源 DQS。

## 主要配置

- `config/thresholds/one_to_two.yaml`：1进2、真实性与 AQS Seed 参数。
- `config/thresholds/execution.yaml`：DQS 与 Compiler Gate 参数。
- `config/thresholds/expectation.yaml`：分层 benchmark shrinkage 与最小样本参数。
- `config/thresholds/high_board.yaml`：高标接力 Seed 参数。
- `config/thresholds/weak_to_strong.yaml`：弱转强 Seed 参数。
- `config/thresholds/outcome.yaml`：Outcome 标签参数。
- `config/thresholds/calibration.yaml`：滚动校准与漂移参数。
- `config/thresholds/monitoring.yaml`：行情健康和熔断恢复参数。
- `config/thresholds/source_validation.yaml`：跨源价格、涨幅及时点偏差 Seed 参数。
- `config/data_sources.yaml`：Adapter 启用状态和允许用途。

结构性交易时间由 `auction/phase.py` 固定，不属于可校准 Seed 阈值。

## Replay 与决策 Pipeline

正式编排入口是 `app.pipeline.AuctionDecisionPipeline`：

```text
NightPlan
→ ReplayEngine / VirtualClock
→ AsOfMarketView
→ AuctionFeatureEngine
→ OneToTwoEngine
→ DataQualityService
→ DecisionCompiler
```

Replay 和 Open Execution 的策略层入口只暴露 `AsOfMarketView`。Feature、Peer、Expectation、Setup 和开盘特征引擎不接受任意全日数组。

`DecisionCompiler` 要求显式提供持久化 `SQLiteHardCancelStore`。运行时数据库路径应放在本地运行目录或 `storage/raw/` 之外的受控状态目录，不要提交 Git；`.gitignore` 已忽略 SQLite/DB 文件。

## Mootdx + 腾讯 Shadow Probe

Mootdx 是可选依赖；只有运行现场探针时需要安装：

```bash
python -m pip install -e '.[data-sources]'
python -m app.probe_sources 000001.SZ 600000.SH --samples 10 --interval-seconds 1 --transactions
```

该入口会把 source-native payload、canonical probe tick 和跨源验证结果以追加式 JSONL 保存到本地 `storage/`。运行数据由 `.gitignore` 排除，不应提交 Git。

两个 Adapter 当前都只允许：

- `FIELD_PROBE`
- `RAW_CAPTURE`
- `SHADOW_RESEARCH`

`MootdxRealtimeAdapter` 是 Primary Probe，`TencentRealtimeAdapter` 是 Secondary Probe。两者的 provider timestamp 都不会映射成 canonical `exchange_ts`，并被 `ExecutionDataSourceGuard` 结构性禁止执行。跨源明显冲突产生结构化 `SOURCE_DISAGREEMENT` 或 `SOURCE_TIME_SKEW`，并进入 DQS DEGRADED/BROKEN。

完整字段和实测结果见 `docs/DATA_SOURCE_INTEGRATION_REPORT.md`。

## Eastmoney 实验探针

```bash
python -m app.probe_data_source 600000.SH
```

`EastmoneySnapshotAdapter` 仅允许：

- `FIELD_PROBE`
- `RAW_CAPTURE`
- `SHADOW_RESEARCH`

它使用的 `f86` 只保存为 `provider_ts`，不会映射到 canonical `exchange_ts`。当前字段和时间语义尚未通过完整交易日 09:15–09:25 验证，`execution_enabled=false`，不得进入可执行 A 数据链。

## 当前边界

- 没有正式交易 CLI；`ApplicationOrchestrator` 是可注入 handler 的应用编排边界。
- 没有已配置、持牌且通过字段语义验收的生产行情源；仓库中的 production adapter 是供应商无关边界和禁行门，不是已上线连接。
- Mootdx、腾讯和 Eastmoney 都不是 execution source。
- M10 已实现校准机制，但仓库内策略阈值仍以 `SEED` 为主，不能在缺少真实样本外证据时宣称为 `CALIBRATED`。
- 默认 `shadow_mode=true`、`auto_order=false`；M11 结构性禁止自动下单。
- 本仓库不会发送委托，也不会把 A 直接解释为买入。

完整开发与验收结论见 `docs/M6_M11_DEVELOPMENT_ACCEPTANCE_REPORT.md`。
