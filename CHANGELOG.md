# 更新日志

## 2.9.3

### 修复

- QQ 官方机器人私聊（C2C）推送失败：该接口不支持群聊专用的 `<qqbot-at-user>` @ 标记，
  带上去会被平台拒绝并抛 `ServerError: C2C消息不支持qqbot-at-user.`（适配器只会重试，救不回来）。
  现在按会话类型决定 @：私聊（`FriendMessage` / 单聊）自动跳过 @，
  正文里若带该标记也会被剔除；群聊 / 频道行为不变。
  平台差异（含这条）仍然只写在 `core/platforms.py`。

## 2.9.2

### 变更

- 精简 `metadata.yaml` 里的插件描述文案
- `main.py` 与 `metadata.yaml` 的版本号同步为 2.9.2

## 2.9.1

### 修复

- 日志记录器全部改为从 `astrbot.api` 导入（上架规范硬性要求）：
  删除 `core/payload.py` 里未被使用的模块级 `logging.getLogger("astrbot")`；
  `core/snapshot.py` 在未传入 `logger` 时的缺省值改为 `from astrbot.api import logger`，
  不再使用标准库 `logging`。

## 2.9.0

首个公开版本。

### 功能

- 行情监控：按物品页链接订阅，抓取最低售价 / 最高求购价 / 两侧挂单量
- 多用户订阅：每个用户一条配置（UID、刷新间隔、物品链接列表），同一用户的物品共用一个刷新间隔
- 推送合并：时间窗内多件物品的变化合成一条消息，件与件之间空一行
- 自定义输出模板：用 `$字段` 占位渲染推送正文
- 商品页快照：价格变化时截取商品页，等渲染完成后与正文一起发出
- 两步验证登录：账号密码 + 验证码，之后用 refresh_token 自动续期

### 适配平台

- QQ 官方机器人（文本内嵌 @ 标记，走 markdown 通道）
- OneBot / aiocqhttp、Telegram / Discord / Slack 等（标准 At 组件）
- 个人微信 / 微信公众号 / 企业微信（纯文本 @，按平台能力自动降级）

### 校验

- `ruff check` 与 `ruff format` 全绿，16 个模块语法检查通过
