"""配置文件监听：让「在 WebUI 配置界面里改动」立即生效。

为什么需要它：
  AstrBot 保存插件配置后**是否重载插件**取决于版本与保存路径，
  依赖这个行为会导致"在配置界面填了验证码却没反应"这类诡异问题。
  插件自己盯着配置文件（每秒级、只在内容变化时才动作）就能彻底消除该不确定性。

设计：
  * 只读监听，不写文件；写入始终由 AstrBot 的 AstrBotConfig.save_config() 负责；
  * 用"内容快照比较"判断变化，而不是只看 mtime，避免自己保存后自我触发；
  * 解析失败（写入中途读到半个 JSON）直接跳过，下个周期会重试。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path


class ConfigFileWatcher:
    """轮询监听一个 JSON 配置文件，内容变化时回调。"""

    def __init__(
        self,
        path: Path,
        on_change: Callable[[dict], Awaitable[None] | None],
        logger,
        interval: float = 3.0,
    ) -> None:
        self.path = Path(path)
        self.on_change = on_change
        self.logger = logger
        self.interval = max(0.5, float(interval))
        self._task: asyncio.Task | None = None
        self._running = False
        #: 最近一次"已应用"的配置内容，用来判断是否真的变了
        self._applied: dict | None = None

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """启动监听（幂等）。若当前没有运行中的事件循环，则安全返回 False。"""
        if self.running:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.logger.warning("当前没有运行中的事件循环，配置热监听未启动。")
            return False
        # 先记下当前内容，避免启动瞬间误判为"刚发生变化"
        self._applied = self._read()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="gaijin-market-config-watcher")
        self.logger.info(f"配置热监听已启动：{self.path.name}")
        return True

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def mark_applied(self, data: dict | None) -> None:
        """插件自己保存配置后调用，避免下一轮被自己触发。"""
        self._applied = dict(data) if isinstance(data, dict) else None

    # ------------------------------------------------------------ 内部

    def _read(self) -> dict | None:
        try:
            if not self.path.exists():
                return None
            # 注意：必须用 utf-8-sig —— AstrBot 保存插件配置时会写入 UTF-8 BOM，
            # 用普通 utf-8 读会直接 JSONDecodeError。
            with self.path.open("r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            # 可能正好读到写入中途，下个周期重试即可
            return None
        except Exception as exc:
            self.logger.warning(f"读取插件配置文件失败: {exc}")
            return None

    async def _loop(self) -> None:
        while self._running:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.sleep(self.interval), timeout=self.interval + 1)
            if not self._running:
                break

            data = self._read()
            if data is None or data == self._applied:
                continue

            self._applied = data
            try:
                result = self.on_change(data)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 回调异常不允许终结监听循环
                self.logger.error(f"应用新配置失败: {exc}")
