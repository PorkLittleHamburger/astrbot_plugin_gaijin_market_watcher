"""用 Pillow 生成轻量价格折线图，避免引入 Matplotlib 等大型依赖。"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 680
BACKGROUND = "#F7F9FC"
PANEL = "#FFFFFF"
TEXT = "#1F2937"
MUTED = "#6B7280"
GRID = "#E5E7EB"
SELL = "#E67E22"
BUY = "#2563EB"

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_price_chart(display_name: str, points: list[dict[str, Any]]) -> bytes:
    """把最近若干次行情绘制为 PNG；数据不足时仍绘制可用的点。"""
    clean = []
    for point in points:
        if not isinstance(point, dict):
            continue
        try:
            timestamp = float(point.get("t") or point.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        sell = _number(point.get("sell"))
        buy = _number(point.get("buy"))
        if timestamp > 0 and (sell is not None or buy is not None):
            clean.append({"t": timestamp, "sell": sell, "buy": buy})
    clean.sort(key=lambda item: item["t"])
    if not clean:
        raise ValueError("没有可绘制价格")

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = _font(30)
    label_font = _font(18)
    small_font = _font(15)

    draw.rounded_rectangle((24, 20, WIDTH - 24, HEIGHT - 20), radius=22, fill=PANEL)
    title = f"{display_name or '物品'} · 最近 {len(clean)} 次行情"
    draw.text((58, 44), title, fill=TEXT, font=title_font)

    legend_y = 92
    draw.line((60, legend_y + 10, 100, legend_y + 10), fill=SELL, width=5)
    draw.ellipse((76, legend_y + 6, 84, legend_y + 14), fill=SELL)
    draw.text((110, legend_y), "最低售价", fill=TEXT, font=label_font)
    draw.line((235, legend_y + 10, 275, legend_y + 10), fill=BUY, width=5)
    draw.ellipse((251, legend_y + 6, 259, legend_y + 14), fill=BUY)
    draw.text((285, legend_y), "最高求购", fill=TEXT, font=label_font)

    left, top, right, bottom = 105, 145, WIDTH - 62, HEIGHT - 115
    values = [value for point in clean for value in (point["sell"], point["buy"]) if value is not None]
    value_min, value_max = min(values), max(values)
    padding = max(1.0, abs(value_max) * 0.08) if abs(value_max - value_min) < 1e-9 else (value_max - value_min) * 0.12
    y_min, y_max = value_min - padding, value_max + padding

    for index in range(6):
        ratio = index / 5
        y = bottom - ratio * (bottom - top)
        value = y_min + ratio * (y_max - y_min)
        draw.line((left, y, right, y), fill=GRID, width=1)
        label = f"{value:.2f}"
        bbox = draw.textbbox((0, 0), label, font=small_font)
        draw.text((left - 14 - (bbox[2] - bbox[0]), y - 9), label, fill=MUTED, font=small_font)

    timestamps = [point["t"] for point in clean]
    time_min, time_max = min(timestamps), max(timestamps)

    def x_at(timestamp: float) -> float:
        if abs(time_max - time_min) < 1e-9:
            return (left + right) / 2
        return left + (timestamp - time_min) / (time_max - time_min) * (right - left)

    def y_at(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    for key, color in (("sell", SELL), ("buy", BUY)):
        series = [(x_at(point["t"]), y_at(point[key])) for point in clean if point[key] is not None]
        if len(series) > 1:
            draw.line(series, fill=color, width=4, joint="curve")
        for x, y in series:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color, outline=PANEL, width=2)

    label_indexes = sorted(set([0, len(clean) - 1] if len(clean) > 1 else [0]))
    for index in label_indexes:
        timestamp = clean[index]["t"]
        x = x_at(timestamp)
        label = time.strftime("%H:%M:%S", time.localtime(timestamp))
        bbox = draw.textbbox((0, 0), label, font=small_font)
        draw.text((x - (bbox[2] - bbox[0]) / 2, bottom + 18), label, fill=MUTED, font=small_font)

    draw.line((left, bottom, right, bottom), fill="#9CA3AF", width=2)
    draw.line((left, top, left, bottom), fill="#9CA3AF", width=2)
    draw.text((left, HEIGHT - 66), "时间", fill=MUTED, font=small_font)
    draw.text((right - 48, top - 28), "GJN", fill=MUTED, font=small_font)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
