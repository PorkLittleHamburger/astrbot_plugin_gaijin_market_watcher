const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
let lastOverview = null;
let toastTimer = null;
let modalResolver = null;

const phaseLabels = {
  waiting_buy: "等待买点",
  holding: "持有待卖",
  listing: "挂单等待成交",
  completed: "已完成",
  paused: "已暂停",
  error: "错误",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function showToast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`.trim();
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4800);
}

function openModal({ title, message, requireText = false, placeholder = "", confirmText = "确认" }) {
  if (modalResolver) modalResolver(null);
  $("modalTitle").textContent = title;
  $("modalMessage").textContent = message;
  $("modalInputWrap").hidden = !requireText;
  $("modalInput").value = "";
  $("modalInput").placeholder = placeholder;
  $("modalInputLabel").textContent = requireText ? "请输入确认内容" : "";
  $("modalConfirm").textContent = confirmText;
  $("modalBackdrop").hidden = false;
  if (requireText) setTimeout(() => $("modalInput").focus(), 0);
  else setTimeout(() => $("modalConfirm").focus(), 0);
  return new Promise((resolve) => { modalResolver = resolve; });
}

function closeModal(value) {
  $("modalBackdrop").hidden = true;
  const resolve = modalResolver;
  modalResolver = null;
  if (resolve) resolve(value);
}

function apiError(error) {
  return error?.message || error?.error || String(error || "请求失败");
}

async function apiGet(path, params = {}) {
  return bridge.apiGet(path, params);
}

async function apiPost(path, payload = {}) {
  return bridge.apiPost(path, payload);
}

function formatTimestamp(ts) {
  if (!ts) return "--";
  return new Date(Number(ts) * 1000).toLocaleString();
}

function fillSettings(settings) {
  const form = $("settingsForm");
  if (form.contains(document.activeElement)) return;
  $("settingEnabled").checked = Boolean(settings.enabled);
  $("settingTotal").value = settings.total_budget ?? "0.00";
  $("settingDaily").value = settings.daily_limit ?? "0.00";
  $("settingMaxOrder").value = settings.max_order ?? "0.00";
  $("settingMaxQty").value = settings.max_quantity ?? 0;
  $("settingInterval").value = settings.poll_interval ?? 5;
  $("settingPrivate").checked = Boolean(settings.private_mode);
}

function actionButton(label, action, ruleId, kind = "", extra = "") {
  return `<button class="button small ${kind}" data-action="${action}" data-rule="${escapeHtml(ruleId)}" ${extra}>${escapeHtml(label)}</button>`;
}

function renderRules(rules) {
  const container = $("rulesList");
  if (!rules.length) {
    container.innerHTML = '<p class="muted empty">暂无规则</p>';
    return;
  }
  container.innerHTML = rules.map((rule) => {
    const runtime = rule.runtime || {};
    const phase = phaseLabels[runtime.phase] || runtime.phase || "未知";
    const hasOrders = Array.isArray(runtime.orders) && runtime.orders.length > 0;
    const actions = [
      actionButton("编辑", "edit", rule.rule_id),
      actionButton(rule.enabled ? "暂停" : "恢复", rule.enabled ? "pause" : "resume", rule.rule_id),
      rule.live
        ? actionButton("退出实盘", "disarm", rule.rule_id)
        : actionButton("切换实盘", "arm", rule.rule_id, "danger"),
      hasOrders ? actionButton("撤销自动订单", "cancel_orders", rule.rule_id, "danger") : "",
      actionButton("删除", "delete", rule.rule_id, "ghost"),
    ].join("");
    return `
      <article class="rule-card" data-rule-card="${escapeHtml(rule.rule_id)}">
        <div class="rule-top">
          <div>
            <div class="rule-title">
              <strong>${escapeHtml(rule.title)}</strong>
              <span class="pill ${rule.live ? "live" : ""}">${rule.live ? "实盘" : "模拟"}</span>
              <span class="pill ${rule.enabled ? "" : "off"}">${rule.enabled ? "启用" : "停用"}</span>
              <span class="pill">${escapeHtml(phase)}</span>
            </div>
            <p class="muted">${escapeHtml(rule.rule_id)} · ${escapeHtml(rule.mode)} · 完成 ${runtime.cycle_count || 0} 轮</p>
          </div>
        </div>
        <div class="rule-meta">
          <div><span>买入限价</span><b>${escapeHtml(rule.buy_price)} GJN</b></div>
          <div><span>税后目标</span><b>${escapeHtml(rule.sell_net_price)} GJN</b></div>
          <div><span>税前挂单</span><b>${escapeHtml(rule.sell_gross_price)} GJN</b></div>
          <div><span>数量</span><b>${escapeHtml(rule.quantity)}</b></div>
          <div><span>持仓</span><b>${escapeHtml(runtime.held_quantity || 0)}</b></div>
          <div><span>挂单</span><b>${escapeHtml(runtime.listed_quantity || 0)}</b></div>
          <div><span>累计税后</span><b>${(Number(runtime.proceeds_raw || 0) / 10000).toFixed(2)} GJN</b></div>
          <div><span>错误</span><b>${escapeHtml(runtime.last_error || "无")}</b></div>
        </div>
        <div class="rule-actions">${actions}</div>
        ${runtime.last_error ? `<div class="error-text">${escapeHtml(runtime.last_error)}</div>` : ""}
      </article>`;
  }).join("");
}

function renderOrders(orders) {
  const container = $("ordersList");
  if (!orders.length) {
    container.innerHTML = '<p class="muted empty">暂无自动挂单</p>';
    return;
  }
  container.innerHTML = orders.map((order) => `
    <div class="stack-item">
      <strong>${escapeHtml(order.side)} · ${escapeHtml(order.market_name || "--")}</strong>
      <small>订单 ${escapeHtml(order.order_id)} · 数量 ${escapeHtml(order.amount)} · 价格 ${(Number(order.price_raw || 0) / 10000).toFixed(2)} GJN · ${formatTimestamp(order.created_at)}</small>
    </div>`).join("");
}

function renderEvents(events) {
  const container = $("logsList");
  if (!events.length) {
    container.innerHTML = '<p class="muted empty">暂无日志</p>';
    return;
  }
  container.innerHTML = events.slice().reverse().map((item) => `
    <div class="stack-item">
      <strong>${escapeHtml(item.message)}</strong>
      <small>${formatTimestamp(item.timestamp)} · ${escapeHtml(item.rule_id || "-")} · ${escapeHtml(item.kind || "event")}</small>
    </div>`).join("");
}

function render(data) {
  lastOverview = data;
  $("masterState").textContent = data.settings.enabled ? "已开启" : "未开启";
  $("exposure").textContent = `${data.exposure} GJN`;
  $("budgetRemaining").textContent = `${data.budget_remaining} GJN`;
  $("dailySpent").textContent = `${data.ledger.daily_spent} GJN`;
  const authBadge = $("authBadge");
  authBadge.textContent = data.auth?.has_jwt ? `已登录 · ${data.auth.nick || "Gaijin"}` : "未登录";
  authBadge.className = `badge ${data.auth?.has_jwt ? "ok" : "bad"}`;
  fillSettings(data.settings);
  renderRules(data.rules || []);
  renderOrders(data.orders || []);
  renderEvents(data.events || []);
}

async function refresh() {
  try {
    render(await apiGet("trade/overview"));
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

function resetRuleForm() {
  $("editRuleId").value = "";
  $("ruleForm").reset();
  $("ruleQty").value = 1;
  $("ruleMode").value = "once";
  $("ruleFormTitle").textContent = "新建交易规则";
}

function editRule(ruleId) {
  const rule = lastOverview?.rules?.find((item) => item.rule_id === ruleId);
  if (!rule) return;
  $("editRuleId").value = rule.rule_id;
  $("ruleUrl").value = `https://trade.gaijin.net/market/1067/${encodeURIComponent(rule.market_name)}`;
  $("ruleUrl").disabled = true;
  $("ruleName").value = rule.title || "";
  $("ruleBuy").value = rule.buy_price;
  $("ruleQty").value = rule.quantity;
  $("ruleSellNet").value = rule.sell_net_price;
  $("ruleMode").value = rule.mode;
  $("ruleFormTitle").textContent = `编辑 ${rule.rule_id}`;
  [...document.querySelectorAll("[data-rule-card]")]
    .find((element) => element.dataset.ruleCard === ruleId)
    ?.scrollIntoView({ behavior: "smooth" });
}

async function armRule(ruleId) {
  try {
    const prepared = await apiPost("trade/rules/action", { rule_id: ruleId, action: "prepare_arm" });
    const typed = await openModal({
      title: "切换实盘",
      message: prepared.message,
      requireText: true,
      placeholder: ruleId,
      confirmText: "确认切换实盘",
    });
    if (typed === null) return;
    await apiPost("trade/rules/action", { rule_id: ruleId, action: "arm", confirmation_id: typed });
    showToast("规则已切换实盘", "success");
    await refresh();
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

async function runRuleAction(action, ruleId) {
  if (action === "edit") return editRule(ruleId);
  if (action === "arm") return armRule(ruleId);
  if (action === "delete") {
    const confirmed = await openModal({ title: "删除规则", message: `确认删除规则 ${ruleId}？` });
    if (confirmed === null) return;
  }
  if (action === "cancel_orders") {
    const confirmed = await openModal({
      title: "撤销自动订单",
      message: `确认撤销 ${ruleId} 的全部自动订单并暂停规则？`,
    });
    if (confirmed === null) return;
  }
  try {
    await apiPost("trade/rules/action", { rule_id: ruleId, action });
    showToast("操作完成", "success");
    await refresh();
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

$("rulesList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  runRuleAction(button.dataset.action, button.dataset.rule);
});

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiPost("trade/settings", {
      enabled: $("settingEnabled").checked,
      total_budget: $("settingTotal").value,
      daily_limit: $("settingDaily").value,
      max_order: $("settingMaxOrder").value,
      max_quantity: Number($("settingMaxQty").value || 0),
      poll_interval: Number($("settingInterval").value || 5),
      private_mode: $("settingPrivate").checked,
    });
    showToast("设置已保存", "success");
    await refresh();
  } catch (error) {
    showToast(apiError(error), "error");
  }
});

$("ruleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    url: $("ruleUrl").value,
    display_name: $("ruleName").value,
    buy_price: $("ruleBuy").value,
    quantity: Number($("ruleQty").value),
    sell_net_price: $("ruleSellNet").value,
    mode: $("ruleMode").value,
  };
  const editId = $("editRuleId").value;
  if (editId) payload.rule_id = editId;
  try {
    await apiPost("trade/rules/save", payload);
    showToast(editId ? "规则已更新" : "规则已创建", "success");
    $("ruleUrl").disabled = false;
    resetRuleForm();
    await refresh();
  } catch (error) {
    showToast(apiError(error), "error");
  }
});

$("resetRuleBtn").addEventListener("click", () => {
  $("ruleUrl").disabled = false;
  resetRuleForm();
});

$("refreshBtn").addEventListener("click", refresh);

$("stopAllBtn").addEventListener("click", async () => {
  const confirmation = await openModal({
    title: "自动交易急停",
    message: "急停会撤销本插件创建的全部自动订单并暂停所有规则。",
    requireText: true,
    placeholder: "stop-all",
    confirmText: "执行急停",
  });
  if (confirmation === null) return;
  try {
    await apiPost("trade/stop-all", { confirmation });
    showToast("自动交易已急停", "success");
    await refresh();
  } catch (error) {
    showToast(apiError(error), "error");
  }
});

$("modalCancel").addEventListener("click", () => closeModal(null));
$("modalConfirm").addEventListener("click", () => closeModal($("modalInput").value));
$("modalInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") closeModal($("modalInput").value);
});
$("modalBackdrop").addEventListener("click", (event) => {
  if (event.target === $("modalBackdrop")) closeModal(null);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("modalBackdrop").hidden) closeModal(null);
});

await bridge.ready();
document.title = bridge.t("views.trade.title", "Gaijin 自动交易");
await refresh();
setInterval(refresh, 3000);
bridge.onContext(() => {
  document.title = bridge.t("views.trade.title", "Gaijin 自动交易");
});
