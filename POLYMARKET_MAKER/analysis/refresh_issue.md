# 卖出流程刷新后仍重复挂单的原因分析

## 现象复盘

日志中多次出现 `[MAKER][SELL] 挂单状态 -> price=... sold=0.00 remaining=50.00 status=MATCHED`，即便状态已经是 `MATCHED`/`FILLED`，累计成交仍然保持 0，导致循环继续撤单重挂，后续还触发“可用仓位不足，调整卖出数量后重试”逐步缩减挂单数量。

## 代码路径

* 卖出流程入口：`maker_sell_follow_ask_with_floor_wait`。逻辑在每次循环内根据持仓刷新、卖一价格、挂单状态决定是否重新挂单。【F:maker_execution.py†L768-L1305】
* 成交累计更新：`_update_fill_totals` 仅依赖状态里 `filledAmount/avgPrice`，如果状态是终态且 `expected_full_size` 提供会将成交量强制提到应有值。【F:maker_execution.py†L316-L343】
* 持仓刷新与缩量：当下单返回余额不足会反复刷新 `position_fetcher`，在超过重试阈值后按 `shrink_tick` 逐步减少 `goal_size`，形成“持续降量挂单”。【F:maker_execution.py†L1042-L1176】

## 可能原因

1. **状态不含有效成交量**：`_update_fill_totals` 只看 `status_payload['filledAmount']`。若 CLOB 返回的终态只有 `filledAmountQuote`（报价货币）或仅有 `fills` 子字段，`filledAmount` 会保持 0，且 `record_size` 为空时不会进入“终态强制拉满”分支，导致 `filled_total` 永远不增长，循环一直认为未成交。【F:maker_execution.py†L316-L343】【F:maker_execution.py†L1236-L1260】
2. **刷新持仓未同步在途单数量**：周期性 `position_fetcher` 只用最新持仓与已统计成交 (`filled_total`) 计算 `goal_size`，但不扣除当前在途挂单的数量。因此当实际仓位被锁定在旧挂单上时，刷新仍得到同样的持仓值，逻辑会误以为“可用仓位不足”，触发缩量重试，出现不断降量的行为。【F:maker_execution.py†L871-L923】【F:maker_execution.py†L1042-L1176】

## 改进思路

* **更健壮的成交解析**：在 `_update_fill_totals` 中落地 `execution._normalize_status` 的多字段兜底逻辑（如 `filledAmountQuote` + 价格换算、`fills` 聚合、`size/quantity` 兜底），避免终态成交量缺失时被当作 0。【F:trading/execution.py†L660-L838】
* **终态与成交缺口的二次查询**：当状态是 `FILLED/MATCHED` 但成交量仍为 0 或小于下单量时，主动再拉一次原始 `get_order_status` 或 `fills`，若仍获取不到成交量则中止循环并记录异常，以免无限重挂。【F:maker_execution.py†L316-L343】【F:maker_execution.py†L1236-L1260】
* **在途挂单占用量纳入持仓刷新**：刷新持仓时，将当前所有未完成挂单的目标数量加入“已占用”计算，避免将锁定仓位误判为可用，从而减少“余额不足”并触发缩量的概率。【F:maker_execution.py†L871-L923】【F:maker_execution.py†L1042-L1176】
* **缩量前的主动撤单与延迟**：在触发缩量逻辑前先确认撤销所有在途订单并等待结算，再重新查询持仓，确保缩量基于真实可用仓位而不是暂时被挂单占用的数量。【F:maker_execution.py†L1042-L1176】
