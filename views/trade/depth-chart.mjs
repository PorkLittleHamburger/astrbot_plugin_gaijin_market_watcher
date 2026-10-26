const SVG_NS = "http://www.w3.org/2000/svg";
const NUMBER = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

function numberText(value) {
  return NUMBER.format(Number(value || 0));
}

function priceText(value) {
  return Number(value || 0).toFixed(2);
}

function levelPrice(level) {
  if (typeof level?.price_raw === "number") return level.price_raw / 10000;
  return Number(level?.price || 0);
}

function levelRaw(level) {
  if (typeof level?.price_raw === "number") return level.price_raw;
  return Math.round(levelPrice(level) * 10000);
}

function levelQuantity(level) {
  const value = Number(level?.quantity ?? 0);
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function svgElement(name, attributes = {}, text = "") {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  if (text) node.textContent = text;
  return node;
}

export function niceTicks(min, max, count = 5) {
  const lo = Number(min || 0);
  const hi = Number(max || 0);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return [lo, hi];
  const rough = (hi - lo) / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(rough));
  const fraction = rough / power;
  const factor = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  const step = factor * power;
  const start = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let value = start; value <= hi + step * 0.001; value += step) ticks.push(Number(value.toFixed(10)));
  if (ticks[0] !== 0) ticks.unshift(0);
  if (ticks[ticks.length - 1] < hi) ticks.push(hi);
  return ticks;
}

function sampleTicks(values, limit = 11) {
  const ordered = [...values].sort((a, b) => a.x - b.x);
  if (ordered.length <= limit) return ordered;
  const result = [];
  for (let index = 0; index < limit; index += 1) {
    result.push(ordered[Math.round((index * (ordered.length - 1)) / (limit - 1))]);
  }
  return result.filter((item, index, array) => index === 0 || item.x !== array[index - 1].x);
}

function priceDomain(levels) {
  if (!levels.length) return null;
  return { min: Math.min(...levels.map((level) => level.price)), max: Math.max(...levels.map((level) => level.price)) };
}

function mapLinear(value, inMin, inMax, outMin, outMax) {
  if (inMax === inMin) return (outMin + outMax) / 2;
  return outMin + ((value - inMin) / (inMax - inMin)) * (outMax - outMin);
}

function seriesPath(points) {
  if (!points.length) return "";
  return points.map((point, index) => (index === 0 ? "M" : "L") + point.x.toFixed(2) + "," + point.y.toFixed(2)).join(" ");
}

export function buildDepthChartModel(book = {}, rule = {}, width = 960, height = 360) {
  const chartWidth = Math.max(360, Number(width) || 960);
  const chartHeight = Math.max(260, Number(height) || 360);
  const plot = { left: 58, right: chartWidth - 20, top: 24, bottom: chartHeight - 48 };
  plot.width = plot.right - plot.left;
  plot.height = plot.bottom - plot.top;

  const buys = (book?.buy_levels || [])
    .map((level) => ({ price: levelPrice(level), priceRaw: levelRaw(level), quantity: levelQuantity(level) }))
    .filter((level) => level.price > 0 && level.quantity > 0)
    .sort((a, b) => b.price - a.price);
  const sells = (book?.sell_levels || [])
    .map((level) => ({ price: levelPrice(level), priceRaw: levelRaw(level), quantity: levelQuantity(level) }))
    .filter((level) => level.price > 0 && level.quantity > 0)
    .sort((a, b) => a.price - b.price);

  const bestBuy = buys[0] || null;
  const bestSell = sells[0] || null;
  const buyTotal = buys.reduce((sum, level) => sum + level.quantity, 0);
  const sellTotal = sells.reduce((sum, level) => sum + level.quantity, 0);
  const crossed = Boolean(bestBuy && bestSell && bestBuy.price >= bestSell.price);
  const bothSides = Boolean(bestBuy && bestSell);
  const centerX = plot.left + plot.width / 2;
  const empty = !buys.length && !sells.length;

  let xForBuy = () => centerX;
  let xForSell = () => centerX;
  let centerPrice = null;
  let buyDomain = priceDomain(buys);
  let sellDomain = priceDomain(sells);
  const buyThresholdPrice = Number(rule?.buy_price || 0);
  const sellTargetPrice = Number(rule?.sell_gross_price || 0);

  if (bothSides && !crossed) {
    const minPrice = Math.min(buyDomain.min, sellDomain.min, buyThresholdPrice > 0 ? buyThresholdPrice : Infinity);
    const maxPrice = Math.max(buyDomain.max, sellDomain.max, sellTargetPrice > 0 ? sellTargetPrice : -Infinity);
    centerPrice = (bestBuy.price + bestSell.price) / 2;
    xForBuy = (price) => mapLinear(price, minPrice, maxPrice, plot.left, plot.right);
    xForSell = xForBuy;
    buyDomain = { min: minPrice, max: maxPrice };
    sellDomain = { min: minPrice, max: maxPrice };
  } else if (bothSides && crossed) {
    if (buyThresholdPrice > 0) buyDomain.min = Math.min(buyDomain.min, buyThresholdPrice);
    if (sellTargetPrice > 0) sellDomain.max = Math.max(sellDomain.max, sellTargetPrice);
    xForBuy = (price) => mapLinear(price, buyDomain.min, bestBuy.price, plot.left, centerX);
    xForSell = (price) => mapLinear(price, bestSell.price, sellDomain.max, centerX, plot.right);
  } else if (bestBuy) {
    xForBuy = (price) => mapLinear(price, buyDomain.min, buyDomain.max, plot.left, plot.right);
    centerPrice = bestBuy.price;
  } else if (bestSell) {
    xForSell = (price) => mapLinear(price, sellDomain.min, sellDomain.max, plot.left, plot.right);
    centerPrice = bestSell.price;
  }

  const maxDepth = Math.max(1, buyTotal, sellTotal);
  const yTicks = niceTicks(0, maxDepth, 5);
  const yMax = Math.max(maxDepth, yTicks[yTicks.length - 1] || maxDepth);
  const y = (quantity) => plot.bottom - (Number(quantity || 0) / yMax) * plot.height;

  const buyPoints = [];
  if (buys.length) {
    const cumulative = new Map();
    let running = 0;
    for (const level of buys) {
      running += level.quantity;
      cumulative.set(level.priceRaw, running);
    }
    for (const level of [...buys].reverse()) {
      const total = cumulative.get(level.priceRaw);
      buyPoints.push({
        side: "buy", price: level.price, priceRaw: level.priceRaw, quantity: level.quantity, cumulative: total,
        x: xForBuy(level.price), y: y(total),
      });
    }
    const endX = bothSides ? centerX : plot.right;
    buyPoints.push({ side: "buy", price: centerPrice, priceRaw: null, quantity: 0, cumulative: 0, x: endX, y: plot.bottom });
  }

  const sellPoints = [];
  if (sells.length) {
    const startX = bothSides ? centerX : plot.left;
    sellPoints.push({ side: "sell", price: centerPrice, priceRaw: null, quantity: 0, cumulative: 0, x: startX, y: plot.bottom });
    let running = 0;
    for (const level of sells) {
      running += level.quantity;
      sellPoints.push({
        side: "sell", price: level.price, priceRaw: level.priceRaw, quantity: level.quantity, cumulative: running,
        x: xForSell(level.price), y: y(running),
      });
    }
  }

  const buyStartX = buyPoints[0]?.x ?? centerX;
  const sellEndX = sellPoints[sellPoints.length - 1]?.x ?? centerX;
  const buyAreaPath = buyPoints.length
    ? "M" + buyStartX.toFixed(2) + "," + plot.bottom.toFixed(2) + " " + seriesPath(buyPoints).replace(/^M/, "L") + " L" + centerX.toFixed(2) + "," + plot.bottom.toFixed(2) + " Z"
    : "";
  const sellAreaPath = sellPoints.length
    ? "M" + centerX.toFixed(2) + "," + plot.bottom.toFixed(2) + " " + seriesPath(sellPoints).replace(/^M/, "L") + " L" + sellEndX.toFixed(2) + "," + plot.bottom.toFixed(2) + " Z"
    : "";

  const buyBestPoint = buyPoints.find((point) => point.priceRaw === bestBuy?.priceRaw) || null;
  const sellBestPoint = sellPoints.find((point) => point.priceRaw === bestSell?.priceRaw) || null;
  const buyThresholdX = buyThresholdPrice > 0 ? xForBuy(buyThresholdPrice) : null;
  const sellThresholdX = sellTargetPrice > 0 ? xForSell(sellTargetPrice) : null;
  const thresholdVisible = (x) => Number.isFinite(x) && x >= plot.left - 0.5 && x <= plot.right + 0.5;

  const priceTicks = [];
  for (const point of buyPoints.filter((point) => point.priceRaw !== null)) priceTicks.push({ x: point.x, label: priceText(point.price), side: "buy" });
  for (const point of sellPoints.filter((point) => point.priceRaw !== null)) priceTicks.push({ x: point.x, label: priceText(point.price), side: "sell" });
  if (bothSides && !crossed) priceTicks.push({ x: centerX, label: priceText(centerPrice), side: "mid" });
  const xTicks = sampleTicks(priceTicks, chartWidth < 620 ? 7 : crossed ? 12 : 11);

  const summary = empty
    ? "暂无模拟盘口数据"
    : "买单 " + buys.length + " 档，累计 " + numberText(buyTotal) + "；卖单 " + sells.length + " 档，累计 " + numberText(sellTotal) + "；" +
      "最高买价 " + (bestBuy ? priceText(bestBuy.price) : "无") + " GJN，最低卖价 " + (bestSell ? priceText(bestSell.price) : "无") + " GJN；" +
      (crossed ? "交叉盘口" : "正常盘口");

  return {
    width: chartWidth,
    height: chartHeight,
    plot,
    empty,
    crossed,
    bestBuy,
    bestSell,
    buyTotal,
    sellTotal,
    centerX,
    centerPrice,
    yTicks,
    yMax,
    xTicks,
    buy: { levels: buys, points: buyPoints, linePath: seriesPath(buyPoints), areaPath: buyAreaPath, bestPoint: buyBestPoint },
    sell: { levels: sells, points: sellPoints, linePath: seriesPath(sellPoints), areaPath: sellAreaPath, bestPoint: sellBestPoint },
    thresholds: {
      buy: buyThresholdPrice > 0 ? { price: buyThresholdPrice, x: buyThresholdX, visible: thresholdVisible(buyThresholdX) } : null,
      sell: sellTargetPrice > 0 ? { price: sellTargetPrice, x: sellThresholdX, visible: thresholdVisible(sellThresholdX) } : null,
    },
    summary,
  };
}

export function createDepthChart({ svg, tooltip, empty, summary }) {
  let lastBook = {};
  let lastRule = {};
  let lastHighlight = null;
  let lastModel = null;

  function showTooltip(event, point) {
    if (!tooltip || !point) return;
    const wrap = svg.closest(".depth-chart-wrap");
    if (!wrap) return;
    const bounds = wrap.getBoundingClientRect();
    tooltip.hidden = false;
    tooltip.innerHTML = "<strong>" + (point.side === "buy" ? "买单 " : "卖单 ") + priceText(point.price) + " GJN</strong>" +
      "<span>本档 " + numberText(point.quantity) + " · 累计 " + numberText(point.cumulative) + "</span>";
    const preferredX = event.clientX - bounds.left + 14;
    const preferredY = event.clientY - bounds.top + 14;
    tooltip.style.left = Math.min(bounds.width - 180, Math.max(8, preferredX)) + "px";
    tooltip.style.top = Math.min(bounds.height - 70, Math.max(8, preferredY)) + "px";
  }

  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
  }

  function nearestPoint(event) {
    if (!lastModel) return null;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const matrix = svg.getScreenCTM();
    if (!matrix) return null;
    const local = point.matrixTransform(matrix.inverse());
    let nearest = null;
    let distance = 24;
    for (const candidate of [...lastModel.buy.points, ...lastModel.sell.points]) {
      if (candidate.priceRaw === null) continue;
      const current = Math.hypot(candidate.x - local.x, candidate.y - local.y);
      if (current < distance) {
        nearest = candidate;
        distance = current;
      }
    }
    return nearest;
  }

  function render(book, rule, options = {}) {
    lastBook = book || {};
    lastRule = rule || {};
    lastHighlight = options.highlight || null;
    const width = Math.max(360, svg.clientWidth || svg.parentElement?.clientWidth || 960);
    const height = Math.max(270, svg.clientHeight || 360);
    const model = buildDepthChartModel(lastBook, lastRule, width, height);
    lastModel = model;
    svg.setAttribute("viewBox", "0 0 " + model.width + " " + model.height);
    svg.setAttribute("aria-label", model.summary);
    svg.replaceChildren();
    if (summary) summary.textContent = model.summary;
    if (empty) empty.hidden = !model.empty;
    svg.classList.toggle("is-empty", model.empty);

    const defs = svgElement("defs");
    defs.innerHTML = '<linearGradient id="depthBuyFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#16a36a" stop-opacity=".66"/><stop offset="100%" stop-color="#16a36a" stop-opacity=".16"/></linearGradient>' +
      '<linearGradient id="depthSellFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#d94b4b" stop-opacity=".66"/><stop offset="100%" stop-color="#d94b4b" stop-opacity=".16"/></linearGradient>';
    svg.appendChild(defs);

    for (const tick of model.yTicks) {
      const yy = model.plot.bottom - (tick / model.yMax) * model.plot.height;
      svg.appendChild(svgElement("line", { x1: model.plot.left, y1: yy, x2: model.plot.right, y2: yy, class: "depth-grid-line" }));
      svg.appendChild(svgElement("text", { x: model.plot.left - 10, y: yy + 4, class: "depth-axis-label depth-y-label", "text-anchor": "end" }, numberText(tick)));
    }
    svg.appendChild(svgElement("line", { x1: model.plot.left, y1: model.plot.top, x2: model.plot.left, y2: model.plot.bottom, class: "depth-axis-line" }));
    svg.appendChild(svgElement("line", { x1: model.plot.left, y1: model.plot.bottom, x2: model.plot.right, y2: model.plot.bottom, class: "depth-axis-line" }));

    for (const tick of model.xTicks) {
      svg.appendChild(svgElement("line", { x1: tick.x, y1: model.plot.bottom, x2: tick.x, y2: model.plot.bottom + 5, class: "depth-axis-line" }));
      svg.appendChild(svgElement("text", { x: tick.x, y: model.plot.bottom + 20, class: "depth-axis-label depth-x-label " + tick.side, "text-anchor": "middle" }, tick.label));
    }

    if (model.crossed) {
      svg.appendChild(svgElement("rect", { x: model.centerX - 31, y: model.plot.top - 1, width: 62, height: 20, rx: 10, class: "depth-cross-badge" }));
      svg.appendChild(svgElement("text", { x: model.centerX, y: model.plot.top + 13, class: "depth-cross-text", "text-anchor": "middle" }, "交叉盘口"));
    }

    if (model.buy.areaPath) svg.appendChild(svgElement("path", { d: model.buy.areaPath, fill: "url(#depthBuyFill)", class: "depth-area depth-buy-area" }));
    if (model.sell.areaPath) svg.appendChild(svgElement("path", { d: model.sell.areaPath, fill: "url(#depthSellFill)", class: "depth-area depth-sell-area" }));
    if (model.buy.linePath) svg.appendChild(svgElement("path", { d: model.buy.linePath, class: "depth-line depth-buy-line" }));
    if (model.sell.linePath) svg.appendChild(svgElement("path", { d: model.sell.linePath, class: "depth-line depth-sell-line" }));

    for (const [side, threshold] of Object.entries(model.thresholds)) {
      if (!threshold?.visible) continue;
      svg.appendChild(svgElement("line", {
        x1: threshold.x, y1: model.plot.top + 22, x2: threshold.x, y2: model.plot.bottom,
        class: "depth-threshold depth-threshold-" + side,
      }));
      const anchor = side === "sell" ? "end" : side === "buy" ? "start" : "middle";
      const labelX = side === "sell" ? Math.min(model.plot.right, threshold.x + 4) : Math.max(model.plot.left, threshold.x - 4);
      svg.appendChild(svgElement("text", {
        x: labelX, y: model.plot.top + 16, class: "depth-threshold-label " + side, "text-anchor": anchor,
      }, side === "buy" ? "规则买 " + priceText(threshold.price) : "目标挂 " + priceText(threshold.price)));
    }

    for (const [side, point, label] of [
      ["buy", model.buy.bestPoint, "最高买"],
      ["sell", model.sell.bestPoint, "最低卖"],
    ]) {
      if (!point) continue;
      svg.appendChild(svgElement("circle", { cx: point.x, cy: point.y, r: 4, class: "depth-best-point " + side }));
      svg.appendChild(svgElement("text", {
        x: point.x, y: Math.max(model.plot.top + 36, point.y - 10), class: "depth-best-label " + side, "text-anchor": "middle",
      }, label + " " + priceText(point.price)));
    }

    if (lastHighlight?.priceRaw != null) {
      const point = [...model.buy.points, ...model.sell.points].find((item) => item.priceRaw === lastHighlight.priceRaw);
      if (point) {
        svg.appendChild(svgElement("circle", { cx: point.x, cy: point.y, r: 10, class: "depth-highlight-pulse" }));
        svg.appendChild(svgElement("circle", { cx: point.x, cy: point.y, r: 5, class: "depth-highlight " + point.side }));
      }
    }
  }

  svg.addEventListener("pointermove", (event) => {
    const point = nearestPoint(event);
    if (point) showTooltip(event, point);
    else hideTooltip();
  });
  svg.addEventListener("pointerleave", hideTooltip);

  const observer = new ResizeObserver(() => render(lastBook, lastRule, { highlight: lastHighlight }));
  observer.observe(svg.parentElement || svg);
  return { render, model: () => lastModel, destroy: () => observer.disconnect() };
}
