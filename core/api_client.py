"""网络层：本项目唯一允许发起 HTTP 请求的地方。

接口形态（已实测）：

    POST https://market-proxy.gaijin.net/web
    Content-Type: application/x-www-form-urlencoded
    body: action=cln_books_brief&token=<JWT>&appid=1067&market_name=<slug>&language=zh_CN

    -> {"response": {"BUY": [[价, 量], ...降序],
                     "SELL": [[价, 量], ...升序],
                     "depth": {"BUY": 挂单总量, "SELL": 挂单总量},
                     "success": true, "type": "COMMODITY"}}

价格刻度：盘口原始值 / 10000 = GJN（例 BUY[0]=380000 -> 38.00 GJN）。

设计要点：
  * 客户端本身「不持有」令牌，令牌一律由调用方显式传入 —— 避免网络层与鉴权层耦合；
  * 统一限速（同一时刻两次请求之间至少间隔 MIN_REQUEST_INTERVAL 秒），温柔待人；
  * 传输层错误自动重试，接口层错误（success=false）不重试、直接抛出。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .constants import (
    ACT_AUTH_REFRESH,
    ACT_BOOKS_BRIEF,
    ACT_CHECK_AUTH,
    ACT_MARKET_SEARCH,
    BOOK_PRICE_SCALE,
    DEFAULT_APPID,
    DEFAULT_LANGUAGE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    EP_MARKET,
    EP_TRADE,
    ERR_BAD_REFRESH_TOKEN,
    ERR_INVALID_TOKEN,
    ERR_TOKEN_REQUIRED,
    MIN_REQUEST_INTERVAL,
    SEARCH_OPTIONS_SELL,
    USER_AGENT,
)
from .models import Quote


class MarketApiError(Exception):
    """市场接口业务错误（response.success == false）。"""

    def __init__(self, action: str, error: str, detail: str = "") -> None:
        self.action = action
        self.error = error
        self.detail = detail or error
        super().__init__(f"[{action}] {self.detail}")


class AuthExpiredError(MarketApiError):
    """令牌缺失或失效，需要刷新 / 重新登录。"""


class RefreshTokenInvalidError(MarketApiError):
    """refresh_token 已失效，需要降级为账号密码登录。"""


class TransportError(MarketApiError):
    """网络层错误（超时、5xx、非 JSON 响应等）。"""


class GaijinMarketClient:
    """异步 HTTP 客户端。使用 httpx（官方要求：插件禁用 requests）。"""

    def __init__(
        self,
        *,
        language: str = DEFAULT_LANGUAGE,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        min_interval: float = MIN_REQUEST_INTERVAL,
        logger=None,
    ) -> None:
        self.language = language
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.min_interval = max(0.0, float(min_interval))
        self.logger = logger

        self._client: httpx.AsyncClient | None = None
        self._throttle_lock = asyncio.Lock()
        self._last_request_ts = 0.0

    # ------------------------------------------------------------ 生命周期

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://trade.gaijin.net/",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------ 底层请求

    async def _throttle(self) -> None:
        """全局限速：保证两次请求之间至少间隔 min_interval 秒。"""
        if self.min_interval <= 0:
            return
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.min_interval - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    @staticmethod
    def _unwrap(action: str, payload: dict) -> dict:
        """拆掉 {"response": {...}} 外壳，并把 success=false 映射成异常。"""
        body = None
        if isinstance(payload, dict):
            # 不同接口的外壳不一致，可能是 response / result / 裸对象
            for key in ("response", "result"):
                if isinstance(payload.get(key), dict):
                    body = payload[key]
                    break
            if body is None:
                body = payload
        else:
            body = {}
        if body.get("success") is False:
            error = str(body.get("error") or "UNKNOWN_ERROR")
            if error in (ERR_TOKEN_REQUIRED, ERR_INVALID_TOKEN):
                raise AuthExpiredError(action, error)
            if error == ERR_BAD_REFRESH_TOKEN:
                raise RefreshTokenInvalidError(action, error)
            raise MarketApiError(action, error)
        return body

    async def _post(self, endpoint: str, action: str, params: dict, *, with_token: str | None) -> dict:
        """发送一次带重试的表单 POST。"""
        payload: dict[str, Any] = {"action": action, "language": self.language}
        if with_token:
            payload["token"] = with_token
        payload.update({k: v for k, v in params.items() if v is not None})

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            await self._throttle()
            try:
                client = await self._get_client()
                response = await client.post(endpoint, data=payload)
                if response.status_code >= 500:
                    raise TransportError(action, f"HTTP {response.status_code}")
                return self._unwrap(action, response.json())
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                # 线性退避后重试
                await asyncio.sleep(0.8 * (attempt + 1))
        raise TransportError(action, f"请求失败: {last_error}")

    # ------------------------------------------------------------ 业务方法

    async def fetch_quote(
        self,
        market_name: str,
        token: str,
        *,
        appid: str = DEFAULT_APPID,
        display_name: str = "",
    ) -> Quote:
        """抓取某个物品的实时行情快照。"""
        body = await self._post(
            EP_TRADE,
            ACT_BOOKS_BRIEF,
            {"appid": appid, "market_name": market_name},
            with_token=token,
        )
        sell_levels = self._levels(body.get("SELL"))
        buy_levels = self._levels(body.get("BUY"))
        depth = body.get("depth") or {}
        return Quote(
            market_name=market_name,
            # SELL 升序 => 首项即最低售价；BUY 降序 => 首项即最高求购价
            sell_min=sell_levels[0][0] if sell_levels else None,
            buy_max=buy_levels[0][0] if buy_levels else None,
            sell_depth=int(depth.get("SELL") or 0),
            buy_depth=int(depth.get("BUY") or 0),
            display_name=display_name,
            app_id=appid,
            kind=str(body.get("type") or ""),
        )

    @staticmethod
    def _levels(raw: Any) -> list[tuple[float, int]]:
        """把 [[原始价, 数量], ...] 归一化为 [(GJN 价, 数量), ...]。"""
        levels: list[tuple[float, int]] = []
        for entry in raw or []:
            try:
                levels.append((float(entry[0]) / BOOK_PRICE_SCALE, int(entry[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return levels

    async def search_items(
        self,
        text: str,
        token: str,
        *,
        appid: str = DEFAULT_APPID,
        limit: int = 20,
    ) -> list[dict]:
        """模糊搜索物品，返回原始 asset 列表。"""
        body = await self._post(
            EP_TRADE,
            ACT_MARKET_SEARCH,
            {
                "skip": 0,
                "count": limit,
                "text": text,
                "options": SEARCH_OPTIONS_SELL,
                "appid_filter": appid,
            },
            with_token=token,
        )
        assets = body.get("assets")
        return assets if isinstance(assets, list) else []

    async def check_auth(self, token: str) -> dict:
        """校验令牌并取回账号信息（不包含任何敏感凭据）。"""
        return await self._post(EP_MARKET, ACT_CHECK_AUTH, {}, with_token=token)

    async def authorize_by_refresh_token(self, refresh_token: str) -> str:
        """用 refresh_token 换取新的市场 JWT（方案三：无密码续期）。"""
        body = await self._post(
            EP_MARKET,
            ACT_AUTH_REFRESH,
            {"refresh_token": refresh_token},
            with_token=None,
        )
        jwt = body.get("jwt") or body.get("token")
        if not jwt:
            raise MarketApiError(ACT_AUTH_REFRESH, "NO_JWT", "接口未返回 jwt")
        return str(jwt)
