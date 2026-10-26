import test from "node:test";
import assert from "node:assert/strict";
import { buildDepthChartModel, niceTicks } from "../views/trade/depth-chart.mjs";

const normalBook = {
  buy_levels: [
    { price: "9.00", price_raw: 90000, quantity: 4 },
    { price: "8.80", price_raw: 88000, quantity: 6 },
  ],
  sell_levels: [
    { price: "9.50", price_raw: 95000, quantity: 3 },
    { price: "9.80", price_raw: 98000, quantity: 5 },
  ],
};

test("normal book builds two-sided cumulative depth", () => {
  const model = buildDepthChartModel(normalBook, { buy_price: "8.00", sell_gross_price: "10.60" }, 960, 360);
  assert.equal(model.empty, false);
  assert.equal(model.crossed, false);
  assert.equal(model.buyTotal, 10);
  assert.equal(model.sellTotal, 8);
  assert.equal(model.buy.points[0].cumulative, 10);
  assert.equal(model.buy.points.at(-1).cumulative, 0);
  assert.equal(model.sell.points[0].cumulative, 0);
  assert.equal(model.sell.points.at(-1).cumulative, 8);
  assert.ok(model.buy.areaPath.startsWith("M"));
  assert.ok(model.sell.areaPath.endsWith("Z"));
  assert.match(model.summary, /买单 2 档/);
  assert.match(model.summary, /最高买价 9\.00 GJN/);
});

test("crossed simulated book uses split scales and reports cross", () => {
  const model = buildDepthChartModel({
    buy_levels: [{ price: "10.60", price_raw: 106000, quantity: 1 }],
    sell_levels: [{ price: "8.00", price_raw: 80000, quantity: 1 }],
  }, { buy_price: "8.00", sell_gross_price: "10.60" }, 900, 340);
  assert.equal(model.crossed, true);
  assert.equal(model.buy.points.at(-1).x, model.centerX);
  assert.equal(model.sell.points[0].x, model.centerX);
  assert.match(model.summary, /交叉盘口/);
});

test("empty and single-sided books remain renderable", () => {
  const empty = buildDepthChartModel({}, {}, 600, 300);
  assert.equal(empty.empty, true);
  assert.equal(empty.buy.areaPath, "");
  assert.match(empty.summary, /暂无/);
  const onlySell = buildDepthChartModel({ sell_levels: [{ price: "12.00", price_raw: 120000, quantity: 2 }] }, {}, 600, 300);
  assert.equal(onlySell.empty, false);
  assert.equal(onlySell.buy.points.length, 0);
  assert.equal(onlySell.sell.points.at(-1).cumulative, 2);
});

test("100 levels and tick generation stay bounded", () => {
  const buy_levels = Array.from({ length: 100 }, (_, index) => ({ price: (7 + index / 100).toFixed(2), price_raw: 70000 + index * 100, quantity: index + 1 }));
  const sell_levels = Array.from({ length: 100 }, (_, index) => ({ price: (12 + index / 100).toFixed(2), price_raw: 120000 + index * 100, quantity: 100 - index }));
  const model = buildDepthChartModel({ buy_levels, sell_levels }, {}, 1000, 420);
  assert.equal(model.buy.levels.length, 100);
  assert.equal(model.sell.levels.length, 100);
  assert.ok(model.xTicks.length <= 11);
  assert.ok(model.yTicks.length <= 8);
  const ticks = niceTicks(0, 99, 5);
  assert.equal(ticks[0], 0);
  assert.ok(ticks.at(-1) >= 99);
});

test("rule thresholds map into chart coordinates when visible", () => {
  const model = buildDepthChartModel(normalBook, { buy_price: "8.00", sell_gross_price: "10.60" }, 960, 360);
  assert.equal(model.thresholds.buy.visible, true);
  assert.equal(model.thresholds.sell.visible, true);
  assert.ok(model.thresholds.buy.x >= model.plot.left);
  assert.ok(model.thresholds.sell.x <= model.plot.right);
});
