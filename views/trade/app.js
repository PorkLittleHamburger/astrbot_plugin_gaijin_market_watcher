const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
let lastOverview = null;
let lastMonitor = null;
let lastOutput = null;
let lastAccount = null;
let activeTab = "monitor";
let toastTimer = null;
let modalResolver = null;
let outputDirty = false;
let accountDirty = false;
let tradeLogCursor = 0;
const tradeLogIds = new Set();

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

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (!value) return "无";
  if (value >= 86400) return `${(value / 86400).toFixed(1)} 天`;
  if (value >= 3600) return `${(value / 3600).toFixed(1)} 小时`;
  if (value >= 60) return `${Math.floor(value / 60)} 分钟`;
  return `${Math.floor(value)} 秒`;
}

function authIsValid(auth) {
  return Boolean(auth?.has_jwt && Number(auth.seconds_left || 0) > 0);
}

function showInlineMessage(elementId, message, kind = "") {
  const el = $(elementId);
  el.textContent = message || "";
  el.className = `inline-message ${kind}`.trim();
  el.hidden = !message;
}

function activateTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
  if (name === "monitor") void loadMonitor();
  if (name === "account") void loadAccount();
  if (name === "output") void loadOutput();
  if (name === "trade") void refreshTrade({ resetLogs: true });
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
});
document.querySelectorAll("[data-go-tab]").forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.goTab));
});

// ---------------------------------------------------------------- 账户登录
function renderAccount(data) {
  lastAccount = data;
  const account = data.account || {};
  const auth = data.auth || {};
  const loginValid = authIsValid(auth);
  const tokenState = loginValid ? "有效" : (auth.has_jwt ? "已过期" : "未登录");
  const editingAccount = $("accountForm").contains(document.activeElement);
  if (!editingAccount && !accountDirty) {
    $("accountEmail").value = account.email || "";
    $("ownerUids").value = (account.owner_uids || []).join("\n");
    $("accountPassword").value = "";
  }
  $("accountPassword").placeholder = account.password_configured ? "已保存，留空保持不变" : "请输入账号密码";

  const statusItems = [
    ["登录令牌", tokenState],
    ["Gaijin 账号", auth.nick || "未知"],
    ["令牌剩余", formatDuration(auth.seconds_left)],
    ["续期令牌", auth.has_refresh_token ? "已保存" : "无"],
    ["账号密码", account.password_configured ? "已保存" : "未配置"],
    ["拥有者 UID", `${(account.owner_uids || []).length} 个`],
  ];
  if (auth.pending_login) statusItems.push(["验证码", `等待提交（已 ${formatDuration(auth.pending_age)}）`]);
  if (auth.last_error) statusItems.push(["最近错误", auth.last_error]);
  $("accountStatus").innerHTML = statusItems
    .map(([label, value]) => `<div class="status-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`)
    .join("");
  $("codePanel").hidden = !auth.pending_login;

  const badge = $("authBadge");
  badge.textContent = loginValid ? `已登录 · ${auth.nick || "Gaijin"}` : (auth.pending_login ? "等待验证码" : tokenState);
  badge.className = `badge ${loginValid ? "ok" : "bad"}`;
  $("loginHint").hidden = loginValid;
  $("monitorStatus").textContent = loginValid ? "已登录" : tokenState;
}

async function loadAccount() {
  try {
    renderAccount(await apiGet("account/overview"));
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

function accountPayload() {
  return {
    email: $("accountEmail").value.trim(),
    password: $("accountPassword").value,
    owner_uids: $("ownerUids").value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean),
  };
}

async function saveAccount({ silent = false } = {}) {
  try {
    const data = await apiPost("account/save", accountPayload());
    accountDirty = false;
    renderAccount(data);
    if (!silent) showToast("账号设置已保存", "success");
    return data;
  } catch (error) {
    if (!silent) showToast(apiError(error), "error");
    else throw error;
    return null;
  }
}

async function startLogin() {
  try {
    await saveAccount({ silent: true });
    const data = await apiPost("account/login/start", {});
    renderAccount(data);
    showInlineMessage("accountMessage", data.message || "登录请求已完成", data.need_code ? "" : "ok");
    showToast(data.need_code ? "请输入两步验证码" : "登录状态已更新", data.need_code ? "" : "success");
  } catch (error) {
    showInlineMessage("accountMessage", apiError(error), "error");
    showToast(apiError(error), "error");
  }
}

async function submitLoginCode() {
  const code = $("loginCode").value.trim();
  if (!code) {
    showToast("请输入验证码", "error");
    return;
  }
  try {
    const data = await apiPost("account/login/code", { code });
    $("loginCode").value = "";
    renderAccount(data);
    showInlineMessage("accountMessage", data.message || "验证码已提交", "ok");
    showToast("登录状态已更新", "success");
  } catch (error) {
    showInlineMessage("accountMessage", apiError(error), "error");
    showToast(apiError(error), "error");
  }
}

$("accountForm").addEventListener("input", () => { accountDirty = true; });
$("accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveAccount();
});
$("loginStartBtn").addEventListener("click", startLogin);
$("loginCodeBtn").addEventListener("click", submitLoginCode);
$("accountRefreshBtn").addEventListener("click", () => { accountDirty = false; void loadAccount(); });
$("loginCode").addEventListener("keydown", (event) => {
  if (event.key === "Enter") submitLoginCode();
});

// ---------------------------------------------------------------- 行情订阅
function sessionsFromMonitor() {
  return lastMonitor?.sessions || [];
}

function fillSessionOptions() {
  const select = $("subSessionSelect");
  const current = select.value;
  const sessions = sessionsFromMonitor();
  select.innerHTML = '<option value="">手工填写 UMO</option>' + sessions
    .map((item) => `<option value="${escapeHtml(item.umo)}">${escapeHtml(item.name || item.uid)} · ${escapeHtml(item.umo)}</option>`)
    .join("");
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}

function renderMonitor(data) {
  lastMonitor = data;
  const groups = data.groups || [];
  const itemCount = groups.reduce((sum, group) => sum + (group.items || []).length, 0);
  $("monitorGroupCount").textContent = String(groups.length);
  $("monitorItemCount").textContent = String(itemCount);
  $("monitorWatchedCount").textContent = String((data.watched_market_names || []).length);
  $("monitorStatus").textContent = authIsValid(lastAccount?.auth) ? "已登录" : "未登录";
  fillSessionOptions();

  const container = $("subscriptionList");
  if (!groups.length) {
    container.innerHTML = '<p class="muted empty">暂无订阅</p>';
    return;
  }
  container.innerHTML = groups.map((group) => `
    <article class="stack-item" data-group-uid="${escapeHtml(group.uid)}" data-group-target="${escapeHtml(group.target_umo)}">
      <div class="stack-head">
        <div>
          <strong>${escapeHtml(group.note || group.uid)}</strong>
          <small>${escapeHtml(group.uid)} · ${escapeHtml(group.target_umo)}</small>
          <small>${group.enabled ? "启用" : "停用"} · ${escapeHtml(group.mode)} · ${escapeHtml(group.interval)} 秒</small>
        </div>
        <div class="stack-actions">
          <button class="button small secondary" type="button" data-sub-action="edit">编辑</button>
          <button class="button small ${group.enabled ? "ghost" : "primary"}" type="button" data-sub-action="toggle">${group.enabled ? "停用" : "启用"}</button>
          <button class="button small danger" type="button" data-sub-action="delete-group">删除组</button>
        </div>
      </div>
      <ul class="sub-items">
        ${(group.items || []).map((item) => `
          <li>
            <span>${escapeHtml(item.display_name || item.market_name)} <small>${escapeHtml(item.url)}</small></span>
            <button class="button small ghost" type="button" data-sub-action="delete-item" data-market="${escapeHtml(item.market_name)}">删除</button>
          </li>`).join("")}
      </ul>
    </article>`).join("");
}

async function loadMonitor() {
  try {
    renderMonitor(await apiGet("monitor/overview"));
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

function resetSubForm() {
  $("subOriginalUid").value = "";
  $("subOriginalTarget").value = "";
  $("subUid").value = "";
  $("subTarget").value = "";
  $("subSessionSelect").value = "";
  $("subInterval").value = String(lastMonitor?.defaults?.interval || 30);
  $("subMode").value = lastMonitor?.defaults?.mode || "only_change";
  $("subEnabled").checked = true;
  $("subItems").value = "";
  $("subscriptionFormTitle").textContent = "新增订阅组";
}

function editSubscriptionGroup(uid, target) {
  const group = (lastMonitor?.groups || []).find((item) => item.uid === uid && item.target_umo === target);
  if (!group) return;
  $("subOriginalUid").value = group.uid;
  $("subOriginalTarget").value = group.target_umo;
  $("subUid").value = group.uid;
  $("subTarget").value = group.target_umo;
  $("subSessionSelect").value = group.target_umo;
  $("subInterval").value = String(group.interval);
  $("subMode").value = group.mode;
  $("subEnabled").checked = Boolean(group.enabled);
  $("subItems").value = (group.items || []).map((item) => item.url).join("\n");
  $("subscriptionFormTitle").textContent = `编辑订阅组 · ${group.note || group.uid}`;
  $("subscriptionForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

function subscriptionPayloadFromForm() {
  const lines = $("subItems").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const existing = (lastMonitor?.groups || []).find(
    (group) => group.uid === $("subOriginalUid").value && group.target_umo === $("subOriginalTarget").value,
  );
  const names = new Map((existing?.items || []).map((item) => [item.url, item.display_name]));
  return {
    original_uid: $("subOriginalUid").value,
    original_target: $("subOriginalTarget").value,
    uid: $("subUid").value.trim(),
    target_umo: $("subTarget").value.trim(),
    enabled: $("subEnabled").checked,
    interval: Number($("subInterval").value || 30),
    mode: $("subMode").value,
    items: lines.map((url) => ({ url, display_name: names.get(url) || "" })),
  };
}

async function saveSubscription(payload) {
  try {
    const data = await apiPost("monitor/subscriptions/save", payload);
    renderMonitor(data);
    showToast(data.message || "订阅已保存", "success");
    return true;
  } catch (error) {
    showToast(apiError(error), "error");
    return false;
  }
}

async function deleteSubscription(uid, target, market = "") {
  const confirmed = await openModal({
    title: market ? "删除订阅物品" : "删除订阅组",
    message: market ? `确认删除物品 ${market}？` : `确认删除 ${uid} 在 ${target} 的整组订阅？`,
  });
  if (confirmed === null) return;
  try {
    const data = await apiPost("monitor/subscriptions/delete", { uid, target_umo: target, market_name: market });
    renderMonitor(data);
    showToast(data.message || "订阅已删除", "success");
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

async function toggleSubscriptionGroup(uid, target) {
  const group = (lastMonitor?.groups || []).find((item) => item.uid === uid && item.target_umo === target);
  if (!group) return;
  await saveSubscription({
    original_uid: group.uid,
    original_target: group.target_umo,
    uid: group.uid,
    target_umo: group.target_umo,
    enabled: !group.enabled,
    interval: group.interval,
    mode: group.mode,
    items: (group.items || []).map((item) => ({ url: item.url, display_name: item.display_name })),
  });
}

$("subscriptionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ok = await saveSubscription(subscriptionPayloadFromForm());
  if (ok) resetSubForm();
});
$("resetSubBtn").addEventListener("click", resetSubForm);
$("subSessionSelect").addEventListener("change", () => {
  const session = sessionsFromMonitor().find((item) => item.umo === $("subSessionSelect").value);
  if (session) {
    $("subTarget").value = session.umo;
    $("subUid").value = session.uid;
  }
});
$("subscriptionList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-sub-action]");
  if (!button) return;
  const card = button.closest("[data-group-uid]");
  const uid = card?.dataset.groupUid || "";
  const target = card?.dataset.groupTarget || "";
  const action = button.dataset.subAction;
  if (action === "edit") editSubscriptionGroup(uid, target);
  if (action === "toggle") toggleSubscriptionGroup(uid, target);
  if (action === "delete-group") deleteSubscription(uid, target);
  if (action === "delete-item") deleteSubscription(uid, target, button.dataset.market || "");
});

// ---------------------------------------------------------------- 输出模板
function renderFieldPalette(elementId, groups, targetId, prefix = "") {
  const container = $(elementId);
  container.innerHTML = Object.entries(groups || {}).map(([groupName, fields]) => `
    <div class="field-group">
      <strong>${escapeHtml(groupName)}</strong>
      <div class="field-chips">
        ${Object.keys(fields).map((field) => `<button class="field-chip" type="button" data-field="${escapeHtml(prefix + field)}" data-target="${escapeHtml(targetId)}">$${escapeHtml(prefix + field)}</button>`).join("")}
      </div>
    </div>`).join("");
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  textarea.setRangeText(text, start, end, "end");
  textarea.focus();
  outputDirty = true;
}

function renderOutput(data) {
  lastOutput = data;
  if (!outputDirty) {
    $("quotePayload").value = data.quote_payload || "";
    $("tradePayload").value = data.trade_payload || "";
  }
  renderFieldPalette("quoteFields", data.fields?.quote || {}, "quotePayload", "");
  renderFieldPalette("tradeFields", data.fields?.trade || {}, "tradePayload", "");
}

async function loadOutput() {
  try {
    renderOutput(await apiGet("output/overview"));
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

async function previewTemplate(kind) {
  const target = kind === "trade" ? $("tradePayload") : $("quotePayload");
  const preview = kind === "trade" ? $("tradePreview") : $("quotePreview");
  try {
    const result = await apiPost("output/preview", { kind, template: target.value });
    const notes = [];
    if (result.error) notes.push(result.error);
    if (result.unknown_fields?.length) notes.push(`未知字段：${result.unknown_fields.join("、")}`);
    if (result.note) notes.push(result.note);
    preview.textContent = [notes.join("\n"), result.text || (result.images?.length ? `[图片] ${result.images.length} 张` : "")].filter(Boolean).join("\n\n") || "无可见内容";
    preview.classList.toggle("error", Boolean(result.error));
  } catch (error) {
    preview.textContent = apiError(error);
    preview.classList.add("error");
  }
}

async function saveOutput() {
  try {
    const data = await apiPost("output/save", {
      quote_payload: $("quotePayload").value,
      trade_payload: $("tradePayload").value,
    });
    outputDirty = false;
    renderOutput(data);
    showToast(data.message || "输出模板已保存", "success");
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

document.addEventListener("click", (event) => {
  const chip = event.target.closest(".field-chip[data-field]");
  if (!chip) return;
  const target = $(chip.dataset.target);
  if (target) insertAtCursor(target, chip.dataset.field);
});
$("quotePayload").addEventListener("input", () => { outputDirty = true; });
$("tradePayload").addEventListener("input", () => { outputDirty = true; });
$("previewQuoteBtn").addEventListener("click", () => previewTemplate("quote"));
$("previewTradeBtn").addEventListener("click", () => previewTemplate("trade"));
$("saveOutputBtn").addEventListener("click", saveOutput);
$("resetQuoteBtn").addEventListener("click", () => {
  $("quotePayload").value = lastOutput?.defaults?.quote_payload || "";
  outputDirty = true;
});
$("resetTradeBtn").addEventListener("click", () => {
  $("tradePayload").value = lastOutput?.defaults?.trade_payload || "";
  outputDirty = true;
});

// ---------------------------------------------------------------- 自动交易
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
        ? actionButton("切换到模拟盘", "disarm", rule.rule_id, "secondary")
        : actionButton("切换到实盘", "arm", rule.rule_id, "danger"),
      !rule.live ? actionButton("完整模拟演练", "drill", rule.rule_id, "secondary") : "",
      hasOrders ? actionButton("撤销自动订单", "cancel_orders", rule.rule_id, "danger") : "",
      actionButton("删除", "delete", rule.rule_id, "ghost"),
    ].join("");
    return `
      <article class="rule-card" data-rule-card="${escapeHtml(rule.rule_id)}">
        <div class="rule-top"><div><div class="rule-title">
          <strong>${escapeHtml(rule.title)}</strong>
          <span class="pill ${rule.live ? "live" : ""}">${rule.live ? "实盘" : "模拟"}</span>
          <span class="pill ${rule.enabled ? "" : "off"}">${rule.enabled ? "启用" : "停用"}</span>
          <span class="pill">${escapeHtml(phase)}</span>
        </div><p class="muted">${escapeHtml(rule.rule_id)} · ${escapeHtml(rule.mode)} · 完成 ${runtime.cycle_count || 0} 轮</p></div></div>
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

function logEventHtml(item) {
  return `
    <div class="stack-item" data-log-id="${escapeHtml(item.event_id || item.timestamp)}">
      <strong>${escapeHtml(item.message)}</strong>
      <small>${escapeHtml(item.occurred_at || formatTimestamp(item.timestamp))} · ${escapeHtml(item.rule_id || "-")} · ${escapeHtml(item.kind || "event")}</small>
    </div>`;
}

function renderLogEvents(events, { reset = false } = {}) {
  const container = $("logsList");
  if (reset) {
    tradeLogIds.clear();
    tradeLogCursor = 0;
    container.innerHTML = '<p class="muted empty">暂无日志</p>';
  }
  const fresh = events.filter((item) => {
    const key = String(item.event_id || `${item.timestamp}:${item.rule_id}:${item.kind}:${item.message}`);
    if (tradeLogIds.has(key)) return false;
    tradeLogIds.add(key);
    return true;
  });
  if (!fresh.length) return;
  container.querySelector(".empty")?.remove();
  container.insertAdjacentHTML("afterbegin", fresh.slice().reverse().map(logEventHtml).join(""));
}

async function loadTradeLogs({ reset = false } = {}) {
  try {
    const data = await apiGet("trade/logs", { after: reset ? 0 : tradeLogCursor, limit: 100 });
    renderLogEvents(data.events || [], { reset });
    if (data.next_cursor) tradeLogCursor = Number(data.next_cursor);
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

function renderTrade(data) {
  lastOverview = data;
  $("masterState").textContent = data.settings.enabled ? "已开启" : "未开启";
  $("exposure").textContent = `${data.exposure} GJN`;
  $("budgetRemaining").textContent = `${data.budget_remaining} GJN`;
  $("dailySpent").textContent = `${data.ledger.daily_spent} GJN`;
  fillSettings(data.settings);
  renderRules(data.rules || []);
  renderOrders(data.orders || []);
}

async function refreshTrade({ resetLogs = false } = {}) {
  try {
    renderTrade(await apiGet("trade/overview"));
    await loadTradeLogs({ reset: resetLogs });
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
  $("ruleForm").scrollIntoView({ behavior: "smooth", block: "start" });
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
    await refreshTrade();
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

async function drillRule(ruleId) {
  try {
    const result = await apiPost("trade/rules/drill", { rule_id: ruleId });
    const lines = [
      `演练编号：${result.drill_id}`,
      `规则：${result.rule_title}（${result.rule_id}）`,
      ...(result.steps || []).map((step) => `${step.title}：${step.detail}`),
      `模拟盈亏：${result.summary?.profit || "0.00"} GJN`,
    ];
    await openModal({
      title: "完整模拟演练结果",
      message: lines.join("\n"),
      confirmText: "关闭",
    });
    await refreshTrade();
  } catch (error) {
    showToast(apiError(error), "error");
  }
}

async function runRuleAction(action, ruleId) {
  if (action === "edit") return editRule(ruleId);
  if (action === "arm") return armRule(ruleId);
  if (action === "drill") return drillRule(ruleId);
  if (action === "disarm") {
    const confirmed = await openModal({
      title: "切换到模拟盘",
      message: `确认将规则 ${ruleId} 切换为模拟盘？存在持仓或自动挂单时操作会被拒绝。`,
    });
    if (confirmed === null) return;
  }
  if (action === "delete") {
    const confirmed = await openModal({ title: "删除规则", message: `确认删除规则 ${ruleId}？` });
    if (confirmed === null) return;
  }
  if (action === "cancel_orders") {
    const confirmed = await openModal({ title: "撤销自动订单", message: `确认撤销 ${ruleId} 的全部自动订单并暂停规则？` });
    if (confirmed === null) return;
  }
  try {
    await apiPost("trade/rules/action", { rule_id: ruleId, action });
    showToast("操作完成", "success");
    await refreshTrade();
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
    await refreshTrade();
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
    await refreshTrade();
  } catch (error) {
    showToast(apiError(error), "error");
  }
});

$("resetRuleBtn").addEventListener("click", () => {
  $("ruleUrl").disabled = false;
  resetRuleForm();
});

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
    await refreshTrade();
  } catch (error) {
    showToast(apiError(error), "error");
  }
});

$("refreshBtn").addEventListener("click", async () => {
  await loadAccount();
  if (activeTab === "trade") await refreshTrade({ resetLogs: true });
  if (activeTab === "monitor") await loadMonitor();
  if (activeTab === "output") await loadOutput();
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
document.title = bridge.t("views.trade.title", "Gaijin Market 控制台");
await loadAccount();
await loadMonitor();
if (activeTab === "trade") await refreshTrade({ resetLogs: true });
setInterval(() => {
  void loadAccount();
  if (activeTab === "trade") void refreshTrade();
}, 3000);
bridge.onContext(() => {
  document.title = bridge.t("views.trade.title", "Gaijin Market 控制台");
});
