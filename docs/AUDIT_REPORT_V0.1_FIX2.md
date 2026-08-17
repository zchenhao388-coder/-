# V0.1 Core Integrity Fix #2 报告

日期：2026-08-16

范围：只修复第二轮审计指定的两个 Core Integrity 问题。未开发 Milestone 6，未扩展策略、指标或功能，未改变 PES/LES、Regime、Phase、CRS、Permission 或 1进2 策略框架。

## 1. Root Cause

### 1.1 Hierarchical Benchmark minimum-sample 旁路

原实现会在所有 hierarchy 层均低于 `minimum_local_sample_size` 时，继续选择最宽层分布并只降低 confidence。该结果没有不可执行状态，仍可进入 Expectation、EG、NormalizedEG、AuctionSurprisePercentile、AQS 和 Compiler。

AuctionSurprisePercentile 原接口只返回 percentile 数值，也没有有效样本数、minimum 或状态，因此相同的 minimum-sample contract 可以从 Surprise 路径被绕过。

### 1.2 HardCancel / NightPlan context identity 不稳定

原 context hash 使用了未经 canonicalization 的 NightPlan 表达，并纳入 `generated_at`。因此，逻辑相同的 NightPlan 只要重建时间、时区字符串表达或序列化顺序不同，就可能生成不同 context id，导致持久化 HardCancel 无法命中。

## 2. 修改文件

生产代码：

- `domain/enums.py`：增加 `BenchmarkStatus`。
- `domain/models.py`：在 `SetupResult` 中保存 benchmark 与 surprise 状态，默认值为 `INSUFFICIENT`，采用 fail-closed。
- `domain/reason_codes.py`：增加结构化原因 `BENCHMARK_MINIMUM_SAMPLE_FAILED`。
- `expectation/benchmark.py`：输出 `effective_sample_size`、`minimum_sample_size` 和 `status`；最宽层 fallback 可继续用于研究计算，但明确标记为 `INSUFFICIENT`。
- `expectation/surprise.py`：返回包含 percentile、有效样本数、minimum 和状态的结构化结果；使用与 benchmark 相同的配置化 minimum contract。
- `auction/features.py`：向后续引擎传递两条样本状态及样本数元数据。
- `auction/one_to_two.py`：benchmark 或 surprise 任一不足时，GradeRecommendation 最高为 B。
- `execution/compiler.py`：在 SetupRequirements 与 AQS/HVS/Percentile 之间增加结构性 `BenchmarkMinimumSample` Gate；失败时最高 B、状态 WAIT。
- `domain/context.py`：实现 NightPlan canonical identity、稳定序列化与反序列化。

测试适配及新增：

- `tests/test_core_integrity_fix2.py`
- `tests/helpers.py`
- `tests/test_e2e_replay_matrix.py`
- `tests/test_one_to_two_compiler.py`
- `tests/test_point_in_time_integrity.py`

本轮没有新增或修改策略阈值；继续使用 `config/thresholds/expectation.yaml` 中配置化的 `minimum_local_sample_size`。

## 3. 最终 Invariant

### 3.1 Minimum-sample

```text
benchmark_effective_sample_size < minimum_sample_size
OR
surprise_effective_sample_size < minimum_sample_size

=> BenchmarkMinimumSample Gate = FAILED
=> recommendation not in {A1, A2}
=> final grade not in {A1, A2}
=> execution_state != ARMED
```

该约束同时存在于 1进2 GradeRecommendation 和 Decision Compiler，避免单层调用或高 AQS/HVS/Percentile 绕过。所有层不足时仍允许保留 broadest distribution 的研究结果，但它没有 execution-grade 资格。

### 3.2 NightPlan identity / HardCancel

Context id 现在是以下 canonical payload 的 SHA-256：

- 固定 schema version；
- `trade_date`；
- 统一为 UTC、固定微秒精度的 `information_available_at`；
- 大写、去重、排序后的 Candidate Pool；
- 按 ticker/setup 排序后的 setup mapping。

`generated_at` 作为非策略身份元数据保留在序列化结果中，但不参与 context identity。Python object identity、运行时内存、随机 UUID、mapping/sequence 顺序和等价时区表达均不影响 identity。

因此：

```text
logically equivalent NightPlan => same context id => sticky HardCancel 命中
materially different NightPlan => different context id => 不错误继承 HardCancel
```

## 4. 新增对抗测试

`tests/test_core_integrity_fix2.py` 共 6 项：

1. 所有 hierarchy 层均少于 minimum，确实 fallback 到 broadest distribution，但状态为 `INSUFFICIENT`。
2. Benchmark 仅 4/5，Surprise 已满足 5/5；极强 ActualAuctionGap、AQS、HVS、真实性、DQS、Rank、Position 均满足时，仍在 minimum gate 被限制为 non-A / non-ARMED。
3. Benchmark 已满足 5/5，Surprise 仅 4/5；即使 percentile=1.0，仍为 non-A / non-ARMED。
4. NightPlan serialize → reload 后 context identity 完全一致；不同生成时间、候选顺序和等价时区表示也一致。
5. 触发 HARD_CANCELLED 后，重建 NightPlan、Pipeline/Compiler 和持久化 Store，仍保持 HARD_CANCELLED。
6. Candidate Pool 实质不同的 NightPlan 生成不同 identity，不错误继承 context。

两条 extreme-gap 测试还显式验证：AQS/HVS 达到 Compiler A Gate、DQS=GOOD、失败原因正是 `BENCHMARK_MINIMUM_SAMPLE_FAILED`，避免测试因其他 Gate 失败而产生假阳性。

## 5. 修改前失败、修改后通过证据

先写对抗测试、尚未修改生产代码时：

```text
python3 -B -m unittest tests.test_core_integrity_fix2 -v

Total:  5
Passed: 1
Failed: 3
Errors: 1
```

复现结果包括：缺少 benchmark status；极强 gap 最终得到 A1；等价 NightPlan identity 不同；重建后原 HardCancel 未命中并恢复为 ARMED。只有“真正不同 plan identity 不同”在原实现中通过。

修复后专项结果：

```text
Total:   6
Passed:  6
Failed:  0
Errors:  0
Skipped: 0
```

最终全量回归：

```text
python3 -B -m unittest discover -s tests -v

Total:   83
Passed:  83
Failed:  0
Errors:  0
Skipped: 0
```

`git diff --check` 通过。追加 Gate 原因断言时曾将现有字段 `reason` 写成 `reason_code`，产生 2 个测试代码错误；修正测试字段名后重新执行全量测试，最终结果如上，生产逻辑没有因此变更。

## 6. 已知 P0 / 影响策略正确性的 P1

- 本轮两个指定问题：已关闭。
- 当前测试和本轮代码审查范围内：未发现仍未关闭的已知 P0，也未发现仍影响底层策略正确性的已知 P1。
- 该结论不代表可实盘：Eastmoney 仍是实验字段探针，可靠交易所时间戳和 09:15–09:25 字段语义尚未完成生产验证，SEED 阈值也尚未校准。
- 本轮不作 Milestone 6 放行决定，且没有进入 Milestone 6。
