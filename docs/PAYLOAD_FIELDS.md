# payload 输出模板 · 字段清单

> 插件版本 `v2.8.5`　|　共 **48** 个字段 + **19** 个别名

配置项 `payload` 决定一条推送**长什么样**。**出厂已预填下面的模板**；清空该项 = 回到默认可读文本。

## 写法（JSON 骨架）

```json
{
    "title":"$item_name行情报告",
    "datetime":"时间：$datetime",
    "content":["卖单最低$sell_lowest_price，较上次变化$sell_change_amount($sell_change_rate)",
                "买单最高$buy_highest_price，较上次变化$buy_change_amount($buy_change_rate)",
                "卖单数$sell_count,较上次变化$sell_count_change_amount($sell_count_change_rate)",
                "买单数$buy_count,较上次变化$buy_count_change_amount($buy_count_change_rate)"],
    "image":"$image"
}
```

**实际发出的消息**（键名与花括号都不出现，只出现值）：

```
@猪肉小憨包er
F-16XL（美国）行情报告
时间：2026-09-24-17-40-12
卖单最低45.10，较上次变化-2.00(-4.2%)
买单最高43.00，较上次变化+2.00(+4.9%)
卖单数33,较上次变化+2(+6.5%)
买单数21,较上次变化-6(-22.2%)
[图片]
```

## 拼装规则

| 规则 | 说明 |
| --- | --- |
| 顺序 | 按你写 JSON 的顺序自上而下拼接 |
| 字符串值 | 替换 `$变量` 后成为**一行** |
| **数组值** | **一个元素一行**；元素内部**不换行**（换行会被压成空格） |
| 空值段 | 自动省略（`$group` 在私聊、`$icon` 未缓存…），不产生空行 |
| 键名 `image`/`icon`/`img`/`picture`… | 作为**图片附件**，不占文字 |
| 键名 `mention`/`at` | 输出 @ 标记；用了它插件就不再自动加 @ |
| 多件物品 | 按“单件”模板逐件渲染后拼接 |
| 渲染失败/为空 | 记警告并**整条回退**为可读文本 |

## 图片：等拍完再一起发

键值里出现 `$image` 时，插件会：

1. 按 `snapshot_mode` / `snapshot_cooldown` 判断是否现拍；
2. **等商品页快照渲染完**（约十几秒）；
3. 把正文与图片**放在同一条消息里**发出（不再另发一张图）。

拍不了（未开快照 / 冷却中 / 渲染失败）→ 只发正文，不影响推送。
开快照的投递走后台任务，因此**不会拖住价格抓取循环**。

## 一、整轮字段

| 字段 | 说明 |
| --- | --- |
| `$title` | 推送标题 |
| `$datetime` | 本轮时间（YYYY-MM-DD-HH-MM-SS，全横线） |
| `$datetime_plain` | 本轮时间（YYYY-MM-DD HH:MM:SS，带空格） |
| `$timestamp` | 本轮时间（Unix 秒，整数） |
| `$digest` | 整轮可读正文（含表头与抓取失败项） |
| `$count` | 本轮物品数量 |
| `$uid` | 收件人 UID |
| `$user` | 收件人昵称 |
| `$group` | 群 ID（私聊为空串） |
| `$platform` | 平台类型，如 qq_official |
| `$mention` | 该平台的 @ 标记；用了它插件就不再自动加 @ |
| `$mode` | 推送模式 on_change / always / on_trigger |
| `$failures` | 本轮抓取失败的物品名（顿号分隔） |
| `$failures_json` | 失败明细 [{name, error}] |
| `$appid` | 游戏 appid |

## 二、物品字段

| 字段 | 说明 |
| --- | --- |
| `$name` | 物品显示名（别名 $item_name） |
| `$market_name` | 市场 slug（如 id50381_f_16xl_usa） |
| `$content` | 该件物品的可读文本块（多行） |
| `$sell` | 最低售价（数字，GJN） |
| `$buy` | 最高求购（数字，GJN） |
| `$sell_text` | 最低售价（两位小数文本，别名 $sell_lowest_price） |
| `$buy_text` | 最高求购（两位小数文本，别名 $buy_highest_price） |
| `$sell_change_amount` | 最低售价较上次的变化量（带符号，两位小数） |
| `$sell_change_rate` | 最低售价变化率（带符号，如 -25% / +2.3%） |
| `$buy_change_amount` | 最高求购较上次的变化量 |
| `$buy_change_rate` | 最高求购变化率 |
| `$sell_delta` | 最低售价变化（含箭头与百分比的描述文本） |
| `$buy_delta` | 最高求购变化（同上） |
| `$sell_depth` | 出售挂单量（别名 $sell_count） |
| `$buy_depth` | 求购挂单量（别名 $buy_count） |
| `$sell_count_change_amount` | 出售挂单量变化量 |
| `$sell_count_change_rate` | 出售挂单量变化率 |
| `$buy_count_change_amount` | 求购挂单量变化量 |
| `$buy_count_change_rate` | 求购挂单量变化率 |
| `$url` | 商品页链接 |
| `$image` | **商品页快照**（图片附件；需要现拍时会等它渲染完再一起发） |
| `$icon` | 物品图标 URL |
| `$tags` | 标签（顿号分隔，如 type:aircraft、quality:ultraRare） |
| `$tags_json` | 标签数组 |
| `$rarity` | 稀有度（由标签推导，如 超稀有） |
| `$color` | 稀有度颜色（如 C816C1） |
| `$reasons` | 阈值文案（门限配置已移除，恒为空串） |
| `$reasons_json` | 阈值文案数组（恒为空） |
| `$reason` | 第一条阈值文案（恒为空） |
| `$kind` | 物品类型（如 COMMODITY） |
| `$error` | 该物品的抓取错误（正常为空串） |
| `$item_time` | 该物品行情时间（YYYY-MM-DD HH:MM:SS） |
| `$item_timestamp` | 该物品行情时间（Unix 秒） |

## 三、别名（可直接用你的叫法）

| 别名 | 等于 |
| --- | --- |
| `$buy_change_pct` | `$buy_change_rate` |
| `$buy_count` | `$buy_depth` |
| `$buy_count_change_pct` | `$buy_count_change_rate` |
| `$buy_highest_price` | `$buy_text` |
| `$buy_orders` | `$buy_depth` |
| `$buy_price` | `$buy_text` |
| `$goods_name` | `$name` |
| `$highest_price` | `$buy_text` |
| `$item_name` | `$name` |
| `$lowest_price` | `$sell_text` |
| `$now` | `$datetime` |
| `$round_time` | `$datetime` |
| `$sell_change_pct` | `$sell_change_rate` |
| `$sell_count` | `$sell_depth` |
| `$sell_count_change_pct` | `$sell_count_change_rate` |
| `$sell_lowest_price` | `$sell_text` |
| `$sell_orders` | `$sell_depth` |
| `$sell_price` | `$sell_text` |
| `$time` | `$datetime` |

## 四、变化量 / 变化率的格式

- `$sell_change_amount` → 带符号的变化量，如 `-2.00`（无变化为 `0`）；
- `$sell_change_rate` → 带符号百分比，如 `-4.2%`（上次为 0 或首次时为空串→该段省略）；
- 挂单量的 `$sell_count_change_amount` 是整数形式，如 `+2` / `-6`；
- 对比基准是**收件人自己上次收到的行情**（每个订阅者独立）。

## 五、时间格式

- `$datetime` → `2026-09-24-17-40-12`（全横线，按你的示例）；
- `$datetime_plain` → `2026-09-24 17:40:12`（常规写法）；
- `$timestamp` → Unix 秒。
