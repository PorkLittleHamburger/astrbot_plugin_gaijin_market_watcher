"""鉴权层：市场令牌（JWT）的生命周期管理。

三级策略，逐级降级：

  1. **现行 JWT 未过期**            -> 直接使用，零请求开销；
  2. **JWT 过期 / 失效，但 RT 可用** -> 方案三：`cln_authorize_by_refresh_token`
     用 refresh_token 静默换新 JWT，**全程不需要密码**，可长期无人值守；
  3. **RT 也失效**                  -> 方案二：SSO 表单登录（账号密码 + 可能的 2FA 验证码）。

【登录为什么是"两阶段"】
  站点是服务端渲染的传统表单：先 POST 账号密码，站点再返回"请输入验证码"的页面。
  第二次提交必须携带第一次的会话 Cookie，因此这里把"待提交状态"持久化到磁盘
  （会话 Cookie + 表单隐藏字段 + 验证码字段名）。这样做的好处：
    * 在 WebUI 页面里点按钮发起登录 -> 填验证码 -> 提交，两步之间可以隔任意长时间；
    * 即使中途插件被重载（AstrBot 保存配置常会触发重载），流程也能接着走；
    * 用户可以直接在插件配置表单里填验证码，插件检测到后自动提交。

安全说明：
  * 账号密码只来自插件配置（用户主动填写），本模块不收集、不打印、不外传；
  * 待提交状态里只有会话 Cookie 与表单字段，**不包含密码**；
  * 落盘文件位于 data/plugin_data/<插件名>/，项目内所有持久化都集中在此处。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin

import httpx

from . import constants as C
from .api_client import GaijinMarketClient, MarketApiError, RefreshTokenInvalidError

# ---------------------------------------------------------------- 表单解析
_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.S | re.I)
_FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.I)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']?([^"'>\s]*)["']?""", re.I)
_RT_RE = re.compile(r"refresh_token=([A-Za-z0-9._\-]{8,})")
#: 可能的验证码输入框名，按优先级排列
_CODE_FIELDS = ("code", "otp", "totp", "twofa_code", "sms_code", "pin")

#: 待提交登录状态的有效期（秒）
PENDING_LOGIN_TTL = 900
#: SSO 登录页地址模板
_LOGIN_URL = (
    f"{C.LOGIN_BASE}{C.SSO_LOGIN_PATH}?return_url={C.SSO_RETURN_URL}&public_key={C.SSO_PUBLIC_KEY}&refresh_token=1"
)


# ---------------------------------------------------------------- JWT 工具
def parse_jwt_exp(token: str) -> float:
    """从 JWT 负载中读出 exp（不做签名校验，仅用于本地过期判断）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # 补齐 base64 padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp") or 0)
    except Exception:
        return 0.0


def describe_jwt(token: str) -> dict:
    """解析 JWT 中的非敏感字段，用于展示与排障。"""
    info: dict = {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        info["nick"] = data.get("nick")
        info["uid"] = data.get("uid")
        info["exp"] = data.get("exp")
        info["iat"] = data.get("iat")
        info["has_2step"] = "2step" in str(data.get("tgs") or "")
    except Exception:
        pass
    return info


def extract_refresh_token(text: str) -> str:
    """从回跳 URL / 响应正文中抓出 refresh_token。"""
    if not text:
        return ""
    matched = _RT_RE.search(unescape(str(text)))
    return matched.group(1) if matched else ""


# ---------------------------------------------------------------- Cookie 工具
def dump_cookies(client: httpx.AsyncClient) -> list[dict]:
    """导出会话 Cookie（含 domain / path，允许重名）。

    【坑】不能用 dict(client.cookies.items())：
      httpx.Cookies 是 MutableMapping，items() 会按名字回查 __getitem__，
      而 gaijin 会下发多个同名 Cookie（identity_sid 分属 .gaijin.net 与 login.gaijin.net），
      此时 httpx 会抛 CookieConflict("Multiple cookies exist with name=...")。
      正确做法是直接遍历底层 CookieJar。
    """
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
        }
        for cookie in client.cookies.jar
    ]


def load_cookies(client: httpx.AsyncClient, cookies) -> None:
    """把导出的 Cookie 写回客户端；domain / path 会一并保留，因此重名 Cookie 不丢。"""
    if isinstance(cookies, dict):  # 兼容早期版本存下来的 {name: value} 格式
        cookies = [{"name": k, "value": v, "domain": "", "path": "/"} for k, v in cookies.items()]
    for item in cookies or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        client.cookies.set(
            str(item["name"]),
            str(item.get("value", "")),
            str(item.get("domain", "")),
            str(item.get("path", "/")),
        )


# ---------------------------------------------------------------- 表单工具
def _parse_login_form(html: str) -> tuple[str, dict[str, str]]:
    """解析页面里第一个表单，返回 (action, 全部 input 字段)。"""
    block = _FORM_RE.search(html or "")
    action = ""
    scope = html or ""
    if block:
        scope = block.group(1)
        tag = _FORM_TAG_RE.search(block.group(0))
        if tag:
            attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag.group(0))}
            action = unescape(attrs.get("action", ""))

    fields: dict[str, str] = {}
    for raw_input in _INPUT_RE.findall(scope):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(raw_input)}
        name = attrs.get("name")
        if not name:
            continue
        fields[name] = unescape(attrs.get("value", ""))
    return action, fields


def _find_code_field(html: str) -> str:
    """在 2FA 页面里找出验证码输入框的字段名。"""
    for raw_input in _INPUT_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(raw_input)}
        name = (attrs.get("name") or "").lower()
        if not name or attrs.get("type", "").lower() == "hidden":
            continue
        if name in _CODE_FIELDS:
            return attrs["name"]
    for raw_input in _INPUT_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(raw_input)}
        if attrs.get("type", "").lower() in ("tel", "number", "text") and attrs.get("name"):
            return attrs["name"]
    # 没找到就返回空串 —— 调用方据此判断"这根本不是验证码页面"。
    # （早期返回默认值 "code" 会把"密码错误重登页"误判成验证码页，导致向用户索取不存在的验证码）
    return ""


# ---------------------------------------------------------------- 异常
class LoginError(Exception):
    """登录流程出错（凭据错误、站点改版、验证码过期等）。"""


class LoginRequiredError(Exception):
    """没有任何可用凭据，需要用户先完成登录。"""


@dataclass
class LoginStep:
    """一次登录动作的结果。"""

    need_code: bool = False
    message: str = ""
    jwt_ready: bool = False


@dataclass
class PendingLogin:
    """等待提交验证码的中间状态（会落盘，故只存必要信息）。"""

    #: Cookie 列表（不是 dict —— 需要保留 domain/path 以容纳重名 Cookie）
    cookies: list[dict] = field(default_factory=list)
    submit_url: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    code_field: str = "code"
    created_at: float = field(default_factory=time.time)

    def expired(self, ttl: float = PENDING_LOGIN_TTL) -> bool:
        return (time.time() - self.created_at) > ttl

    def to_dict(self) -> dict:
        return {
            "cookies": self.cookies,
            "submit_url": self.submit_url,
            "fields": self.fields,
            "code_field": self.code_field,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PendingLogin | None:
        if not isinstance(data, dict) or not data.get("submit_url"):
            return None
        raw_cookies = data.get("cookies") or []
        if isinstance(raw_cookies, dict):  # 兼容旧格式
            raw_cookies = [{"name": k, "value": v, "domain": "", "path": "/"} for k, v in raw_cookies.items()]
        return cls(
            cookies=[c for c in raw_cookies if isinstance(c, dict) and c.get("name")],
            submit_url=str(data.get("submit_url") or ""),
            fields=dict(data.get("fields") or {}),
            code_field=str(data.get("code_field") or "code"),
            created_at=float(data.get("created_at") or 0),
        )


# ---------------------------------------------------------------- 管理器
class AuthManager:
    """持有并维护市场令牌，并提供两阶段登录。"""

    def __init__(self, client: GaijinMarketClient, storage, config, logger) -> None:
        self.client = client
        self.storage = storage
        self.config = config
        self.logger = logger
        self._jwt: str = ""
        self._refresh_token: str = ""
        self._last_refresh_error: str = ""

    # ------------------------------------------------------------ 初始化

    def bootstrap(self) -> None:
        """合并「配置文件」与「落盘状态」；落盘的一定是插件刷新出来的最新值。"""
        state = self.storage.get_auth_state()
        self._jwt = self.config.jwt_token or ""
        self._refresh_token = self.config.refresh_token or ""

        stored_jwt = str(state.get("token") or "")
        stored_rt = str(state.get("refresh_token") or "")
        stored_ts = float(state.get("token_updated_at") or state.get("refresh_updated_at") or 0)

        if stored_jwt and stored_ts > 0:
            self._jwt = stored_jwt
        if stored_rt:
            self._refresh_token = stored_rt

    def absorb(self, jwt: str = "", refresh_token: str = "") -> None:
        """吸收新令牌并落盘（只写令牌，不写密码）。"""
        if jwt:
            self._jwt = jwt
        if refresh_token:
            self._refresh_token = refresh_token
        self.storage.set_auth_state(self._jwt if jwt else "", self._refresh_token if refresh_token else "")

    # ------------------------------------------------------------ 状态

    @property
    def jwt(self) -> str:
        return self._jwt

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def token_expire_at(self) -> float:
        return parse_jwt_exp(self._jwt) if self._jwt else 0.0

    def token_seconds_left(self) -> float:
        expire = self.token_expire_at()
        return (expire - time.time()) if expire else 0.0

    def is_token_valid(self, margin: int = 120) -> bool:
        """带裕量的有效性判断（默认提前 2 分钟视为过期）。"""
        return bool(self._jwt) and self.token_seconds_left() > margin

    def status(self) -> dict:
        info = describe_jwt(self._jwt) if self._jwt else {}
        pending = self.get_pending_login()
        return {
            "has_jwt": bool(self._jwt),
            "has_refresh_token": bool(self._refresh_token),
            "expire_at": self.token_expire_at(),
            "seconds_left": self.token_seconds_left(),
            "nick": info.get("nick"),
            "uid": info.get("uid"),
            "two_step": info.get("has_2step"),
            "last_error": self._last_refresh_error,
            "has_account": bool(self.config.account_email and self.config.account_password),
            "pending_login": pending is not None and not pending.expired(),
            "pending_code_field": pending.code_field if pending else "",
            "pending_age": (time.time() - pending.created_at) if pending else 0.0,
        }

    # ------------------------------------------------------------ 取用

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """获取一个可用令牌；必要时自动走方案三续期。"""
        if not force_refresh and self.is_token_valid():
            return self._jwt

        await self.try_refresh()

        if self._jwt:
            return self._jwt
        raise LoginRequiredError("没有可用的市场令牌，且 refresh_token 续期失败。")

    async def try_refresh(self) -> bool:
        """尝试用 refresh_token 静默续期，成功返回 True（不抛异常）。"""
        if not self._refresh_token:
            self._last_refresh_error = "未配置 refresh_token"
            return False
        try:
            await self.refresh()
            return True
        except (RefreshTokenInvalidError, MarketApiError) as exc:
            self._last_refresh_error = str(exc)
            self.logger.warning(f"方案三（refresh_token 续期）失败: {exc}")
        except Exception as exc:  # 网络抖动不应导致插件崩溃
            self._last_refresh_error = str(exc)
            self.logger.warning(f"refresh_token 续期异常: {exc}")
        return False

    async def refresh(self) -> str:
        """方案三：静默换取新 JWT。"""
        if not self._refresh_token:
            raise RefreshTokenInvalidError("cln_authorize_by_refresh_token", "NO_REFRESH_TOKEN")
        jwt = await self.client.authorize_by_refresh_token(self._refresh_token)
        self.absorb(jwt=jwt)
        self._last_refresh_error = ""
        self.logger.info("已用 refresh_token 换取新的市场令牌。")
        return jwt

    async def verify(self) -> dict:
        """校验当前令牌并返回账号信息。"""
        token = await self.get_token()
        return await self.client.check_auth(token)

    # ------------------------------------------------------------ 两阶段登录

    def get_pending_login(self) -> PendingLogin | None:
        """读取待提交的登录状态（超过 TTL 视为无效并清理）。"""
        pending = PendingLogin.from_dict(self.storage.get_pending_login())
        if pending is None:
            return None
        if pending.expired():
            self.storage.clear_pending_login()
            return None
        return pending

    @property
    def has_pending_login(self) -> bool:
        return self.get_pending_login() is not None

    async def begin_login(self, email: str, password: str) -> LoginStep:
        """第一阶段：提交账号密码。

        返回值有两种可能：
          * need_code=False -> 站点直接给了 refresh_token，登录已完成；
          * need_code=True  -> 站点要求验证码，中间状态已落盘，等待 submit_code()。
        """
        email, password = str(email or "").strip(), str(password or "")
        if not email or not password:
            raise LoginError("请先在插件配置里填写账号邮箱与密码。")

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.client.timeout),
            follow_redirects=False,  # 必须手动跟，才能截获回跳 URL 里的 refresh_token
            headers={"User-Agent": C.USER_AGENT},
        ) as browser:
            response = await browser.get(_LOGIN_URL)
            action, fields = _parse_login_form(response.text)
            submit_url = urljoin(_LOGIN_URL, action or C.SSO_LOGIN_PATH)

            payload = dict(fields)
            payload["login"] = email
            payload["password"] = password
            response = await browser.post(submit_url, data=payload, headers={"Referer": _LOGIN_URL})

            # 跟着 3xx 一路走，直到拿到 refresh_token 或停在验证码页面
            for _ in range(5):
                token = self._grab_refresh_token(response)
                if token:
                    await self._finish_login(token)
                    return LoginStep(
                        need_code=False,
                        jwt_ready=True,
                        message="登录成功，已获取 refresh_token 并换取新的 JWT。",
                    )
                if response.status_code >= 300 or not (response.text or "").strip():
                    location = response.headers.get("location") or ""
                    if not location:
                        break
                    response = await browser.get(urljoin(submit_url, location), headers={"Referer": _LOGIN_URL})
                    continue
                break

            # 落在哪一页？必须区分"验证码页"与"被退回的登录页"：
            # 账号密码错误时站点会重新渲染登录页，若把它当成验证码页，
            # 就会向用户索取一个根本不存在的验证码，形成死循环。
            _, code_form_fields = _parse_login_form(response.text)
            if "password" in code_form_fields:
                raise LoginError(
                    "登录被拒：站点重新返回了登录页，通常是账号或密码不正确。"
                    "请核对插件配置里的 account_email / account_password 后重试。"
                )

            code_field = _find_code_field(response.text)
            if not code_field:
                raise LoginError(
                    "登录未能完成：站点返回的页面既没有验证码输入框，也没有下发 refresh_token。"
                    "可能是站点改版或触发了风控，请稍后重试或改用 refresh_token 导入。"
                )
            self.storage.set_pending_login(
                PendingLogin(
                    cookies=dump_cookies(browser),
                    submit_url=str(response.url) or submit_url,
                    fields=code_form_fields,
                    code_field=code_field,
                ).to_dict()
            )
            self.logger.info(f"SSO 登录需要验证码，等待提交（字段 {code_field}）。")
            return LoginStep(
                need_code=True,
                message=(
                    "账号密码已提交，站点要求输入验证码。\n"
                    f"验证码字段：{code_field}\n"
                    "请在下方输入验证码并提交（或在插件配置里填写 twofa_code 后保存）。"
                ),
            )

    async def submit_code(self, code: str) -> LoginStep:
        """第二阶段：提交验证码，完成登录。"""
        pending = self.get_pending_login()
        if pending is None:
            raise LoginError("没有等待中的登录流程（可能已超时 15 分钟），请重新点击「开始登录」。")

        code = str(code or "").strip()
        if not code:
            raise LoginError("验证码不能为空。")

        payload = dict(pending.fields)
        payload[pending.code_field] = code

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.client.timeout),
            follow_redirects=False,
            headers={"User-Agent": C.USER_AGENT},
        ) as browser:
            # 恢复第一次提交时建立的会话 Cookie —— 提交验证码必须带上它
            load_cookies(browser, pending.cookies)

            response = await browser.post(pending.submit_url, data=payload, headers={"Referer": _LOGIN_URL})
            for _ in range(5):
                token = self._grab_refresh_token(response)
                if token:
                    self.storage.clear_pending_login()
                    await self._finish_login(token)
                    return LoginStep(
                        need_code=False,
                        jwt_ready=True,
                        message="验证码校验通过，登录成功，已换取新的 JWT。",
                    )
                if response.status_code >= 300:
                    location = response.headers.get("location") or ""
                    if not location:
                        break
                    response = await browser.get(urljoin(pending.submit_url, location), headers={"Referer": _LOGIN_URL})
                    continue
                break

        raise LoginError("验证码未通过或站点未下发 refresh_token。请确认验证码是否正确、是否已过期后重试。")

    # ------------------------------------------------------------ 内部

    async def _finish_login(self, refresh_token: str) -> None:
        """登录成功后统一收尾：吸收 RT，并立即用它换取 JWT。"""
        self.absorb(refresh_token=refresh_token)
        try:
            await self.refresh()
        except Exception as exc:  # 换取失败不算登录失败，下次轮询会再试
            self._last_refresh_error = str(exc)
            self.logger.warning(f"登录成功但换取 JWT 失败，稍后自动重试: {exc}")

    @staticmethod
    def _grab_refresh_token(response: httpx.Response) -> str:
        """从响应头 / 最终 URL / 正文三处寻找 refresh_token。"""
        token = extract_refresh_token(response.headers.get("location") or "")
        if token:
            return token
        token = extract_refresh_token(str(response.url))
        if token:
            return token
        if response.status_code == 200:
            token = extract_refresh_token(response.text)
            if token:
                return token
        return ""
