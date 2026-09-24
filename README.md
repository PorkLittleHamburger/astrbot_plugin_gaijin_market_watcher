# Gaijin 市场行情监控

监控 Gaijin Market 上物品的**最低售价**、**最高求购价**与**挂单量**，变化时推送到群聊或私聊（可 @ 到本人）。

- 版本：`2.9.0`　插件名：`astrbot_plugin_gaijin_market_watcher`
- 依赖：`httpx`（行情）、`playwright`（可选的商品页快照）

## 安装与首次使用

1. 解压到 `AstrBot/data/plugins/`，在 WebUI 里重载插件；
2. 配置里填 `account_email` / `account_password`，并把 `owner_uids` 填上（聊天里发 `/gjm id` 可查看自己的 UID）；
3. 聊天里发 `/login` 发起登录，按提示再用 `/login <验证码>` 提交两步验证码；
4. 订阅物品：`/gjm sub <物品页链接>`，或在配置的「用户订阅」里添加。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `account_email` / `account_password` | 空 | 登录用；密码留空则只靠 refresh_token |
| `twofa_code` | 空 | 备用验证码入口：填写保存后自动提交并清空 |
| `jwt_token` / `refresh_token` | 空 | 令牌，一般无需手填 |
| `appid` | `1067` | 1067 = War Thunder |
| `language` | `zh_CN` | 物品名称显示语言 |
| `push_mode` | `on_change` | `on_change` 价格变化时推（推荐）/ `always` 每轮都推 |
| `push_batch_seconds` | `10` | 推送合并窗口：窗口内多件物品的变化合成一条消息（0=每件单独发） |
| `mention_users` | `true` | 推送时 @ 订阅者 |
| `snapshot_mode` | `on_change` | 网页快照：`off` 关闭 / `on_change` 价格变化后截图 |
| `snapshot_cooldown` | `600` | 同一会话两次截图的最小间隔（秒） |
| `payload` | 空 | 输出模板：用 `$字段` 占位渲染推送正文，字段见 `docs/PAYLOAD_FIELDS.md` |
| `request_timeout` / `request_retries` | `15` / `2` | 请求超时与重试次数 |
| `item_interval` | `0.4` | 同一轮内多个物品之间的请求间隔（秒） |
| `owner_uids` | 空 | 拥有者 UID，`/login` 与管理员指令的身份依据 |
| `user_subscriptions` | 空 | 用户订阅：**每用户一条**（UID、刷新间隔、物品链接列表） |

## 指令

```
/login [验证码]          登录 / 提交两步验证码
/gjm sub <物品链接>      订阅
/gjm unsub <物品链接>    取消订阅
/gjm my                  我的订阅
/gjm list                被监控的物品与最新行情
/gjm price <物品链接>    查询单个物品行情
/gjm shot [物品链接]     立即拍一张商品页快照
/gjm id                  查看本会话 UID / UMO
/gjm status              运行与令牌状态
/gjm now                 立即刷新一轮（拥有者）
/gjm interval <秒>       设置自己的刷新间隔（拥有者）
/gjm token <回跳链接>    手动导入 refresh_token（拥有者）
/gjm debug               排障信息（拥有者）
```

## 关键行为

- **刷新间隔是"用户级"的**：一个用户的全部物品共用一个间隔（配置界面里同一 UID 折叠成一个表单）。
  物品的**抓取频率**取所有订阅者的最小值，而每个人的**推送频率**不会快过他自己设的间隔。
- **多件物品合并成一条消息**：`push_batch_seconds`（默认 10 秒）窗口内到达的变化会合成一条再发，
  因此各物品按各自节奏到点也不会刷屏；同一件物品在窗口内重复变化只保留一条。
- **输出模板 `payload`**：写 JSON 骨架时键名不会出现在消息里，按书写顺序逐行输出值；
  数组值一行一项（元素内不换行）；`image` 键是图片附件 —— 需要现拍时会**等快照渲染完再图文同发**。
  模板渲染失败会自动回退为可读文本，不会把推送搞坏。
- **商品页快照**：复用插件自己维护的 JWT（注入浏览器 localStorage），无需密码；单张约 15 秒。
- **@ 订阅者**：QQ 官方机器人不支持 At 组件，改用文本内嵌标记并走 markdown 通道（已实测）；其它平台用标准 At 组件。
- **数据落盘**：`AstrBot/data/plugin_data/astrbot_plugin_gaijin_market_watcher/`
  （`quotes.json` 最新行情、`history.json` 历史点、`names.json` 名称缓存、`items_meta.json` 图标元数据、
  `sessions.json` UID→会话映射、`subs.json` 订阅快照、`auth.json` 令牌、`snapshots/` 快照图）。

## 平台支持

各平台适配器的出站行为并不一致（差异藏在适配器实现里），插件已按实测结论分别处理：

| 平台 | @ 订阅者 | 图片（快照） | 主动推送 |
| --- | --- | --- | --- |
| qq_official | 文本内嵌标记（markdown 通道） | 支持 | 支持（群聊需适配器补丁，本插件已处理） |
| qq_official_webhook | 文本内嵌标记（markdown 通道） | 支持 | 支持 |
| aiocqhttp / OneBot | At 组件 | 支持 | 支持 |
| telegram / discord / slack … | At 组件 | 支持 | 支持 |
| weixin_oc（个人微信） | 纯文本 @昵称（At 会被忽略） | 支持（但消息必须带文字） | 需 context_token：用户先发过消息才行 |
| weixin_official_account | 纯文本 @昵称 | 支持 | 受微信客服消息时限约束 |
| wecom（客服模式） | 纯文本 @昵称 | 支持 | 不支持 |

要点：

- **QQ 官方机器人**（`qq_official` / `qq_official_webhook`）：适配器出站只取纯文本或 markdown，
  `At` 组件会被**静默丢弃**，因此插件改用**文本内嵌标记并强制走 markdown 通道**（已真机验证）。
- **个人微信**（`weixin_oc`）：出站只处理 `Plain`/`Image`/`Video`/`File`，`At` 会被忽略 ——
  插件对这些平台**降级为纯文本 @昵称**；且该平台会丢弃**没有文字**的消息，插件会自动补一句兜底文字；
  主动推送需要 `context_token`（用户先给机器人发过消息才会有），缺失时平台会跳过发送，
  插件检测到发送失败会**通知拥有者**（同一会话限频 30 分钟）。
- **企业微信客服模式**（`wecom`）：平台本身**不支持主动推送**，价格提醒无法送达（插件会告知）。
- 其它平台（telegram / discord / slack / aiocqhttp 等）用标准 `At` 组件，无需特殊处理。
- 平台差异集中在 `core/platforms.py` 一个模块里，新增平台支持只需改这一处。

## 已知限制

- 只有 `appid=1067`（War Thunder）。
- 订阅只接受**物品页链接**（名称搜索不可靠，故不支持）。
- 刷新间隔过短可能触发站点风控（配置体检会提示）。
- 需要 `playwright` + chromium 才能用网页快照；缺失时只发文字并记一条警告。
- `refresh_token` 失效后需重新 `/login`；登录依赖站点当前的 SSO 表单结构。

## 文档

- `docs/PAYLOAD_FIELDS.md` —— 输出模板的全部字段与别名
- `docs/DATA_INVENTORY.md` —— 插件能取到的全部数据（接口字段、本地落盘、边界）
