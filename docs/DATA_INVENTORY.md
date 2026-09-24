# Gaijin 市场行情监控插件 · 数据清单

> 版本 `v2.8.5`　|　生成时间 2026-09-24　|　所有字段均为**真实探测结果**（用你账号的有效 JWT 跑出来的，非文档推测）

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

## 二、物品元数据　`cln_market_search`　⚠️ 已实现但未接线

单页 10 条，每个 `asset` 返回：

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

| 文件 | 内容 |
| --- | --- |
| `quotes.json` | 每物品最近一次行情：`market_name / sell_min / buy_max / sell_depth / buy_depth / timestamp / display_name / app_id / kind / error` |
| `history.json` | 每物品最近 **200** 个点：`{t, sell, buy}`（可用于迷你走势） |
| `names.json` | `market_name → 显示名` 缓存 |
| `sessions.json` | `UID → 会话 UMO` + 用户昵称（推送与 @ 的前提） |
| `subs.json` | 订阅快照（每用户每物品一条） |
| `auth.json` | `refresh_token` + `token` + 更新时间（**不含账号密码**） |
| `snapshots/` | 商品页快照 PNG，自动保留最近 20 张 |

---

## 七、平台侧数据（QQ 官方，由消息事件带入）

| 数据 | 用途 |
| --- | --- |
| UID（= 用户 openid） | 身份判定、按人订阅、@ 目标 |
| 昵称 | @ 时的显示名、`/gjm id` |
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
3. ~~**`tags` / `color` / `icon`**~~ → **已实现**：缓存后用 payload 的 `$tags` / `$rarity` / `$color` / `$image`
4. **`history.json` 200 点** → 迷你走势（现在只存不读）
5. `cln_get_pair_stat` → 官方走势图（需再试参）
6. `cln_market_search` → 用名称检索来订阅（现在严格只接受链接）

---

## 附：原始响应样本位置

本地探测样本已存于工程 `api_samples/`：`probe.json`（books_brief / market_info / market_search / pair_stat）、
`probe2.json`（check_user_auth / books / asset_class / pair_stat 变体）。
