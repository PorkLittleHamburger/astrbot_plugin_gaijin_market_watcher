# Gaijin 市场行情监控与自动交易

监控 [Gaijin Market](https://trade.gaijin.net/market/sell) 上物品的**最低售价**、**最高求购价**与**挂单量**，
变化时推送到群聊或私聊，可 @ 到订阅者本人。

## 功能

- **行情监控**：按物品页链接订阅，实时抓取最低售价 / 最高求购价 / 两侧挂单量
- **会话级订阅**：同一用户可在不同群聊或私聊分别维护自己的订阅
- **群聊聚合**：固定间隔刷新群内全部订阅物品，合并成一条快照并分别 @ 订阅者
- **价格折线图**：每次自动行情推送按物品展示最近 N 次最低售价和最高求购
- **两步验证登录**：账号密码 + 验证码登录，之后用 refresh_token 自动续期，无需重复登录
- **自动交易**：监测普通商品盘口，按限价买入，达到税后目标价后卖出；默认模拟，二次确认后实盘
- **交易控制台**：WebUI 提供规则、预算、订单、日志和急停；聊天交易仅限拥有者私聊

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

在 AstrBot 插件市场搜索Gaijin 市场行情监控，或通过以下仓库地址安装：
```text
https://github.com/PorkLittleHamburger/astrbot_plugin_gaijin_market_watcher
```
安装、更新、删除等操作需要 AstrBot 管理员权限，请先在管理面板配置管理员。

### 2. 登录 Gaijin 账号

在插件配置里填 `account_email` / `account_password`，并把你的 UID 填进 `owner_uids`
（可发送 AstrBot 内置命令 `/sid` 查看；留空则回退 AstrBot 管理员判定）。
请注意，插件只会将密码和账户储存在本地。
登录成功后的续期凭据由插件内部保存，WebUI 不提供令牌字段。
**自动交易不会回退管理员判定：未配置 `owner_uids` 时交易功能直接禁用。**
然后在聊天里：

```
/login              发起登录
/login <验证码>     提交两步验证码
```

### 3. 自动交易（仅拥有者私聊）

自动交易首版只支持 `COMMODITY` 普通商品，拍卖物品会拒绝创建规则。手续费固定为 15%。
新规则始终从模拟盘开始，必须先在 WebUI 设置总开关、总预算、每日限额、单笔上限和最大数量。

```
/gjm trade add <物品链接> <买入价> <数量> <税后卖出价> <once|loop>
/gjm trade list
/gjm trade show <规则ID>
/gjm trade arm <规则ID>                 查看实盘风险摘要
/gjm trade arm <规则ID> confirm         60 秒内二次确认切换实盘
/gjm trade pause|resume|disarm <规则ID>
/gjm trade cancel <规则ID>              撤销本插件为该规则创建的订单并暂停
/gjm trade delete <规则ID>
/gjm trade orders
/gjm trade log [1-50]
/gjm trade budget
/gjm trade stop-all confirm             撤销全部自动订单并暂停所有规则
```

买入逻辑：最低售价达到买入限价后，按阈值内卖单深度买入；允许部分成交，并立即撤销未成交余量。
卖出逻辑：目标税后价以上的求购单覆盖多少就卖多少；剩余持仓按可达到目标到手价的挂单价发布卖单。

WebUI 的“插件可视化视图 → Gaijin 自动交易”页面可由所有已登录 AstrBot Dashboard 管理员操作。
聊天侧仍严格要求 `owner_uids` 拥有者和 `FriendMessage` 私聊。

### 4. 订阅物品

```
/gjm sub <物品页链接>      订阅（只接受物品页链接）
/gjm mysub                先刷新行情，再查看当前会话中我的订阅和全局列表
/gjm unsub <编号|物品链接> 取消订阅
/gjm refresh mode <模式>  always / only_change
/gjm refresh interval <秒> [物品链接|编号]
```

### 5. 查看与排障

```
/gjm status               运行与令牌状态
/gjm price <物品链接>     查询单个物品的实时行情
/gjm help                 查看全部指令
```

### 主要配置项

| 配置 | 说明 |
| --- | --- |
| `account_email` / `account_password` | Gaijin 账号，供 `/login` 使用 |
| `owner_uids` | 拥有者 UID，`/login` 与管理员指令的身份依据 |
| `user_subscriptions` | 用户订阅：每用户一条（UID、刷新模式、逐物品链接和间隔） |
| `push_mode` | 旧配置的默认刷新模式；新订阅可用 `/gjm refresh mode` 覆盖 |
| `push_batch_seconds` | 私聊推送合并窗口（秒），窗口内多件物品合成一条 |
| `group_push_interval` | 群聊全量推送间隔，默认 30 秒 |
| `chart_history_points` | 每张价格图展示的最近成功采样次数，默认 5 |
| `payload` | 输出模板，留空则使用默认可读文本 |

### 刷新模式

- `always`：按照该物品设置的刷新间隔抓取并推送，即使行情没有变化也会发送。
- `only_change`：固定每 5 秒检查一次网站，只在价格发生变化时发送。
- 群聊不使用个人 `always` / `only_change` 控制发送；插件每个 `group_push_interval` 主动刷新该群全部物品并发送完整快照。
- `/gjm refresh mode always|only_change`：设置自己全部订阅的刷新模式。
- `/gjm refresh interval <秒>`：设置自己全部订阅的默认刷新间隔。
- `/gjm refresh interval <秒> <物品链接|编号>`：只修改指定物品；编号来自 `/gjm mysub`。

### 价格折线图

- 每次自动私聊推送和群聊快照都会按物品附带价格图。
- 图中同时展示最低售价与最高求购，横轴为本地时间，纵轴为 GJN。
- 每个物品独立一张图；QQ 官方每条消息只支持一张图片，因此多物品时先发送聚合文本，再按物品顺序补发图片。
- 图表只记录成功抓取；失败不会新增数据点，缺少行情时显示占位。

字段清单：`docs/PAYLOAD_FIELDS.md`（输出模板）、`docs/DATA_INVENTORY.md`（插件能取到的全部数据）。
自动交易实现与状态机：`docs/AUTO_TRADING.md`。
