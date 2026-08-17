# Probe Stability Fix Report

## Root cause

2026-08-17 13:00–15:00 Probe 暴露了三个采集层问题：Mootdx `quotes()` 没有调用级 timeout；Mootdx 在网络调用前生成 `receive_ts`；Mootdx 与腾讯位于同一串行循环，Primary 阻塞会同步停止 Secondary。旧入口也没有硬结束时间，只能依靠外部中断。

## 修改

- `MootdxRealtimeAdapter` 对 quote/transaction 调用增加明确 timeout。调用在 daemon worker 内执行；超时后 `poll_once()` 有界返回，且同一未完成调用不会产生重叠线程。
- Mootdx `receive_ts` 改为 provider 响应完成后立即生成，保持 timezone-aware；`exchange_ts` 与其他字段语义未变。
- 新增双 source 隔离运行时：Mootdx 与腾讯分别连接、轮询、落盘和关闭；一个 source stall 不阻塞另一个。
- 新增每源追加式 `SOURCE_STARTED`、`HEARTBEAT`、`STALL`、`WATCHDOG_STALL`、`GAP`、`POLL_ERROR`、`SOURCE_STOPPED` 等结构化事件。异常不会清理或覆盖既有 raw 数据。
- CLI 新增 `--end-time` / `--duration-seconds`。达到 deadline 后设置停止信号、在有界 grace period 内正常 join；不再依赖人工 `SIGINT`。
- 两个免费源仍为 FIELD_PROBE / RAW_CAPTURE / SHADOW，`exchange_ts=None`、`execution_enabled=false`，Production Guard 未修改。

## 测试

- 数据源专项：21/21 Passed。
- 全量 regression：168/168 Passed，0 Failed，0 Skipped（原 165 项全部保留，新增 3 项）。
- 对抗测试模拟 Mootdx 长阻塞，验证：Mootdx timeout 有界；腾讯继续产生并落盘多轮数据；watchdog/stall/gap/heartbeat 结构化记录存在；两个 source worker 到 deadline 后均结束；无 `FORCED_STOP`；全部 Probe 数据保持不可执行。
- Core Integrity：AsOfMarketView、A ⇔ ARMED、09:25 前禁 A/ARMED、minimum-sample、DQS、HardCancel、NightPlan identity、Eastmoney 禁执行及 Compiler Gates 全部历史测试通过。

## 明早采样结论

具备重新进行 09:15–09:25 双源 Shadow Probe 的工程条件。建议在 09:15 前启动并使用硬结束时间，例如：

```bash
python -m app.probe_sources 000001.SZ 600000.SH --end-time 09:25:05 --interval-seconds 1 --mootdx-timeout-seconds 2 --tencent-timeout-seconds 2
```

这只表示采集稳定性修复具备复测条件，不表示字段语义、exchange timestamp、SLA 或 production execution 已通过验收。明早仍需以实际 heartbeat、stall、gap 和跨源记录判断效果。
