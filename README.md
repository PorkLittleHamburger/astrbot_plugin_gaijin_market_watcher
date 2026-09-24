# Gaijin 市场行情监控

监控 [Gaijin Market](https://trade.gaijin.net/market/sell) 上物品的**最低售价**、**最高求购价**与**挂单量**，
变化时推送到群聊或私聊，可 @ 到订阅者本人。

## 功能

- **行情监控**：按物品页链接订阅，实时抓取最低售价 / 最高求购价 / 两侧挂单量
- **多用户订阅**：每个用户一条配置（UID、刷新间隔、物品链接列表）；同一用户的物品共用一个刷新间隔，
  物品的抓取频率取所有订阅者的最小值，而每个人的推送不会快于他自己设的间隔
- **推送合并**：可设时间窗，窗口内多件物品的变化合成**一条**消息，件与件之间空一行
- **自定义输出模板**：用 `$字段` 占位渲染推送正文（字段清单见 `docs/PAYLOAD_FIELDS.md`）
- **商品页快照**：可选在价格变化时截取商品页，等渲染完成后与正文**一起**发出
- **两步验证登录**：账号密码 + 验证码登录，之后用 refresh_token 自动续期，无需重复登录

## 适配平台

| 平台 | @ 订阅者 | 图片（快照） | 主动推送 |
| --- | --- | --- | --- |
| QQ 官方机器人（`qq_official` / `qq_official_webhook`） | 文本内嵌标记（走 markdown 通道） | 支持 | 支持 |
| 个人微信（`weixin_oc`） | 纯文本 @昵称（该平台不认 At 组件） | 支持（消息必须带文字） | 需用户先给机器人发过消息 |
其余平台未做测试

平台差异（@ 的方式、是否要求带文字、主动推送前提）集中写在 `core/platforms.py`，增改支持只需改这一个文件。

## 使用方法

### 1. 安装

需要 AstrBot ≥ 4.23.6、Python ≥ 3.10。

在 AstrBot 插件市场搜索Gaijin 市场行情监控，或通过以下仓库地址安装：
```text
https://github.com/PorkLittleHamburger/astrbot_plugin_gaijin_market_watcher
```
安装、更新、删除等操作需要 AstrBot 管理员权限，请先在管理面板配置管理员。
商品页快照需要 `playwright` 与 chromium。

### 2. 登录 Gaijin 账号

在插件配置里填 `account_email` / `account_password`，并把你的 UID 填进 `owner_uids`（发 `/gjm id` 可查看）。
请注意，插件只会将密码和账户储存在本地。
然后在聊天里：

```
/login              发起登录
/login <验证码>     提交两步验证码
```

### 3. 订阅物品

```
/gjm sub <物品页链接>      订阅（只接受物品页链接）
/gjm unsub <物品页链接>    取消订阅
/gjm my                   查看我的订阅
/gjm interval <秒>        设置自己的刷新间隔（对该用户名下所有物品生效）
```

### 4. 查看与排障

```
/gjm status               运行与令牌状态
/gjm list                 被监控的物品与最新行情
/gjm price <物品链接>     查询单个物品的实时行情
/gjm shot [物品链接]      立即拍一张商品页快照
/gjm id                   查看本会话 UID / UMO
/gjm help                 查看全部指令
```

### 主要配置项

| 配置 | 说明 |
| --- | --- |
| `account_email` / `account_password` | Gaijin 账号，供 `/login` 使用 |
| `owner_uids` | 拥有者 UID，`/login` 与管理员指令的身份依据 |
| `user_subscriptions` | 用户订阅：每用户一条（UID、刷新间隔、物品链接列表） |
| `push_mode` | `on_change` 价格变化时推（推荐）/ `always` 每轮都推 |
| `push_batch_seconds` | 推送合并窗口（秒），窗口内多件物品合成一条 |
| `snapshot_mode` / `snapshot_cooldown` | 商品页快照的开关与限频 |
| `payload` | 输出模板，留空则使用默认可读文本 |

字段清单：`docs/PAYLOAD_FIELDS.md`（输出模板）、`docs/DATA_INVENTORY.md`（插件能取到的全部数据）。
