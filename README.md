# Gaijin Market 控制台（独立版）

这是独立部署的 Gaijin Market 控制台插件，使用新的插件 ID、独立数据目录和 `/gjma` 指令前缀，
可与原 `astrbot_plugin_gaijin_market_watcher` 同时安装而不会互相覆盖。

插件监控 [Gaijin Market](https://trade.gaijin.net/market/sell) 上物品的**最低售价**、**最高求购价**与**挂单量**，
变化时推送到群聊或私聊，可 @ 到订阅者本人。

## 功能

- **行情监控**：按物品页链接订阅，实时抓取最低售价 / 最高求购价 / 两侧挂单量
- **会话级订阅**：同一用户可在不同群聊或私聊分别维护自己的订阅
- **群聊聚合**：固定间隔刷新群内全部订阅物品，合并成一条快照并分别 @ 订阅者
- **价格折线图**：每次自动行情推送按物品展示最近 N 次最低售价和最高求购
- **账户控制台**：账号密码、拥有者 UID、两步验证码和登录状态都在插件视图中管理
- **双输出模板**：行情推送与全部交易事件分别配置模板；交易模板提供规则、持仓和成交字段
- **自动交易**：监测普通商品盘口，按限价买入，达到税后目标价后卖出；默认模拟，二次确认后实盘
- **一体化控制台**：同一插件视图提供“行情订阅 / 自动交易 / 输出模板 / 账户登录”四个标签页

## 适配平台

| 平台 | @ 订阅者 | 主动推送 |
| --- | --- | --- |
| QQ 官方机器人（`qq_official` / `qq_official_webhook`） | 文本内嵌标记（走 markdown 通道）；**私聊（C2C）不支持该标记，会自动跳过 @** | 支持 |
| 个人微信（`weixin_oc`） | 纯文本 @昵称（该平台不认 At 组件） | 需用户先给机器人发过消息 |

其余平台未做测试

平台差异（@ 的方式、是否要求带文字、主动推送前提）集中写在 `core/platforms.py`，增改支持只需改这一个文件。

## 使用方法

### 1. 安装

需要 AstrBot ≥ 4.29.0、Python ≥ 3.10（自动交易 WebUI 使用 `views/` 与 `AstrBotPluginView` 接口）。

通过 AstrBot WebUI 上传本插件的 ZIP 包安装。源码仓库：
```text
https://github.com/PorkLittleHamburger/astrbot_plugin_gaijin_market_watcher
```
安装、更新、删除等操作需要 AstrBot 管理员权限，请先在管理面板配置管理员。

### 2. 登录 Gaijin 账号

打开插件详情页中的 `Gaijin Market 控制台`，进入“账户登录”标签页，填写账号邮箱、密码和拥有者 UID
（可发送 AstrBot 内置命令 `/sid` 查看）。点击“保存并开始登录”；如果站点要求两步验证码，页面会显示验证码输入框。

账号密码保存在插件数据目录的 `account.json` 中，API 只返回“是否已保存”，不会回显密码；续期令牌仍保存为 `auth.json`。
两步验证码只在提交时使用，不写入配置或日志。聊天登录仍保留为备用入口：

```
/gjma login              发起登录
/gjma login <验证码>     提交两步验证码
```

**自动交易不会回退管理员判定：账户页未配置拥有者 UID 时交易功能直接禁用。**

### 3. 自动交易（仅拥有者私聊）

自动交易首版只支持 `COMMODITY` 普通商品，拍卖物品会拒绝创建规则。手续费固定为 15%。
新规则始终从模拟盘开始，必须先在 WebUI 设置总开关、总预算、每日限额、单笔上限和最大数量。

```
/gjma trade add <物品链接> <买入价> <数量> <税后卖出价> <once|loop>
/gjma trade list
/gjma trade show <规则ID>
/gjma trade arm <规则ID>                 查看实盘风险摘要
/gjma trade arm <规则ID> confirm         60 秒内二次确认切换实盘
/gjma trade pause|resume|disarm <规则ID>
/gjma trade cancel <规则ID>              撤销本插件为该规则创建的订单并暂停
/gjma trade delete <规则ID>
/gjma trade orders
/gjma trade log [1-50]
/gjma trade budget
/gjma trade stop-all confirm             撤销全部自动订单并暂停所有规则
```

买入逻辑：最低售价达到买入限价后，按阈值内卖单深度买入；允许部分成交，并立即撤销未成交余量。
卖出逻辑：目标税后价以上的求购单覆盖多少就卖多少；剩余持仓按可达到目标到手价的挂单价发布卖单。

每条规则提供明确的“切换到模拟盘 / 切换到实盘”按钮；切换实盘仍须输入规则 ID 确认。
模拟规则可点击“完整模拟演练”：插件只读取一次当前市场盘口，随后用隔离的模拟深度完整跑完买入和清仓，
并展示模拟成本、到账和盈亏。演练不会修改规则持仓、收益或预算，也不会调用任何真实下单接口。

插件控制台可由所有已登录 AstrBot Dashboard 管理员操作。
聊天侧仍严格要求账户页配置的拥有者 UID 和 `FriendMessage` 私聊。

### 4. 行情订阅

在控制台“行情订阅”标签页中按 `UID + 会话 UMO` 新增、编辑、启停或删除订阅组，也可以删除组内单件物品。
已识别会话可从下拉列表选择，也可以手工填写 UMO。聊天命令继续可用：

```
/gjma sub <物品页链接>      订阅（只接受物品页链接）
/gjma mysub                先刷新行情，再查看当前会话中我的订阅和全局列表
/gjma unsub <编号|物品链接> 取消订阅
/gjma refresh mode <模式>  always / only_change
/gjma refresh interval <秒> [物品链接|编号]
```

订阅保存在插件数据目录的 `subscriptions.json`，聊天命令和控制台共用同一份数据。

### 5. 输出模板

控制台“输出模板”标签页包含：

- **行情推送模板**：默认值与原 `payload` 模板完全一致，继续支持 `$mention`、图片键和数组逐行规则。
- **交易事件模板**：用于规则创建/更新、设置、买入、卖出、挂单、周期完成、模拟演练和异常等全部交易通知。

字段按钮会插入 `$变量` 到光标位置；“校验并预览”返回解析错误、未知字段和示例渲染结果。
模板保存在插件数据目录的 `output.json`，修改后立即生效。

交易日志按上海日期追加到 `logs/trade-YYYY-MM-DD.jsonl`，默认保留 30 天。
控制台每 3 秒按事件游标读取新增日志，只插入发生变化的部分。

### 6. 查看与排障

```
/gjma status               运行与令牌状态
/gjma price <物品链接>     查询单个物品的实时行情
/gjma help                 查看全部指令
```

### 主要配置项

| 配置 | 说明 |
| --- | --- |
| `push_mode` | 新订阅的默认刷新模式 |
| `push_batch_seconds` | 私聊推送合并窗口（秒），窗口内多件物品合成一条 |
| `group_push_interval` | 群聊全量推送间隔，默认 30 秒 |
| `chart_history_points` | 每张价格图展示的最近成功采样次数，默认 5 |
| `request_timeout` / `request_retries` / `item_interval` | 接口请求参数 |

账号、拥有者 UID、订阅和输出模板已从原生配置表单隐藏，统一在插件控制台管理。
升级时会自动把旧配置迁移到 `account.json`、`subscriptions.json` 和 `output.json`。

### 刷新模式

- `always`：按照该物品设置的刷新间隔抓取并推送，即使行情没有变化也会发送。
- `only_change`：固定每 5 秒检查一次网站，只在价格发生变化时发送。
- 群聊不使用个人 `always` / `only_change` 控制发送；插件每个 `group_push_interval` 主动刷新该群全部物品并发送完整快照。
- `/gjma refresh mode always|only_change`：设置自己全部订阅的刷新模式。
- `/gjma refresh interval <秒>`：设置自己全部订阅的默认刷新间隔。
- `/gjma refresh interval <秒> <物品链接|编号>`：只修改指定物品；编号来自 `/gjma mysub`。

### 价格折线图

- 每次自动私聊推送和群聊快照都会按物品附带价格图。
- 图中同时展示最低售价与最高求购，横轴为本地时间，纵轴为 GJN。
- 每个物品独立一张图；QQ 官方每条消息只支持一张图片，因此多物品时先发送聚合文本，再按物品顺序补发图片。
- 图表只记录成功抓取；失败不会新增数据点，缺少行情时显示占位。

字段清单：`docs/PAYLOAD_FIELDS.md`（输出模板）、`docs/DATA_INVENTORY.md`（插件能取到的全部数据）。
自动交易实现与状态机：`docs/AUTO_TRADING.md`。
