# DATA_SOURCE_INTEGRATION_REPORT

生成日期：2026-08-17  
范围：Mootdx Primary Probe、腾讯财经 Secondary Probe、跨源 DQS 和长期原始落盘。未新增策略、未修改 M1–M11 的交易定义。

## 1. Executive Summary

本轮完成了两个免费行情源的第一阶段工程接入：

- Primary：`MootdxRealtimeAdapter`
- Secondary：`TencentRealtimeAdapter`
- 允许用途：`FIELD_PROBE / RAW_CAPTURE / SHADOW_RESEARCH`
- 禁止用途：`EXECUTION`

两者都接入既有 `RealtimeAdapter`、`AsOfMarketView`、`ExecutionDataSourceGuard`、DQS、`FeedHealthMonitor` 和追加式 JSONL 存储。`CrossSourceValidator` 输出结构化 `SOURCE_DISAGREEMENT`、`SOURCE_TIME_SKEW` 或 `SOURCE_CROSSCHECK_UNAVAILABLE`，并可把 DQS 降为 DEGRADED 或 BROKEN。

结论：工程接入完成，但生产验收未完成。Mootdx 和腾讯都没有被标记为 production/execution verified，当前仍被 M11 production guard 结构性阻断。

参考实现仅用于核对调用思路和字段位置，没有把 a-stock-data 加为 runtime dependency：

- Mootdx 官方仓库：https://github.com/mootdx/mootdx
- a-stock-data 参考：https://github.com/simonlin1212/a-stock-data

未引入 AKShare。

## 2. 实际接入接口

### 2.1 MootdxRealtimeAdapter

文件：`adapters/mootdx.py`

实现接口：

- `connect()`：延迟导入 `mootdx.quotes.Quotes` 并连接标准市场服务器；测试可注入 client。
- `subscribe()` / `unsubscribe()`：维护 canonical ticker 订阅。
- `poll_once()` / `stream()`：调用 `client.quotes(symbol=[...])`，生成 probe `AuctionTick`。
- `get_latest()`：返回最近一次映射结果。
- `capture_transactions()`：调用 `client.transaction()`，只保存 source-native 逐笔载荷。
- `load()`：单次当前快照读取，按可靠的本地 `receive_ts` 过滤。
- `health()` / `close()`：连接、订阅、最后接收时间和异常状态。

Mootdx 为可选依赖：`mootdx>=0.11.7,<0.12`。核心研究/Replay 测试不依赖 Mootdx 安装。

### 2.2 TencentRealtimeAdapter

文件：`adapters/tencent.py`

实现接口：

- `connect()`：建立无状态 HTTP Probe 生命周期，不预先请求网络。
- `subscribe()` / `unsubscribe()`：将 `.SH/.SZ/.BJ` 映射为腾讯 symbol。
- `poll_once()` / `stream()`：访问 `https://qt.gtimg.cn/q=...`，按 GBK 和 `~` 字段解析。
- `get_latest()` / `load()` / `health()` / `close()`：与既有 `RealtimeAdapter` 合同一致。
- `raw_quote_payloads()`：保留原始响应行、完整字段数组、映射版本和 provenance。

腾讯只作为 Secondary Probe，不提供逐笔接口，也不替代 Mootdx Primary。

### 2.3 Probe Orchestrator

文件：`app/probe_sources.py`

示例：

```bash
python -m pip install -e '.[data-sources]'
python -m app.probe_sources 000001.SZ 600000.SH --samples 10 --interval-seconds 1 --transactions
```

每轮流程：

```text
Mootdx raw quote + optional transaction
Tencent raw quote
→ append-only source payload
→ canonical probe AuctionTick
→ AsOfMarketView.get_probe_ticks
→ CrossSourceValidator
→ derived SOURCE_VALIDATION record
```

该入口不调用 Compiler，不产生 A/ARMED，不发送委托。

## 3. 字段映射与验收状态

状态含义：

- `CAPTURED_SHAPE`：真实响应中已经观察到字段，但不等于语义验收。
- `PROVISIONAL`：已做 Shadow 映射，仍需完整交易日证据。
- `UNVERIFIED`：不得作为 execution-grade canonical 字段。
- `N/A`：当前源没有可接受映射。

### 3.1 Mootdx

| Canonical 字段 | Mootdx 字段 | 当前状态 | 说明 |
|---|---|---|---|
| ticker | `code` + 请求上下文/market | CAPTURED_SHAPE | 主板样本已解析；北交所仍需单独实测 |
| receive_ts | 本地带时区接收时钟 | CAPTURED_SHAPE | 本地观测时间，不是交易所时间 |
| provider_ts | `servertime` / transaction `time` | CAPTURED_SHAPE | quote 可到毫秒；transaction 实测为分钟粒度 |
| exchange_ts | 无 | N/A | 明确保持 `None` |
| virtual_price | `price` | PROVISIONAL | 仅确认有值；集合竞价虚拟开盘价语义未验收 |
| gap_pct | `price / last_close - 1` | PROVISIONAL | 派生口径明确，输入语义仍未验收 |
| matched_volume | `vol * 100` | PROVISIONAL | lots→shares 与金额关系相符；集合竞价“匹配量”语义未验收 |
| matched_amount | `amount` | PROVISIONAL | 单位看似为元；集合竞价“匹配额”语义未验收 |
| bid/ask | `bid1` / `ask1` | PROVISIONAL | 捕获到五档价格 |
| orderbook | `bid/ask 1–5` + volume | PROVISIONAL | volume 暂按 lots×100；需供应商语义证据 |
| unmatched_side/volume | 无直接字段 | N/A | 保持 `None`，不从五档静默推断 |
| transactions | `transaction()` | CAPTURED_SHAPE | 保留 `time/price/vol/num/buyorsell` 原始字段，不转为 execution tick |

### 3.2 腾讯财经

| Canonical 字段 | 腾讯字段 | 当前状态 | 说明 |
|---|---|---|---|
| ticker | 响应变量名和 field 2 | CAPTURED_SHAPE | 沪深样本已解析；北交所需单独实测 |
| receive_ts | 本地带时区 HTTP 接收时钟 | CAPTURED_SHAPE | 本地观测时间 |
| provider_ts | field 30 `YYYYMMDDHHMMSS` | CAPTURED_SHAPE | 只记 provider time |
| exchange_ts | 无已认证字段 | N/A | 明确保持 `None` |
| virtual_price | field 3 | PROVISIONAL | 集合竞价语义未验收 |
| gap_pct | field 32；缺失时由 3/4 派生 | PROVISIONAL | 实测与 Mootdx 派生值接近 |
| matched_volume | field 6 × 100 | PROVISIONAL | 正常交易阶段与金额关系相符；竞价匹配语义未验收 |
| matched_amount | field 35 第三段 | PROVISIONAL | 暂按元；竞价语义未验收 |
| bid/ask | fields 9/19 | PROVISIONAL | 五档字段形状已捕获 |
| orderbook | fields 9–28 | PROVISIONAL | volume 暂按 lots×100 |
| unmatched_side/volume | 无直接已验收字段 | N/A | 保持 `None` |
| transactions | 无 | N/A | 腾讯不承担逐笔主源职责 |

所有缺失字段使用 `None`，没有把“没有数据”填成 0。

## 4. Timestamp 语义

三个时间严格分离：

- `receive_ts`：本机实际收到/完成解析数据的带时区时刻，可用于 AsOf 接收边界。
- `provider_ts`：免费源返回的服务器/行情字符串，只用于 freshness、乱序和跨源时差监控。
- `exchange_ts`：只有存在交易所来源与合同证据时才能设置。当前两个 Adapter 永远输出 `None`。

`AsOfMarketView.get_ticks()` 继续要求 exchange_ts 和 receive_ts 都不晚于 AsOf，因此两个免费 Probe 的 tick 不会进入正式策略特征链。新增的 `get_probe_ticks()` 只按 receive boundary 暴露 Shadow/字段验证数据，CrossSourceValidator 使用该入口，仍然不能读取未来接收的数据。

## 5. 2026-08-17 实测结果

### 5.1 竞价末段 Mootdx 单次 Probe

请求在 09:25:19 发起，09:25:25 完成服务器选择和响应：

| 股票 | provider_ts | price | vol | amount | bid1 / bid_vol1 | ask1 / ask_vol1 |
|---|---:|---:|---:|---:|---:|---:|
| 000001.SZ | 09:24:53.286 | 11.20 | 21,048 lots | 23,573,760 | 11.19 / 222 lots | 11.20 / 5,882 lots |
| 600000.SH | 09:24:57.552 | 9.09 | 915 lots | 831,735 | 9.09 / 8 lots | 9.10 / 595 lots |

这只能证明在最终竞价附近可以取得价格、五档、vol、amount 和 provider time 的字段形状。由于接收发生在 09:25 后、首次连接包含选服务器耗时，并且没有同一时点腾讯样本，不能据此宣称 09:24:30 或 09:25 的 exchange-accurate 快照已验证。

### 5.2 腾讯单次 Probe

09:30:47 接收的响应中，provider_ts 为 09:30:45，两个样本均返回价格、昨收、五档、总量、总额和 88 段左右的原始字段。该样本发生在连续竞价阶段，不能替代 09:15–09:25 验收。

### 5.3 双源持续 Probe 与跨源结果

本地已经实际追加保存：

- Mootdx quote：两个 ticker 多轮样本；
- Tencent quote：两个 ticker 多轮样本；
- Mootdx transaction：首次捕获 000001.SZ 230 行、600000.SH 233 行，时间范围包含 `09:15` 至捕获时；
- canonical probe tick；
- SOURCE_VALIDATION V1/V2 派生记录。

真实 Probe 发现：

- 09:38 两源价格基本一致。
- 第一轮 Mootdx provider time 曾比腾讯落后约 29 秒，而 receive time 接近。V1 只比较 receive time，错误给出 AGREED。
- 随即升级为 SOURCE_VALIDATION V2：同时比较 receive-time skew 和 provider-time skew。旧 V1 记录按追加式原则保留，不删除、不覆盖。
- 09:39 V2：000001.SZ provider skew 约 9.654 秒，600000.SH 约 8.394 秒；价格一致但输出 `SOURCE_TIME_SKEW`，DQS DEGRADED。
- 09:40 V2：000001.SZ provider skew 约 12.066 秒；输出 CRITICAL，DQS BROKEN。

逐笔样本的 `time` 实测为分钟粒度，首行可能出现 `buyorsell=8`、`vol=0` 等特殊值，说明开源说明中的 0/1/2 买卖方向不能直接套用到所有竞价/边界记录；当前完整保留 raw，不做静默修正。

### 5.4 尚未完成的竞价验证

本轮没有获得从 09:15 开始的双源连续采样，因此以下仍未验证：

- 09:15–09:20 侦察阶段的刷新频率和虚拟价格语义；
- 09:20 撤单开始前后 bid/ask、vol/amount 如何变化；
- 09:24:30–09:25 的连续最后阶段及 09:25 定格行为；
- 两源重复、乱序、回补、修订和断流概率；
- Mootdx `vol/amount` 在竞价阶段是否严格等于 matched volume/amount；
- 五档量与未匹配委托量之间是否存在可靠映射；
- 免费源 timestamp 是否由交易所、通达信服务器或缓存节点生成。

因此没有任何字段被写入 `FieldAcceptanceStatus.VERIFIED`。

## 6. SOURCE_DISAGREEMENT 规则

配置文件：`config/thresholds/source_validation.yaml`

所有阈值状态均为 `SEED`，未硬编码到校验代码：

- 价格相对差 ≥0.2%：DEGRADED；≥1.0%：BROKEN。
- gap 绝对差 ≥0.3 个百分点：DEGRADED；≥1.0 个百分点：BROKEN。
- receive-time skew >3 秒：DEGRADED；>10 秒：BROKEN。
- provider-time skew >3 秒：DEGRADED；>10 秒：BROKEN。
- Secondary 不可用、字段缺失或未来 Secondary 被 AsOf 过滤：`SOURCE_CROSSCHECK_UNAVAILABLE`，DQS DEGRADED。

输出结构：

- `SourceFieldComparison`：字段、两源值、绝对差、相对差和字段级状态。
- `SourceValidationReport`：两源身份、AsOf、总体状态、DQS 状态、时点偏差、reason codes。
- 总体状态：`AGREED / DISAGREEMENT / CRITICAL / INSUFFICIENT`。
- `execution_eligible` 对当前 Probe 永远为 `false`。

集成方式：

- `DataQualityService.evaluate(..., source_validation=report)`：DEGRADED/BROKEN 只能降低 DQS，绝不能升级原始 DQS。
- `FeedHealthMonitor.evaluate(..., source_validation=report)`：冲突原因进入健康报告和熔断链。
- CRITICAL/BROKEN 继续通过既有 Compiler Gate 触发禁止执行/HardCancel；没有新增 Gate 旁路。

## 7. 落盘格式

默认根目录：`storage/`，所有记录均为 append-only JSONL。

```text
storage/
├── raw/
│   ├── mootdx_realtime_probe/<trade_date>/quote/<ticker>.jsonl
│   ├── mootdx_realtime_probe/<trade_date>/transaction/<ticker>.jsonl
│   ├── tencent_realtime_probe/<trade_date>/quote/<ticker>.jsonl
│   └── canonical/<source>/<trade_date>/<ticker>.jsonl
└── derived/<trade_date>/<ticker>.jsonl
```

source-native envelope 保存：

- source / event_type / ticker；
- receive_ts / provider_ts / exchange_ts；
- 完整 raw payload；
- 字段 provenance。

canonical tick 仍保留 `raw`、`source`、映射版本和验收标志。运行数据由 `.gitignore` 排除，不提交仓库。

## 8. 测试结果

基线：147 Passed，0 Failed，0 Skipped。

Mootdx 阶段：

- 专项：7 Passed，0 Failed，0 Skipped。
- 首次全量发现 1 个兼容性回归：曾尝试把直接传入 DQS 的无 exchange_ts tick 从既有 DEGRADED 改为 BROKEN，导致原 Eastmoney 合同测试失败；该核心规则修改已撤回。
- 修复后全量：154 Passed，0 Failed，0 Skipped。

腾讯与跨源阶段：

- 专项最终：11 Passed，0 Failed，0 Skipped。
- 覆盖 Provider-time skew 不能被相近 receive time 掩盖。
- 最终全量：165 Passed，0 Failed，0 Skipped。
- `git diff --check`：通过。

关键覆盖：

- 两个免费源均被 Production Guard 拒绝；
- provider timestamp 不映射为 exchange timestamp；
- 缺失字段保持 None；
- raw/canonical/derived 追加式落盘；
- 乱序和重复不被 Adapter 静默丢弃；
- Future Secondary 不能穿过 AsOf boundary；
- PRICE/GAP/SKEW 冲突结构化输出并降低 DQS；
- 原有 A⇔ARMED、09:25、minimum-sample、DQS、HardCancel 和 NightPlan identity regression 全部继续运行。

## 9. M11 Production Guard 状态

当前仍然 BLOCKED。

`ExecutionDataSourceGuard` 对 `mootdx_realtime_probe` 和 `tencent_realtime_probe` 名称做结构性禁行；即使误改配置布尔值，也会产生 `EXPERIMENTAL_SOURCE_FORBIDDEN`。同时两者 capability 均为：

- `execution_enabled=false`
- `license_verified=false`
- `sla_verified=false`
- `auction_semantics_verified=false`
- `exchange_ts=false`

Mootdx 官方仓库还明确声明项目只用于学习交流、不得用于商业目的。这与生产实盘用途存在直接许可冲突，不能被技术测试替代。

## 10. 下一步缺口

按优先级：

1. 在下一个真实交易日 09:14:50 前启动双源连续采集，至少覆盖 09:15–09:25:10。
2. 增加多个沪/深/北交所、主板/创业板/科创板、涨停附近和低流动性样本。
3. 对 09:20 撤单和 09:24:30–09:25 最后阶段逐秒检查 price、五档、vol、amount、重复、乱序和刷新间隔。
4. 将采样与交易所官方/持牌源逐字段对照，确定 virtual/matched/unmatched 的真实语义和单位。
5. 获得可用于实盘的供应商许可、SLA、字段字典、exchange-origin timestamp 证据和多交易日验收记录。
6. 只有满足现有 `DataSourceAcceptanceReport` 全部条件后，才能新建持牌生产 Adapter；不得把当前两个 Probe 改名或改布尔值冒充 production。

本轮到此停止，不扩展策略、不修改 Compiler Gate。
