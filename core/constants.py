"""Gaijin Market 接口契约常量。

本模块是唯一允许出现"魔法字符串/魔法数字"的地方：端点、action 名、
价格刻度、错误码全部集中于此，其它模块不得硬编码。
"""

# ==================== 网络端点 ====================
PROXY_BASE = "https://market-proxy.gaijin.net"

#: 行情接口(盘口/搜索/统计)。cln_* 系列 action 走这里。
EP_TRADE = PROXY_BASE + "/web"
#: 鉴权接口(用户校验/refresh_token 换 JWT)。cmn_* 系列 action 走这里。
EP_MARKET = PROXY_BASE + "/market"
#: 资产接口(暂未使用，保留以便扩展)。

# ==================== SSO 登录 ====================
LOGIN_BASE = "https://login.gaijin.net"
SSO_LOGIN_PATH = "/en/sso/login/"
#: 站点 settings.json 中公开的 public_key
SSO_PUBLIC_KEY = "7Cgsc5xNVXm3Yup9WGuD"
#: base64("https://trade.gaijin.net")
SSO_RETURN_URL = "aHR0cHM6Ly90cmFkZS5nYWlqaW4ubmV0"

# ==================== action ====================
ACT_BOOKS_BRIEF = "cln_books_brief"  # 精简盘口 -> 最低售价/最高求购
ACT_MARKET_SEARCH = "cln_market_search"  # 模糊搜索(返回物品列表)
ACT_MARKET_INFO = "cln_market_info"  # 游戏列表/限价/手续费
ACT_AUTH_REFRESH = "cln_authorize_by_refresh_token"
ACT_CHECK_AUTH = "cmn_check_user_auth"

# ==================== 价格刻度（实测锚定，改动前务必回归验证）====================
#: 盘口原始值 / 10000 = GJN。锚点：BUY[0]=380000 时页面显示 38.00
BOOK_PRICE_SCALE = 10_000
#: 搜索接口原始值 / 1e8 = GJN（比盘口大 1e4 倍，勿混用）
SEARCH_PRICE_SCALE = 100_000_000

#: 全站限价（cln_market_info 返回值换算后）

# ==================== 默认值 ====================
DEFAULT_APPID = "1067"  # War Thunder，目前唯一 appid
DEFAULT_LANGUAGE = "zh_CN"
DEFAULT_POLL_INTERVAL = 30  # 秒
MIN_POLL_INTERVAL = 1  # 秒，单条订阅的硬下限
MAX_POLL_INTERVAL = 3600  # 秒，单条订阅的硬上限
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 2
MIN_REQUEST_INTERVAL = 0.4  # 同一时刻两次请求的最小间隔(秒)，温柔限速
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: 搜索时 options 参数：只看有售出挂单的物品
SEARCH_OPTIONS_SELL = "any_sell_orders;include_marketpairs"

# ==================== 业务错误码 ====================
ERR_TOKEN_REQUIRED = "TOKEN_REQUIRED"
ERR_INVALID_TOKEN = "INVALID_TOKEN"
ERR_BAD_REFRESH_TOKEN = "BAD_REFRESH_TOKEN"

#: 推送模式
PUSH_ON_CHANGE = "on_change"  # 仅价格变化时推送
PUSH_ALWAYS = "always"  # 每次轮询都推送
PUSH_ON_TRIGGER = "on_trigger"  # 仅命中阈值时推送

PUSH_MODES = (PUSH_ON_CHANGE, PUSH_ALWAYS)
#: 兜底模板（平台类型未知时使用）

#: 数字格式化
PRICE_DECIMALS = 2

# 网页快照模式：关闭 / 价格变化后截 / 仅阈值触发时截
SNAPSHOT_MODES = ("off", "on_change")

#: 推送合并窗口（秒）：窗口内多件物品的变化合并成一条消息；0 = 不合并
PUSH_BATCH_SECONDS = 10
