"""商品页快照：用本地 Playwright 打开 Gaijin 商品页并截图。

登录态复用插件自己维护的 JWT（注入 localStorage 的 ``MarketApp,auth,tokenPair``），
因此不需要密码、也不需要重新登录。

设计原则：**快照永远是附属品**。任何一步失败（Playwright 缺失 / 站点改版 /
网络抖动）都只让这一张图消失，绝不抛出到推送链路、绝不影响文字消息。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

LOCAL_STORAGE_KEY = "MarketApp,auth,tokenPair"
COOKIE_BUTTON_TEXTS = ("Accept all", "Accept All", "接受全部", "Принять все")
ORDER_BOOK_SELECTORS = (".commodityOrders", ".ordersTable", ".commodity")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1440, "height": 1000}
REMOVE_BANNER_JS = """
() => {
  document.querySelectorAll('div,section').forEach((el) => {
    const t = el.innerText || '';
    if (/About cookies on this site/i.test(t) && el.offsetHeight < 700) el.remove();
  });
}
"""

SNAPSHOT_MODES = ("off", "on_change", "on_trigger")


class SnapshotUnavailable(RuntimeError):
    """Playwright 不可用（未安装或浏览器缺失）。"""


def playwright_available() -> bool:
    """探测 Playwright 是否可用（只探测，不启动浏览器）。"""
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


class PageSnapshotter:
    """把一个 URL 截成图片。同一时刻只允许一个浏览器实例。"""

    def __init__(
        self,
        out_dir: Path | str,
        *,
        viewport: dict[str, int] | None = None,
        scale: float = 1.0,
        timeout_ms: int = 45000,
        settle_ms: int = 2500,
        keep_files: int = 20,
        logger: Any = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)
        self.scale = max(1.0, float(scale))
        self.timeout_ms = int(timeout_ms)
        self.settle_ms = int(settle_ms)
        self.keep_files = max(1, int(keep_files))
        self.logger = logger or logging.getLogger("astrbot")
        self._lock = asyncio.Semaphore(1)

    async def capture(self, url: str, jwt: str, *, tag: str = "item") -> Path:
        """截图并返回本地路径；失败抛异常（由调用方决定是否降级）。"""
        if not jwt:
            raise SnapshotUnavailable("缺少 JWT，无法注入登录态")
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            raise SnapshotUnavailable(f"Playwright 不可用：{exc}") from exc

        self.out_dir.mkdir(parents=True, exist_ok=True)
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40] or "item"
        target = self.out_dir / f"gjm_snapshot_{safe_tag}_{int(time.time())}.png"
        init_script = (
            "try{localStorage.setItem("
            + json.dumps(LOCAL_STORAGE_KEY)
            + ", JSON.stringify({token: "
            + json.dumps(jwt)
            + "}));}catch(e){}"
        )

        async with self._lock, async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport=self.viewport,
                    device_scale_factor=self.scale,
                    user_agent=USER_AGENT,
                    locale="zh-CN",
                )
                # 关键一步：在页面脚本执行前把 JWT 写进 localStorage
                await context.add_init_script(init_script)
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                await self._dismiss_cookies(page)
                await self._wait_ready(page)
                await page.screenshot(path=str(target), full_page=True)
            finally:
                with contextlib.suppress(Exception):
                    await browser.close()

        self._prune()
        return target

    async def _dismiss_cookies(self, page: Any) -> None:
        for text in COOKIE_BUTTON_TEXTS:
            with contextlib.suppress(Exception):
                button = page.get_by_role("button", name=text, exact=False).first
                if await button.count():
                    await button.click(timeout=2500)
                    return
        with contextlib.suppress(Exception):
            await page.evaluate(REMOVE_BANNER_JS)

    async def _wait_ready(self, page: Any) -> None:
        """等盘口渲染出来；等不到也照样截（可能是未登录或改版）。"""
        for selector in ORDER_BOOK_SELECTORS:
            try:
                await page.wait_for_selector(selector, timeout=self.settle_ms)
                break
            except Exception:
                continue
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(self.settle_ms)

    def _prune(self) -> None:
        with contextlib.suppress(Exception):
            files = sorted(
                (p for p in self.out_dir.glob("gjm_snapshot_*.png") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            for stale in files[: max(0, len(files) - self.keep_files)]:
                with contextlib.suppress(Exception):
                    stale.unlink()


class SnapshotDispatcher:
    """按配置决定「是否 / 何时」截图，并把图发到对应会话。

    截图放后台任务，因此不会拖慢文字推送：文字（含 @）先到，图片约十几秒后跟上。
    """

    def __init__(
        self,
        *,
        context: Any,
        config: Any,
        storage: Any,
        jwt_provider: Callable[[], Awaitable[str]],
        appid_provider: Callable[[], str],
        data_dir: Path | str,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.config = config
        self.storage = storage
        self.jwt_provider = jwt_provider
        self.appid_provider = appid_provider
        self.logger = logger or logging.getLogger("astrbot")
        self.snapshotter = PageSnapshotter(Path(data_dir) / "snapshots", logger=self.logger)
        self._cooldown_until: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._unavailable_noted = False

    # ------------------------------------------------------------ 对外

    def _conf(self) -> Any:
        """取当前配置（支持传入 callable，避免配置热重载后用到旧对象）。"""
        config = self.config
        return config() if callable(config) else config

    def should_capture(self, umo: str, market_name: str, *, triggered: bool = False) -> bool:
        """按配置判断本次是否要现拍（会占用冷却窗口）。"""
        config = self._conf()
        mode = getattr(config, "snapshot_mode", "off")
        if mode == "off" or not umo or not market_name:
            return False
        if mode == "on_trigger" and not triggered:
            return False
        now = time.time()
        if now < self._cooldown_until.get(umo, 0.0):
            return False
        cooldown = max(0, int(getattr(config, "snapshot_cooldown", 0) or 0))
        self._cooldown_until[umo] = now + cooldown
        return True

    async def snapshot(self, market_name: str) -> Path | None:
        """现拍一张并返回路径（失败返回 None，只记日志）。"""
        try:
            return await self.capture(market_name)
        except SnapshotUnavailable as exc:
            if not self._unavailable_noted:
                self._unavailable_noted = True
                self.logger.warning(f"[GJM] 网页快照不可用，之后不再尝试：{exc}")
        except Exception as exc:
            self.logger.warning(f"[GJM] 网页快照失败（{market_name}）：{exc}")
        return None

    async def __call__(self, umo: str, market_name: str, *, triggered: bool = False) -> str:
        """供 Notifier 使用：需要就现拍并返回图片路径，否则返回空串。"""
        if not self.should_capture(umo, market_name, triggered=triggered):
            return ""
        path = await self.snapshot(market_name)
        return str(path) if path else ""

    def page_url(self, market_name: str) -> str:
        return f"https://trade.gaijin.net/market/{self.appid_provider()}/{market_name}"

    def schedule(self, umo: str, market_names: list[str], *, triggered: bool = False) -> bool:
        """按配置决定是否为本轮推送配一张快照（非阻塞）。"""
        config = self._conf()
        mode = getattr(config, "snapshot_mode", "off")
        if mode == "off" or not umo or not market_names:
            return False
        if mode == "on_trigger" and not triggered:
            return False
        now = time.time()
        if now < self._cooldown_until.get(umo, 0.0):
            return False
        cooldown = max(0, int(getattr(config, "snapshot_cooldown", 0) or 0))
        self._cooldown_until[umo] = now + cooldown

        market_name = market_names[0]
        try:
            task = asyncio.create_task(self._capture_and_send(umo, market_name))
        except RuntimeError as exc:  # 没有运行中的事件循环（例如被同步调用）
            self.logger.debug(f"[GJM] 当前上下文无法调度网页快照任务：{exc}")
            return False
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def capture(self, market_name: str) -> Path:
        """立即截一张（供手动指令使用），返回本地路径。"""
        jwt = await self.jwt_provider()
        return await self.snapshotter.capture(self.page_url(market_name), jwt, tag=market_name)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------ 内部

    async def _capture_and_send(self, umo: str, market_name: str) -> None:
        try:
            path = await self.capture(market_name)
            await self._send_image(umo, path, market_name)
        except asyncio.CancelledError:
            raise
        except SnapshotUnavailable as exc:
            if not self._unavailable_noted:
                self._unavailable_noted = True
                self.logger.warning(f"[GJM] 网页快照不可用，之后不再尝试：{exc}")
        except Exception as exc:
            self.logger.warning(f"[GJM] 网页快照失败（{market_name}）：{exc}")

    async def _send_image(self, umo: str, path: Path, market_name: str) -> bool:
        try:
            from astrbot.api.event import MessageChain
        except Exception as exc:
            self.logger.warning(f"[GJM] 图片组件不可用：{exc}")
            return False

        title = self.storage.get_item_name(market_name) or market_name
        try:
            chain = MessageChain()
            chain.message(f"{title} · 网页快照")
            chain.file_image(str(path))
        except Exception as exc:
            self.logger.warning(f"[GJM] 构造图片消息失败：{exc}")
            return False
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as exc:
            self.logger.warning(f"[GJM] 快照图片发送失败（{umo}）：{exc}")
            return False
