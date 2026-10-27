import { createDepthChart } from "./depth-chart.mjs";
import { sellerNet } from "./book-model.mjs";

const PHASE_TEXT = {
  waiting_buy: "等待买点",
  buying: "买入中",
  holding: "持有中",
  selling: "卖出中",
  working: "买卖并行",
  listing: "挂单中",
  completed: "已完成",
  paused: "已暂停",
  error: "错误",
};

const $id = (id) => document.getElementById(id);

export function createPaperWorkbench({ $, apiGet, apiPost, showToast, openModal, runWithBusy, escapeHtml, getRule, refreshTrade }) {
  let ruleId = "";
  let rule = null;
  let scenarioId = "";
  let chart = null;
  let returnFocus = null;
  let refreshSeq = 0;
  let lastAccountScope = "";
  let lastAccountVersion = -1;
  let lastBookScope = "";
  let lastBookVersion = "";
  let pendingOrder = null;
  let workspaceSeq = 0;
  let side = "BUY";

  function marketInput() { return $id("paperMarketInput"); }
  function priceInput() { return $id("paperPrice"); }
  function quantityInput() { return $id("paperQuantity"); }

  function ensureChart() {
    if (chart) return chart;
    chart = createDepthChart({
      svg: $("depthChart"),
      tooltip: $("depthChartTooltip"),
      empty: $("depthChartEmpty"),
      summary: $("depthChartSummary"),
    });
    return chart;
  }

  function chartBook(raw) {
    const levels = (items) => (items || []).map((level) => ({
      price: (Number(level.price_raw || 0) / 10000).toFixed(2),
      price_raw: Number(level.price_raw || 0),
      quantity: Number(level.amount || level.quantity || 0),
    }));
    return {
      buy_levels: levels(raw?.buy_levels),
      sell_levels: levels(raw?.sell_levels),
      buy_depth: raw?.depth_buy || 0,
      sell_depth: raw?.depth_sell || 0,
    };
  }

  function updateEstimate() {
    const priceText = priceInput().value.trim();
    const quantity = Number(quantityInput().value || 0);
    const price = Number(priceText || 0);
    const total = price * quantity;
    if (side === "SELL") {
      $("paperEstimateLabel").textContent = "预计到手（15% 手续费后）";
      $("paperEstimateValue").textContent = `${sellerNet(priceText, quantity).toFixed(2)} GJN`;
    } else {
      $("paperEstimateLabel").textContent = "预计支出";
      $("paperEstimateValue").textContent = `${total.toFixed(2)} GJN`;
    }
  }

  function setSide(value) {
    side = value === "SELL" ? "SELL" : "BUY";
    if (rule) priceInput().value = side === "BUY" ? rule.buy_price : rule.sell_gross_price;
    $("paperSideBuy").classList.toggle("active", side === "BUY");
    $("paperSideSell").classList.toggle("active", side === "SELL");
    $("paperSideBuy").setAttribute("aria-pressed", side === "BUY" ? "true" : "false");
    $("paperSideSell").setAttribute("aria-pressed", side === "SELL" ? "false" : "true");
    $("paperSubmit").textContent = `提交模拟${side === "BUY" ? "买单" : "卖单"}`;
    updateEstimate();
  }

  function setWorkbenchMessage(message, kind = "") {
    const element = $("workbenchMessage");
    element.textContent = message || "";
    element.className = `inline-message ${kind}`.trim();
    element.hidden = !message;
  }

  function resetVersionGuards() {
    lastAccountScope = "";
    lastAccountVersion = -1;
    lastBookScope = "";
    lastBookVersion = "";
  }

  function renderAccount(account) {
    if (!account) return;
    const scope = scenarioId || "base";
    const version = Number(account.version ?? -1);
    if (scope === lastAccountScope && version >= 0 && version < lastAccountVersion) return;
    lastAccountScope = scope;
    lastAccountVersion = version;
    $("paperAccountGrid").innerHTML = [
      ["可用 GJN", account.cash_available],
      ["冻结 GJN", account.cash_reserved],
      ["账户权益", account.equity],
      ["已实现盈亏", account.realized_pnl],
      ["未实现盈亏", account.unrealized_pnl],
    ].map(([label, value]) => `<div class="status-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`).join("");
    const inventory = account.inventory || [];
    $("paperInventory").innerHTML = inventory.length
      ? inventory.map((item) => {
          const lots = (item.lots || [])
            .map((lot) => `${lot.remaining_quantity}/${lot.quantity} @ ${lot.unit_cost} GJN`)
            .join(" · ");
          return `<div class="paper-inventory-row"><div><strong>${escapeHtml(item.market_name)}</strong><span>${escapeHtml(item.quantity)} 可用 ${escapeHtml(item.available_quantity)} · 成本 ${escapeHtml(item.cost_basis)} GJN</span>${lots ? `<small class="paper-lot-line">FIFO 批次：${escapeHtml(lots)}</small>` : ""}</div></div>`;
        }).join("")
      : '<p class="muted empty">暂无模拟库存</p>';
    if ($("paperStartingCash") && !$("paperStartingCash").dataset.touched) {
      $("paperStartingCash").value = account.starting_cash || "1000";
    }
  }

  function renderRuleState() {
    if (!rule) return;
    const runtime = rule.runtime || {};
    $("workbenchPhase").textContent = PHASE_TEXT[runtime.phase] || runtime.phase || "未知";
    $("workbenchStatusGrid").innerHTML = [
      ["规则", `${rule.title} · ${rule.rule_id}`],
      ["环境", rule.live ? "实盘" : "模拟盘"],
      ["状态", rule.enabled ? "启用" : "暂停"],
      ["买入限价", `${rule.buy_price} GJN`],
      ["税后目标", `${rule.sell_net_price} GJN`],
      ["目标数量", rule.quantity],
      ["轮次", runtime.cycle_count || 0],
    ].map(([label, value]) => `<div class="status-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`).join("");
    $("workbenchPause").disabled = !rule.enabled;
    $("workbenchResume").disabled = Boolean(rule.enabled);
  }

  function renderOrders(rows) {
    const visible = (rows || []).slice(0, 30);
    const list = $("paperOrdersList");
    const focusedCancel = document.activeElement?.matches?.("[data-cancel-paper-order]")
      ? document.activeElement.dataset.cancelPaperOrder
      : "";
    list.innerHTML = visible.length
      ? visible.map((order) => `<div class="paper-order-row" data-order-id="${escapeHtml(order.order_id)}" data-side="${escapeHtml(order.side)}">
          <div><strong>${escapeHtml(order.market_name)}</strong><b class="${order.side === "BUY" ? "buy-text" : "sell-text"}">${escapeHtml(order.side)} ${escapeHtml(order.price)} GJN</b>
          <span>${escapeHtml(order.filled_quantity)} / ${escapeHtml(order.quantity)} 成交 · ${escapeHtml(order.status)}</span></div>
          ${["OPEN", "PARTIALLY_FILLED"].includes(order.status) ? `<button class="button small ghost" data-cancel-paper-order="${escapeHtml(order.order_id)}" type="button" aria-label="撤销 ${escapeHtml(order.market_name)} ${escapeHtml(order.side)} ${escapeHtml(order.price)} GJN 模拟订单">撤单</button>` : ""}
        </div>`).join("")
      : '<p class="muted empty">暂无订单</p>';
    if (focusedCancel) {
      const target = [...list.querySelectorAll("[data-cancel-paper-order]")]
        .find((button) => button.dataset.cancelPaperOrder === focusedCancel);
      if (target) target.focus({ preventScroll: true });
      else {
        list.tabIndex = -1;
        list.focus({ preventScroll: true });
      }
    }
  }

  function renderFills(rows) {
    const visible = (rows || []).slice(0, 30);
    $("paperFillsList").innerHTML = visible.length
      ? visible.map((fill) => `<div class="paper-fill-row">
          <strong>${escapeHtml(fill.market_name)}</strong>
          <b class="${fill.side === "BUY" ? "buy-text" : "sell-text"}">${escapeHtml(fill.side)} ${escapeHtml(fill.price)} GJN</b>
          <span>${escapeHtml(fill.quantity)} 个 · 费 ${escapeHtml(fill.fee)} · 净 ${escapeHtml(fill.net)} GJN${fill.side === "SELL" ? ` · 成本 ${escapeHtml(fill.cost_basis)} · 已实现 ${escapeHtml(fill.realized_pnl)} GJN` : ""}</span>
        </div>`).join("")
      : '<p class="muted empty">暂无成交</p>';
  }

  function renderScenario(rows) {
    const active = (rows || []).find((item) => item.rule_id === ruleId && item.status !== "ENDED");
    scenarioId = active ? active.scenario_id : "";
    if (active && rule?.market_name) marketInput().value = rule.market_name;
    marketInput().readOnly = Boolean(active);
    $("paperMarketSearchBtn").disabled = Boolean(active);
    $("paperScenarioBadge").textContent = active ? `场景 ${active.status}` : "真实盘口";
    $("paperScenarioBadge").className = active ? "pill live" : "pill";
    $("paperScenarioClose").hidden = !active;
    $("paperScenarioCreate").hidden = Boolean(active);
    if (!active) {
      $("paperScenarioStatus").textContent = "使用真实市场盘口；不会修改真实市场";
    } else {
      const target = active.target_side === "SELL" ? "卖出" : "买入";
      const gap = `${target}缺口 ${Math.ceil(Number(active.target_quantity || 0))} 个`;
      $("paperScenarioStatus").textContent = active.status === "PENDING"
        ? `场景将在 ${Math.ceil(Number(active.activate_in_seconds || 0))} 秒后生效，持续 ${Math.ceil(Number(active.duration_seconds || 0))} 秒 · ${gap}`
        : `场景将在 ${Math.ceil(Number(active.remaining_seconds || 0))} 秒后结束 · ${gap}`;
    }
  }

  function renderBook(raw) {
    const scope = scenarioId || "base";
    const version = String(raw?.version || raw?.book_version || "");
    if (version && scope === lastBookScope && version === lastBookVersion) return;
    lastBookScope = scope;
    lastBookVersion = version;
    const levels = chartBook(raw);
    if (chart) chart.render(levels, rule || {}, {});
    const bestBuy = levels.buy_levels[0]?.price || "无";
    const bestSell = levels.sell_levels[0]?.price || "无";
    $("bestBuy").textContent = bestBuy === "无" ? "无" : `${bestBuy} GJN`;
    $("bestSell").textContent = bestSell === "无" ? "无" : `${bestSell} GJN`;
    const spread = bestBuy !== "无" && bestSell !== "无" ? (Number(bestSell) - Number(bestBuy)).toFixed(2) : "";
    $("bookSpread").textContent = spread ? `${spread} GJN` : "无";
    $("buyDepth").textContent = `${levels.buy_levels.length} 档 / 深度 ${raw?.depth_buy || 0}`;
    $("sellDepth").textContent = `${levels.sell_levels.length} 档 / 深度 ${raw?.depth_sell || 0}`;
    $("bookSource").textContent = raw?.synthetic ? "场景盘口（仅当前规则可见）" : "真实市场盘口";
  }

  function render(data) {
    renderRuleState();
    renderScenario(data.scenarios || []);
    renderAccount(data.account);
    renderOrders(data.orders || []);
    renderFills(data.fills || []);
  }

  function invalidateRefresh() {
    refreshSeq += 1;
  }

  function actionIsCurrent(sessionId, selectedRuleId) {
    return sessionId === workspaceSeq && ruleId === selectedRuleId && !$("workbenchBackdrop").hidden;
  }

  async function refresh({ initial = false } = {}) {
    if (!ruleId || $("workbenchBackdrop").hidden) return;
    const requestId = ++refreshSeq;
    const selectedRuleId = ruleId;
    const isCurrent = () => requestId === refreshSeq && ruleId === selectedRuleId && !$("workbenchBackdrop").hidden;
    try {
      const trade = await apiGet("trade/overview");
      if (!isCurrent()) return;
      rule = (trade.rules || []).find((item) => item.rule_id === selectedRuleId) || getRule?.(selectedRuleId) || null;
      if (!rule) throw new Error("找不到当前规则");
      let first = await apiGet("trade/paper/overview", { scenario_id: scenarioId });
      if (!isCurrent()) return;
      const activeScenario = (first.scenarios || []).find(
        (item) => item.rule_id === selectedRuleId && item.status !== "ENDED",
      );
      if (!scenarioId && activeScenario) {
        scenarioId = activeScenario.scenario_id;
        first = await apiGet("trade/paper/overview", { scenario_id: scenarioId });
        if (!isCurrent()) return;
      }
      render(first);
      const market = marketInput().value.trim() || rule?.market_name || "";
      if (market) {
        const marketData = await apiGet("trade/paper/book", { market_name: market, scenario_id: scenarioId });
        if (!isCurrent()) return;
        renderBook(marketData.book || {});
        renderAccount(marketData.account || first.account);
      }
      const latest = await apiGet("trade/paper/overview", { scenario_id: scenarioId });
      if (!isCurrent()) return;
      render(latest);
      if ("active_scenario_id" in latest) scenarioId = latest.active_scenario_id || "";
      setWorkbenchMessage("");
      if (initial) {
        marketInput().value = rule?.market_name || "";
        priceInput().value = rule?.buy_price || "";
        quantityInput().value = String(rule?.quantity || 1);
        updateEstimate();
      }
      return true;
    } catch (error) {
      if (!isCurrent()) return;
      const message = error?.message || "读取模拟盘失败";
      setWorkbenchMessage(`读取失败：${message}`, "error");
      showToast(message, "error");
      if (scenarioId && /场景.*(结束|不存在)/.test(message)) {
        scenarioId = "";
        resetVersionGuards();
        void refresh();
      }
      return false;
    }
  }

  async function submitOrder(event) {
    event.preventDefault();
    const selectedRuleId = ruleId;
    const sessionId = workspaceSeq;
    invalidateRefresh();
    const market = marketInput().value.trim();
    const price = priceInput().value.trim();
    const quantity = Number(quantityInput().value || 0);
    if (!market) return showToast("请选择商品", "error");
    if (!/^\d+(?:\.\d{1,2})?$/.test(price) || Number(price) <= 0) return showToast("价格必须大于 0 且最多两位小数", "error");
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 1000) return showToast("数量必须在 1 到 1000 之间", "error");
    const fingerprint = JSON.stringify([selectedRuleId, market, side, price, quantity, scenarioId]);
    if (!pendingOrder || pendingOrder.fingerprint !== fingerprint) {
      pendingOrder = {
        fingerprint,
        clientOrderId: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      };
    }
    await runWithBusy($("paperSubmit"), "提交中…", async () => {
      try {
        await apiPost("trade/paper/orders", {
          market_name: market,
          side,
          price,
          quantity,
          client_order_id: pendingOrder.clientOrderId,
          scenario_id: scenarioId,
        });
        if (!actionIsCurrent(sessionId, selectedRuleId)) return;
        showToast("模拟订单已提交", "success");
        if (await refresh()) pendingOrder = null;
        await refreshTrade?.();
      } catch (error) {
        if (actionIsCurrent(sessionId, selectedRuleId)) {
          showToast(error?.message || "模拟订单提交失败", "error");
        }
      }
    });
  }

  async function cancelOrder(orderId) {
    const selectedRuleId = ruleId;
    const sessionId = workspaceSeq;
    invalidateRefresh();
    try {
      await apiPost(`trade/paper/orders/${encodeURIComponent(orderId)}/cancel`, { scenario_id: scenarioId });
      if (!actionIsCurrent(sessionId, selectedRuleId)) return;
      showToast("模拟订单已撤销", "success");
      await refresh();
      await refreshTrade?.();
    } catch (error) {
      if (actionIsCurrent(sessionId, selectedRuleId)) showToast(error?.message || "撤单失败", "error");
    }
  }

  async function createScenario() {
    if (!ruleId) return;
    const selectedRuleId = ruleId;
    const sessionId = workspaceSeq;
    invalidateRefresh();
    const delay = Number($("paperScenarioDelay").value || 5);
    const duration = Number($("paperScenarioDuration").value || 30);
    await runWithBusy($("paperScenarioCreate"), "生成中…", async () => {
      try {
        const result = await apiPost("trade/paper/scenarios", { rule_id: selectedRuleId, delay_seconds: delay, duration_seconds: duration });
        if (!actionIsCurrent(sessionId, selectedRuleId)) return;
        scenarioId = result.scenario_id || "";
        resetVersionGuards();
        showToast("行情场景已创建", "success");
        await refresh();
      } catch (error) {
        if (actionIsCurrent(sessionId, selectedRuleId)) showToast(error?.message || "创建场景失败", "error");
      }
    });
  }

  async function closeScenario() {
    if (!scenarioId) return;
    const selectedRuleId = ruleId;
    const sessionId = workspaceSeq;
    invalidateRefresh();
    await runWithBusy($("paperScenarioClose"), "结束中…", async () => {
      try {
        await apiPost(`trade/paper/scenarios/${encodeURIComponent(scenarioId)}`, {});
        if (!actionIsCurrent(sessionId, selectedRuleId)) return;
        scenarioId = "";
        resetVersionGuards();
        showToast("场景已结束，已恢复真实盘口", "success");
        await refresh();
      } catch (error) {
        if (actionIsCurrent(sessionId, selectedRuleId)) showToast(error?.message || "结束场景失败", "error");
      }
    });
  }

  async function resetAccount() {
    const startingCash = $("paperStartingCash").value.trim();
    const confirmation = await openModal({
      title: "重置模拟账户",
      message: "这会清空全部纸面订单、成交、库存和盈亏，并按输入的初始 GJN 重建账户。真实市场不会受影响。",
      requireText: true,
      placeholder: "RESET",
      confirmText: "重置账户",
    });
    if (confirmation === null) return;
    invalidateRefresh();
    if (String(confirmation).trim().toUpperCase() !== "RESET") {
      showToast("重置确认必须输入 RESET", "error");
      return;
    }
    await runWithBusy($("paperAccountReset"), "重置中…", async () => {
      try {
        await apiPost("trade/paper/reset", { starting_cash: startingCash, confirmation });
        scenarioId = "";
        pendingOrder = null;
        resetVersionGuards();
        showToast("模拟账户已重置", "success");
        await refresh({ initial: true });
        await refreshTrade?.();
      } catch (error) {
        showToast(error?.message || "重置模拟账户失败", "error");
      }
    });
  }

  async function searchMarkets() {
    if (scenarioId) return showToast("行情场景期间请先结束场景，再切换商品", "error");
    const query = marketInput().value.trim();
    if (!query) return;
    const selectedRuleId = ruleId;
    const sessionId = workspaceSeq;
    invalidateRefresh();
    await runWithBusy($("paperMarketSearchBtn"), "搜索中…", async () => {
      try {
        const data = await apiGet("trade/paper/markets/search", { query });
        if (!actionIsCurrent(sessionId, selectedRuleId)) return;
        const assets = data.assets || [];
        $("paperMarketResults").innerHTML = assets.length
          ? assets.map((item) => `<button type="button" class="chip-button" data-market-name="${escapeHtml(item.hash_name || item.market_hash_name || "")}">${escapeHtml(item.name || item.hash_name || "")}</button>`).join("")
          : '<span class="muted">没有匹配商品</span>';
      } catch (error) {
        showToast(error?.message || "搜索失败", "error");
      }
    });
  }

  function open(selectedRuleId, trigger = null) {
    invalidateRefresh();
    workspaceSeq += 1;
    ruleId = selectedRuleId;
    returnFocus = trigger || document.activeElement;
    rule = getRule?.(ruleId) || null;
    scenarioId = "";
    $("workbenchBackdrop").hidden = false;
    document.body.classList.add("workbench-open");
    $("workbenchTitle").textContent = `${rule?.title || ruleId} · 模拟交易工作台`;
    $("workbenchSubtitle").textContent = `${ruleId} · 正在读取模拟账户与真实盘口…`;
    $("paperScenarioDelay").value = "5";
    $("paperScenarioDuration").value = "30";
    $("paperStartingCash").dataset.touched = "";
    pendingOrder = null;
    resetVersionGuards();
    setWorkbenchMessage("");
    ensureChart();
    setSide("BUY");
    setTimeout(() => $("workbenchClose").focus(), 0);
    return refresh({ initial: true });
  }

  function close() {
    if ($("workbenchBackdrop").hidden) return;
    invalidateRefresh();
    workspaceSeq += 1;
    $("workbenchBackdrop").hidden = true;
    document.body.classList.remove("workbench-open");
    ruleId = "";
    scenarioId = "";
    pendingOrder = null;
    setWorkbenchMessage("");
    const target = returnFocus;
    returnFocus = null;
    if (target?.isConnected) setTimeout(() => target.focus(), 0);
  }

  function init() {
    $("paperTicketForm").addEventListener("submit", (event) => void submitOrder(event));
    $("paperSideBuy").addEventListener("click", () => setSide("BUY"));
    $("paperSideSell").addEventListener("click", () => setSide("SELL"));
    priceInput().addEventListener("input", updateEstimate);
    quantityInput().addEventListener("input", updateEstimate);
    $("paperMarketSearchBtn").addEventListener("click", () => void searchMarkets());
    $("paperOrdersList").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-cancel-paper-order]");
      if (button) void cancelOrder(button.dataset.cancelPaperOrder);
    });
    $("paperMarketResults").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-market-name]");
      if (!button) return;
      marketInput().value = button.dataset.marketName || "";
      $("paperMarketResults").innerHTML = "";
      priceInput().focus();
    });
    $("paperScenarioCreate").addEventListener("click", () => void createScenario());
    $("paperScenarioClose").addEventListener("click", () => void closeScenario());
    $("paperStartingCash").addEventListener("input", () => { $("paperStartingCash").dataset.touched = "1"; });
    $("paperAccountReset").addEventListener("click", () => void resetAccount());
    $("paperOrdersRefresh").addEventListener("click", () => void runWithBusy($("paperOrdersRefresh"), "刷新中…", refresh));
    setSide("BUY");
  }

  init();
  return { open, refresh, close };
}
