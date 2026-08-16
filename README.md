# A股超短决策系统 V0.1

本仓库实现已锁定策略框架的工程底座，当前范围为 Milestone 1–5：Domain、Canonical Data Layer、Replay、Auction Feature Engine、Night Expectation、1进2以及 09:25 Decision Compiler。

## 不变量

- Gate 顺序：Permission → Validation → Authenticity → Score → Rank → Execution。
- 09:20 前数据只用于侦察；真实性由 09:20 后序列判断。
- A 等级只生成 `ARMED`，不会直接生成买入动作。
- Night Pool 外候选最高为 `C_AUCTION_EMERGENT`。
- `HARD_CANCELLED` 以交易日和股票为键，当日不可恢复。
- 缺失行情字段使用 `None`（N/A），不得用 `0` 代填。
- 数值阈值从 `config/thresholds/*.yaml` 读取，并带 `STRUCTURAL/SEED/CALIBRATED/RETIRED` 状态。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 真实源探针

```bash
python3 -m app.probe_data_source 600000.SH
```

`EastmoneySnapshotAdapter` 仅用于字段可得性探测。目前它是未文档化的公开快照接口，尚未通过完整交易日 09:15–09:25 语义验证，不得作为生产执行源。正式接入应替换为有许可与 SLA 的交易所 Level-1/Level-2 或券商行情源；策略层无需修改。
