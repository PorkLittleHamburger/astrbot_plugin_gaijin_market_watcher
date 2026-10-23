# Gaijin 市场行情监控插件 · 数据清单

> 版本 `v2.17.1`　|　更新日期 2026-10-23　|　接口字段来自真实探测结果，插件落盘结构已按当前实现更新

---

## 一、实时行情　`cln_books_brief`　✅ 插件正在用

`POST https://market-proxy.gaijin.net/web`（表单编码）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `SELL` | `[[价, 量], ...]` | 卖单，**升序**（首项 = 最低售价），**最多 100 档** |
| `BUY` | `[[价, 量], ...]` | 买单，**降序**（首项 = 最高求购价），**最多 100 档** |
| `depth.SELL` / `depth.BUY` | int | 该侧挂单总量 |
| `type` | str | `COMMODITY` / 其它 |
| `success` | bool | 业务成功与否 |

**价格刻度：盘口原始值 ÷ 10000 = GJN**（例 `450000` → `45.00 GJN`）

实测样本（2026-09-24，F-16XL）：

```
SELL[0..2] = [450000,3] [455000,1] [458000,1]      → 45.00 / 45.50 / 45.80 GJN
BUY [0..2] = [420000,1] [415100,2] [415000,1]      → 42.00 / 41.51 / 41.50 GJN
depth      = SELL 570 / BUY 2168
```

> ⚠️ **插件目前只用了 `SELL[0]`、`BUY[0]` 与两个 depth**。
> 另外 **99 档盘口数据每轮都被丢弃** —— 这是当前最大的未开发数据源。

---

## 二、物品元数据　`cln_market_search`　✅ 已接线

插件在首次抓取物品行情后按名称搜索，并缓存匹配项的图标、标签、稀有度与颜色。每个 `asset` 返回：

| 字段 | 示例 | 说明 |
| --- | --- | --- |
| `appid` | `1067` | 游戏 |
| `hash_name` | `id50381_f_16xl_usa` | **即插件里的 market_name** |
| `name` | `F-16XL（美国）` | 本地化名称（按 `language`） |
| `commodity` | `true` | 是否同质化商品 |
| `icon` | `https://static-ggc.gaijin.net/units/f_16xl.png` | 图标（可做缩略图推送） |
| `price` | `4499000000` | 最低售价，**刻度 ÷1e8** → 44.99 GJN |
| `buy_price` | `4200000000` | 最高求购，同上 → 42.00 GJN |
| `depth` / `buy_depth` | `572` / `2168` | 两侧挂单量 |
| `tags` | `["type:aircraft","quality:ultraRare","country:usa","inGamePreview:yes"]` | 类型 / 稀有度 / 国家 |
| `color` | `C816C1` | 稀有度颜色 |
| `asset_class` | `[{"name":"__itemdefid","value":"50381"}]` | 资产分类键值 |

> 两种价格刻度**不可混用**：盘口 ÷10000，搜索 ÷1e8。

---

## 三、市场与收费元数据　`cln_market_info`　⚠️ 未接线

| 字段 | 实测值 | 说明 |
| --- | --- | --- |
| `games[]` | 1 项 | `appid` / `name` / `briefName` / `desc[]` / `icon` / `banner` |
| `marketFee` | `0.05` | **手续费 5%**（可算到手价） |
| `minPrice` / `maxPrice` | `1000` / `20000000` | 限价，原始刻度 → **0.10 / 2000.00 GJN** |
| `auction.minDurationHours` / `maxDurationHours` | `168` / `720` | 挂单时长 7~30 天 |
| `marketUserToken` | 40 位 hex | 会话级市场 token |

**手续费换算示例**：挂单 47.99 GJN → 到手 `47.99 × 0.95 = 45.59 GJN`。

---

## 四、价格走势　`cln_get_pair_stat`　⚠️ 未接线（当前为空）

- 需要 `currencyid` 参数（缺了会报 `CURRENCYID_EXPECTED`）
- 返回结构：`{"1h": [...], "1d": [...]}` 时间序列
- **实测该物品两个序列都是空数组** —— 可能仅对特定物品类型有数据，需进一步试参

---

## 五、账号数据　`cmn_check_user_auth`　⚠️ 只读了其中两项

`POST https://market-proxy.gaijin.net/market`

| 字段 | 示例 | 插件是否使用 |
| --- | --- | --- |
| `userId` | `155305772` | 用（`/gjm debug`） |
| `nick` | `PorkLittleBurger` | 用（状态显示） |
| `mail` | `h***@outlook.com` | **不用、不落地** |
| `country` | `JP` | 不用 |
| `roles` | `CLIENT;ANONYMOUS` | 不用 |
| `2step` | `true` | 用（`has_2step`） |
| `email_verified` | `true` | 不用 |
| `wealthy_gjn` | `false` | 不用 |
| `ext_user_tags` / `deniedTags` | `[]` | 不用 |
| `policy.check2factor` / `checkEmailVerification` / `checkDeniedTags` | — | 不用 |

**JWT 载荷可读出**：`nick` `uid` `exp` `iat` `cntry` `lng` `auth`(2step) `tgs` `iss` `kid` `slt` `fac` `loc`

**登录态信息**（插件内部）：`refresh_token` 是否存在、JWT 剩余秒数、是否正等待验证码、续期错误。

> 🔒 隐私说明：`mail`（邮箱）属于个人信息。插件的 `check_auth` 虽会取回整个对象，但**只读 `nick`/`userId`**，
> 不写入任何落盘文件、不进入推送内容。你在 `/gjm debug` 里能看到的是 `nick` 与 `userId`。

---

## 六、插件本地落盘　`data/plugin_data/astrbot_plugin_gaijin_market_watcher/`

群聊快照按 `group_push_interval` 周期主动刷新群内全部订阅物品。同一物品只保留一个行情块，
该物品的全部群订阅者都会出现在对应 @ 列表中；没有成功行情时显示占位。

| 文件 | 内容 |
| --- | --- |
| `quotes.json` | 每物品最近一次行情，并内嵌最近最多 50 个 `{t, sell, buy}` 价格点，用于折线图 |
| `names.json` | `market_name → 显示名` 缓存 |
| `sessions.json` | `UID → 会话 UMO` + 用户昵称（推送与 @ 的前提） |
| `pending_login.json` | 待提交验证码的临时会话 Cookie 与表单字段（15 分钟过期） |
| `items_meta.json` | 物品图标、标签、稀有度与颜色缓存 |
| `auth.json` | `refresh_token` + `token` + 更新时间（**不含账号密码**） |

用户订阅、所属会话 `target`、刷新间隔与模式保存在 AstrBot 插件配置 `user_subscriptions` 中。

旧版独立 `history.json` 只作为兼容数据源读取；新行情点直接写入 `quotes.json`，不再产生额外历史文件写盘。

---

## 七、平台侧数据（QQ 官方，由消息事件带入）

| 数据 | 用途 |
| --- | --- |
| UID（= 用户 openid） | 身份判定、按人订阅、@ 目标 |
| 昵称 | @ 时的显示名、推送文案 |
| 群 ID / 会话 UMO | 推送目标定位 |
| 平台类型（`peral` 等） | 决定 @ 的实现方式、是否启用平台专有能力 |
| 是否管理员 | 未配置拥有者 UID 时的回退判定 |

---

## 八、明确拿不到的（边界）

- **成交历史 / 成交量**：站点没有公开接口；`cln_get_pair_stat` 对该物品返回空
- **挂单归属**：盘口只给「价格 + 数量」，拿不到是谁挂的
- **物品内部 ID 之外的资产详情**：如具体涂装/编号等（`asset_class` 只给 `__itemdefid`）
- **任何交易能力**：插件只读，不做下单/撤单（也不该做）
- **聊天内容**：插件不读消息正文（只用指令参数与发送者标识）

---

## 九、可做但尚未做（按性价比排序）

1. **100 档盘口** → 买卖价差、挂单墙、深度图；数据每轮已在手，纯浪费
2. **`marketFee`** → 推送里直接给「到手价」，避免误判收益
3. ~~**`tags` / `color` / `icon`**~~ → **已实现**：缓存后用 payload 的 `$tags` / `$rarity` / `$color` / `$icon`
4. `cln_get_pair_stat` → 官方走势图（需再试参）
5. `cln_market_search` → 用名称检索来订阅（现在严格只接受链接）

---

## 附：原始响应样本位置

本地探测样本已存于工程 `api_samples/`：`probe.json`（books_brief / market_info / market_search / pair_stat）、
`probe2.json`（check_user_auth / books / asset_class / pair_stat 变体）。
