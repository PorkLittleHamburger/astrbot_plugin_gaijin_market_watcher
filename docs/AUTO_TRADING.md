# Gaijin Market 自动交易

> 版本 `2.20.0`　|　首版仅支持 `COMMODITY` 普通商品

## 安全边界

- 聊天中的交易指令只接受与 插件控制台账户页配置的拥有者 UID 匹配的 `FriendMessage` 私聊。
- 未配置拥有者 UID 时自动交易直接禁用，不回退 AstrBot 管理员判定。
- WebUI 交易页由 AstrBot Dashboard 登录保护，所有 Dashboard 管理员均可操作。
- 新规则默认模拟运行；切换实盘必须二次确认规则 ID。
- 模拟规则可执行隔离的“完整模拟演练”：只读取一次真实盘口，后续成交由模拟深度补足，不修改规则持仓或累计收益。
- 实盘要求总开关开启，且总预算、每日限额、单笔上限、最大数量均大于 0。
- 交易事件只发送到拥有者私聊；没有私聊会话时只写事件日志。

## 固定费率

手续费按产品要求固定为 **15%**，不读取站点 `marketFee`。

```text
税后到手 = floor_to_0.01(税前价格 × 0.85)
税前价格 = 能令税后到手 >= 目标值的最低 0.01 GJN 价格
```

## 执行规则

1. 最低卖价达到买入限价后读取完整盘口。
2. 只统计买入限价以内的卖单深度；允许部分成交。
3. 买单提交后撤销新产生的未成交余量，并通过库存前后差异确认实际持有数量。
4. 目标税后价以上的求购单覆盖多少就立即卖多少。
5. 未卖出的部分按达到税后目标所需的税前价发布卖单。
6. `once` 在清仓后停用；`loop` 清仓后重新等待买点。

## 订单与资产对账

- 下单使用 `transactid + reqstamp`。
- 网络结果不确定时先读取开放订单与库存，不盲目重复下单。
- 买入前如发现同物品存在未知开放订单，规则进入错误状态而不是并行下单。
- 通过 `GetContexts` / `GetContextContents` 获取 `contextid/assetid`。
- 同名资产若在买入前已存在，会记录基线数量，只跟踪本次新增的差额。
- 手工撤销自动卖单时，通过库存返回判断“撤销”而不是“成交”。

## 价格与状态

- 所有价格以 Gaijin 原始刻度整数保存，`1 GJN = 10000`。
- 用户输入保留两位小数。
- 买入成本、当前持仓、挂单数量、累计税后收益和完成轮数持久化。
- 插件重启后恢复未完成状态；错误规则不会自动继续下单。

## 数据文件

位于 `data/plugin_data/astrbot_plugin_gaijin_market_autotrader/`：

| 文件 | 内容 |
| --- | --- |
| `trade_rules.json` | 规则定义 |
| `trade_settings.json` | 总开关与风险额度 |
| `trade_runtime.json` | 状态机、持仓、资产分配、自动订单 |
| `trade_ledger.json` | 上海时区每日买入额 |
| `logs/trade-YYYY-MM-DD.jsonl` | 按上海日期追加的交易事件，每条一行，保留 30 天 |

旧版 `trade_events.json` 首次升级时会迁移到按日 JSONL，并保留原文件作为备份。

## WebUI API

- `GET  trade/overview`
- `POST trade/settings`
- `POST trade/rules/save`
- `POST trade/rules/action`
- `POST trade/rules/drill`
- `POST trade/stop-all`
- `GET  trade/logs?after=<event_id>&limit=<N>`
