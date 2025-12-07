# Legacy taker ladder check

结论：当前可执行路径已完全切换为 maker 流程，早期的五档让利 FOK 卖出脚本（`Volatility_sell.py` / `execute_auto_sell`）已经不在仓库中，也没有被引用，卖出执行由带地板价的 maker 路径接管。

## 证据

- 仓库根目录已不存在 `Volatility_sell.py`，全局搜索也没有 `execute_auto_sell` 的实现或调用，`rg "execute_auto"` 仅命中历史蓝图文档。当前主流程引用的卖出工具是 `maker_sell_follow_ask_with_floor_wait`（GTC 跟随卖一，强制不跌破 floor）。【F:Volatility_arbitrage_run.py†L2526-L2556】【F:maker_execution.py†L844-L1493】
- `maker_sell_follow_ask_with_floor_wait` 内部在多处检查/约束 `ask` 与 `floor_X` 的关系：卖一跌破地板时撤单等待、重新挂单时取 `max(ask, floor_X)`。这与“5 次让利直到成本价”相反，确保挂单不会低于利润地板。【F:maker_execution.py†L911-L1508】
- 旧版 `_place_sell_fok` 虽仍留在 `Volatility_arbitrage_run.py` 作为辅助函数，但在全局无调用，且也只是单价 FOK 下单，不含五档让利梯度，无法触发“向下多档吃单”的遗留行为。【F:Volatility_arbitrage_run.py†L1523-L1535】

因此，“平买平卖”问题不再可能由旧的五档让利脚本直接触发，排查应集中在 maker 路径的价差/地板配置与成交延迟上。
