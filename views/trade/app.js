import { createDepthChart } from "./depth-chart.mjs";

const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
let lastOverview = null;
let lastMonitor = null;
let lastOutput = null;
let lastAccount = null;
let activeTab = "monitor";
let toastTimer = null;
let modalResolver = null;
let modalReturnFocus = null;
let outputDirty = false;
let accountDirty = false;
let tradeLogCursor = 0;
const tradeLogIds = new Set();
let workbenchRuleId = "";
let workbenchData = null;
let workbenchReturnFocus = null;
let workbenchPollBusy = false;
let workbenchChart = null;
let bookConsoleSide = "buy";
let editingBookLevel = null;
let highlightedBookLevel = null;
let bookHighlightTimer = null;
let bookOperationBusy = false;
const workbenchBook = { sell: [], buy: [] };

async function runWithBusy(button, label, task) {
  if (!button || button.dataset.busy === "1") return undefined;
  const hadFocus = document.activeElement === button;
  const returnSelector = focusSelector(button);
  const original = {
    text: button.textContent,
    html: button.innerHTML,
    disabled: button.disabled,
    ariaLabel: button.getAttribute("aria-label"),
  };
  button.dataset.busy = "1";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.setAttribute("aria-label", label);
  button.textContent = label;
  try {
    return await task();
  } finally {
    button.dataset.busy = "";
    button.disabled = original.disabled;
    button.removeAttribute("aria-busy");
    if (original.ariaLabel === null) button.removeAttribute("aria-label");
    else button.setAttribute("aria-label", original.ariaLabel);
    button.innerHTML = original.html;
    if (hadFocus) {
      setTimeout(() => {
        const target = returnSelector ? document.querySelector(returnSelector) : button;
        if (target instanceof HTMLElement && !target.disabled) target.focus();
      }, 0);
    }
  }
}

function formSubmitter(event) {
  return event.submitter || event.currentTarget.querySelector('button[type="submit"]');
}

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

function cssEscape(value) {
  const text = String(value ?? "");
  return window.CSS?.escape ? CSS.escape(text) : text.replace(/["\\]/g, "\\$&");
}

function focusSelector(element) {
  if (!(element instanceof Element) || element === document.body) return "";
  if (element.id) return `#${cssEscape(element.id)}`;
  const { action, rule, subAction, market, goTab } = element.dataset;
  if (action && rule) {
    return `button[data-action="${cssEscape(action)}"][data-rule="${cssEscape(rule)}"]`;
  }
  if (subAction && market) {
    return `button[data-sub-action="${cssEscape(subAction)}"][data-market="${cssEscape(market)}"]`;
  }
  if (subAction) return `button[data-sub-action="${cssEscape(subAction)}"]`;
  if (goTab) return `button[data-go-tab="${cssEscape(goTab)}"]`;
  return "";
}

function focusSelectorTarget(selector) {
  if (!selector) return null;
  const target = document.querySelector(selector);
  return target instanceof HTMLElement && !target.disabled ? target : null;
}

function showToast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`.trim();
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  el.setAttribute("aria-atomic", "true");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4800);
}

function openModal({ title, message, requireText = false, placeholder = "", confirmText = "确认" }) {
  if (modalResolver) modalResolver(null);
  modalReturnFocus = document.activeElement;
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
  const returnFocus = modalReturnFocus;
  modalReturnFocus = null;
  if (returnFocus?.isConnected) setTimeout(() => returnFocus.focus(), 0);
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

function activateTab(name, { focus = false } = {}) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus();
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
  if (name === "monitor") void loadMonitor();
  if (name === "account") void loadAccount();
  if (name === "output") void loadOutput();
  if (name === "trade") void refreshTrade({ resetLogs: true });
}

const tabButtons = [...document.querySelectorAll(".tab")];
tabButtons.forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = tabButtons.indexOf(button);
    let next = current;
    if (event.key === "ArrowLeft") next = (current - 1 + tabButtons.length) % tabButtons.length;
    if (event.key === "ArrowRight") next = (current + 1) % tabButtons.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabButtons.length - 1;
    activateTab(tabButtons[next].dataset.tab, { focus: true });
  });
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
    return true;
  } catch (error) {
    showToast(apiError(error), "error");
    return false;
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
  await runWithBusy(formSubmitter(event), "保存中…", () => saveAccount());
});
$("loginStartBtn").addEventListener("click", () => {
  void runWithBusy($("loginStartBtn"), "登录中…", startLogin);
});
$("loginCodeBtn").addEventListener("click", () => {
  void runWithBusy($("loginCodeBtn"), "提交中…", submitLoginCode);
});
$("accountRefreshBtn").addEventListener("click", () => {
  void runWithBusy($("accountRefreshBtn"), "刷新中…", async () => {
    accountDirty = false;
    if (await loadAccount()) showToast("登录状态已刷新", "success");
  });
});
$("loginCode").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("loginCodeBtn").click();
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

async function saveSubscription(payload, button = null) {
  const persist = async () => {
    try {
      const data = await apiPost("monitor/subscriptions/save", payload);
      renderMonitor(data);
      showToast(data.message || "订阅已保存", "success");
      return true;
    } catch (error) {
      showToast(apiError(error), "error");
      return false;
    }
  };
  return button ? runWithBusy(button, "保存中…", persist) : persist();
}

async function deleteSubscription(uid, target, market = "", button = null) {
  const confirmed = await openModal({
    title: market ? "删除订阅物品" : "删除订阅组",
    message: market ? `确认删除物品 ${market}？` : `确认删除 ${uid} 在 ${target} 的整组订阅？`,
  });
  if (confirmed === null) return;
  const remove = async () => {
    try {
      const data = await apiPost("monitor/subscriptions/delete", { uid, target_umo: target, market_name: market });
      renderMonitor(data);
      showToast(data.message || "订阅已删除", "success");
    } catch (error) {
      showToast(apiError(error), "error");
    }
  };
  await (button ? runWithBusy(button, "删除中…", remove) : remove());
}

async function toggleSubscriptionGroup(uid, target, button = null) {
  const group = (lastMonitor?.groups || []).find((item) => item.uid === uid && item.target_umo === target);
  if (!group) return;
  await saveSubscription(
    {
      original_uid: group.uid,
      original_target: group.target_umo,
      uid: group.uid,
      target_umo: group.target_umo,
      enabled: !group.enabled,
      interval: group.interval,
      mode: group.mode,
      items: (group.items || []).map((item) => ({ url: item.url, display_name: item.display_name })),
    },
    button,
  );
}

$("subscriptionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ok = await saveSubscription(subscriptionPayloadFromForm(), formSubmitter(event));
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
  if (action === "toggle") void toggleSubscriptionGroup(uid, target, button);
  if (action === "delete-group") void deleteSubscription(uid, target, "", button);
  if (action === "delete-item") void deleteSubscription(uid, target, button.dataset.market || "", button);
});

// ---------------------------------------------------------------- 输出模板
function renderFieldPalette(elementId, groups, targetId, prefix = "") {
  const container = $(elementId);
  container.innerHTML = Object.entries(groups || {}).map(([groupName, fields]) => `
    <div class="field-group">
      <strong>${escapeHtml(groupName)}</strong>
      <div class="field-chips">
        ${Object.keys(fields).map((field) => `<button class="field-chip" type="button" data-field="$${escapeHtml(prefix + field)}" data-target="${escapeHtml(targetId)}">$${escapeHtml(prefix + field)}</button>`).join("")}
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

async function previewTemplate(kind, button = null) {
  const target = kind === "trade" ? $("tradePayload") : $("quotePayload");
  const preview = kind === "trade" ? $("tradePreview") : $("quotePreview");
  const run = async () => {
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
  };
  await (button ? runWithBusy(button, "校验中…", run) : run());
}

async function saveOutput(button = null) {
  const run = async () => {
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
  };
  await (button ? runWithBusy(button, "保存中…", run) : run());
}

document.addEventListener("click", (event) => {
  const chip = event.target.closest(".field-chip[data-field]");
  if (!chip) return;
  const target = $(chip.dataset.target);
  if (target) insertAtCursor(target, chip.dataset.field);
});
$("quotePayload").addEventListener("input", () => { outputDirty = true; });
$("tradePayload").addEventListener("input", () => { outputDirty = true; });
$("previewQuoteBtn").addEventListener("click", () => previewTemplate("quote", $("previewQuoteBtn")));
$("previewTradeBtn").addEventListener("click", () => previewTemplate("trade", $("previewTradeBtn")));
$("saveOutputBtn").addEventListener("click", () => saveOutput($("saveOutputBtn")));
$("resetQuoteBtn").addEventListener("click", () => {
  $("quotePayload").value = lastOutput?.defaults?.quote_payload || "";
  outputDirty = true;
});
$("resetTradeBtn").addEventListener("click", () => {
  $("tradePayload").value = lastOutput?.defaults?.trade_payload || "";
  outputDirty = true;
});

// ---------------------------------------------------------------- 模拟工作台
function numberText(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function renderWorkbenchStatus(data) {
  workbenchData = data;
  const rule = data.rule || {};
  const runtime = data.runtime || rule.runtime || {};
  $("workbenchSubtitle").textContent = `${rule.title || rule.market_name || "--"} · ${rule.rule_id || "--"} · ${rule.mode || "--"}`;
  $("workbenchPhase").textContent = phaseLabels[runtime.phase] || runtime.phase || "未知";
  const items = [
    ["模拟/实盘", rule.live ? "实盘" : "模拟盘"],
    ["规则状态", rule.enabled ? "启用" : "暂停"],
    ["持仓", `${numberText(runtime.held_quantity)} / ${numberText(rule.quantity)}`],
    ["已挂售", numberText(runtime.listed_quantity)],
    ["当前成本", `${(Number(runtime.cost_basis_raw || 0) / 10000).toFixed(2)} GJN`],
    ["累计到账", `${(Number(runtime.proceeds_raw || 0) / 10000).toFixed(2)} GJN`],
    ["完成轮次", numberText(runtime.cycle_count)],
    ["最后错误", runtime.last_error || "无"],
  ];
  $("workbenchStatusGrid").innerHTML = items
    .map(([label, value]) => `<div class="status-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`)
    .join("");
  $("workbenchPause").disabled = !rule.enabled;
  $("workbenchResume").disabled = Boolean(rule.enabled);
}

function bookSourceText(source) {
  return {
    live: "当前跟随真实行情；任意盘口修改都会立即保存为模拟覆盖。",
    manual: "已保存的模拟盘口；该规则只使用这份盘口。",
    generated: "按规则参数生成的盘口；已保存为当前模拟覆盖。",
  }[source] || "盘口状态";
}

function setBookActionStatus(message, detail = "", kind = "") {
  const strip = document.querySelector(".book-action-strip");
  strip.classList.remove("success", "error");
  if (kind) strip.classList.add(kind);
  $("bookActionStatus").textContent = message;
  $("bookActionDetail").textContent = detail || "正在等待新操作…";
}

function normalizeBookLevel(level) {
  return {
    price: String(level?.price ?? ""),
    price_raw: Number(level?.price_raw || Math.round(Number(level?.price || 0) * 10000)),
    quantity: String(level?.quantity ?? ""),
  };
}

function renderLevelList(side) {
  const list = $(side === "sell" ? "sellLevelList" : "buyLevelList");
  const levels = workbenchBook[side];
  if (!levels.length) {
    list.innerHTML = '<p class="level-empty">暂无档位</p>';
    return;
  }
  let cumulative = 0;
  list.innerHTML = levels.map((level, index) => {
    const quantity = Number(level.quantity || 0);
    cumulative += Number.isFinite(quantity) ? Math.max(0, quantity) : 0;
    const flash = highlightedBookLevel?.side === side && highlightedBookLevel?.priceRaw === level.price_raw;
    return `<div class="level-row ${flash ? "flash" : ""}" role="listitem" data-level-side="${side}" data-level-index="${index}" data-price-raw="${escapeHtml(level.price_raw)}">
      <button class="level-main" type="button" data-edit-level="${side}" data-index="${index}" aria-label="编辑${side === "buy" ? "买单" : "卖单"} ${escapeHtml(level.price)} GJN，数量 ${escapeHtml(level.quantity)}">
        <b>${escapeHtml(level.price)} GJN</b><strong>× ${escapeHtml(level.quantity)}</strong>
        <span>累计 ${numberText(cumulative)}</span><span>约 ${(Number(level.price || 0) * quantity).toFixed(2)} GJN</span>
      </button>
      <button class="level-delete" type="button" data-delete-level="${side}" data-index="${index}" aria-label="删除${side === "buy" ? "买单" : "卖单"} ${escapeHtml(level.price)} GJN">×</button>
    </div>`;
  }).join("");
}

function renderBook(book) {
  workbenchBook.sell = (book?.sell_levels || []).map(normalizeBookLevel).sort((a, b) => Number(a.price) - Number(b.price));
  workbenchBook.buy = (book?.buy_levels || []).map(normalizeBookLevel).sort((a, b) => Number(b.price) - Number(a.price));
  renderLevelList("sell");
  renderLevelList("buy");
  $("bestSell").textContent = book?.best_sell ? `${book.best_sell} GJN` : "无";
  $("bestBuy").textContent = book?.best_buy ? `${book.best_buy} GJN` : "无";
  $("bookSpread").textContent = book?.spread ? `${book.spread} GJN` : "无";
  $("sellDepth").textContent = `${workbenchBook.sell.length} 档 / 深度 ${numberText(book?.sell_depth)}`;
  $("buyDepth").textContent = `${workbenchBook.buy.length} 档 / 深度 ${numberText(book?.buy_depth)}`;
  $("bookSource").textContent = bookSourceText(workbenchData?.book_source);
  if (workbenchChart) workbenchChart.render(book || {}, workbenchData?.rule || {}, { highlight: highlightedBookLevel });
}

function sellerNet(price) {
  return Math.floor(Number(price || 0) * 0.85 * 100) / 100;
}

function updateOrderPreview() {
  const price = Number($("orderPrice").value || 0);
  const quantity = Math.max(0, Number($("orderQuantity").value || 0));
  const total = price * quantity;
  if (bookConsoleSide === "sell") {
    $("orderPreviewLabel").textContent = "预计到手（税后）";
    $("orderPreviewValue").textContent = `${sellerNet(total).toFixed(2)} GJN`;
    $("orderPreviewHint").textContent = `挂单总额 ${total.toFixed(2)} GJN，固定扣除 15% 手续费。`;
  } else {
    $("orderPreviewLabel").textContent = "预计支出";
    $("orderPreviewValue").textContent = `${total.toFixed(2)} GJN`;
    $("orderPreviewHint").textContent = "买入档位会增加模拟市场的求购深度。";
  }
}

function syncOrderConsole() {
  const isBuy = bookConsoleSide === "buy";
  $("orderSideBuy").classList.toggle("active", isBuy);
  $("orderSideSell").classList.toggle("active", !isBuy);
  $("orderSideBuy").setAttribute("aria-pressed", isBuy ? "true" : "false");
  $("orderSideSell").setAttribute("aria-pressed", isBuy ? "false" : "true");
  $("orderConsoleMode").textContent = editingBookLevel ? "编辑" : "新增";
  $("bookOrderCancelEdit").hidden = !editingBookLevel;
  $("bookOrderSubmit").textContent = editingBookLevel
    ? `更新${isBuy ? "买单" : "卖单"}`
    : `添加${isBuy ? "买单" : "卖单"}`;
  updateOrderPreview();
}

function defaultOrderPrice(side) {
  if (side === "buy") {
    return workbenchBook.buy[0]?.price || workbenchData?.rule?.buy_price || "";
  }
  return workbenchBook.sell[0]?.price || workbenchData?.rule?.sell_gross_price || "";
}

function resetOrderConsole() {
  editingBookLevel = null;
  $("orderPrice").value = defaultOrderPrice(bookConsoleSide);
  $("orderQuantity").value = String(workbenchData?.rule?.quantity || 1);
  showInlineMessage("bookOrderMessage", "", "");
  syncOrderConsole();
}

function setConsoleSide(side, { reset = true } = {}) {
  bookConsoleSide = side === "sell" ? "sell" : "buy";
  if (reset) {
    editingBookLevel = null;
    $("orderPrice").value = defaultOrderPrice(bookConsoleSide);
    $("orderQuantity").value = String(workbenchData?.rule?.quantity || 1);
    showInlineMessage("bookOrderMessage", "", "");
  }
  syncOrderConsole();
}

function beginEditLevel(side, index) {
  const level = workbenchBook[side]?.[Number(index)];
  if (!level) return;
  editingBookLevel = { side, index: Number(index) };
  bookConsoleSide = side;
  $("orderPrice").value = level.price;
  $("orderQuantity").value = level.quantity;
  showInlineMessage("bookOrderMessage", "", "");
  syncOrderConsole();
  setBookActionStatus("正在编辑盘口档位", `${side === "buy" ? "买单" : "卖单"} ${level.price} GJN × ${level.quantity}`);
  $("orderConsoleTitle").scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(() => $("orderPrice").focus(), 0);
}

function ensureWorkbenchChart() {
  if (workbenchChart) return workbenchChart;
  workbenchChart = createDepthChart({
    svg: $("depthChart"),
    tooltip: $("depthChartTooltip"),
    empty: $("depthChartEmpty"),
    summary: $("depthChartSummary"),
  });
  return workbenchChart;
}

function renderWorkbench(data) {
  workbenchData = data;
  renderWorkbenchStatus(data);
  renderBook(data.book || {});
  if (data.message) {
    const failed = Boolean(data.runtime?.last_error);
    setBookActionStatus(data.message, failed ? data.runtime.last_error : "盘口状态已同步", failed ? "error" : "success");
    showInlineMessage("workbenchMessage", data.message, failed ? "error" : "ok");
  }
}

async function refreshWorkbench() {
  if (!workbenchRuleId || $("workbenchBackdrop").hidden || workbenchPollBusy) return;
  workbenchPollBusy = true;
  try {
    const data = await apiGet("trade/rules/workbench", { rule_id: workbenchRuleId });
    renderWorkbench(data);
  } catch (error) {
    setBookActionStatus("读取盘口失败", apiError(error), "error");
    showInlineMessage("workbenchMessage", apiError(error), "error");
  } finally {
    workbenchPollBusy = false;
  }
}

async function openWorkbench(ruleId, trigger = null) {
  const rule = lastOverview?.rules?.find((item) => item.rule_id === ruleId);
  if (!rule) {
    showToast("找不到该规则", "error");
    return;
  }
  if (rule.live) {
    showToast("请先把规则切换到模拟盘", "error");
    return;
  }
  workbenchRuleId = ruleId;
  workbenchReturnFocus = trigger || document.activeElement;
  editingBookLevel = null;
  highlightedBookLevel = null;
  clearTimeout(bookHighlightTimer);
  $("workbenchBackdrop").hidden = false;
  $("workbenchTitle").textContent = `${rule.title} · 模拟交易工作台`;
  $("workbenchSubtitle").textContent = `${rule.rule_id} · 正在读取盘口…`;
  $("workbenchStatusGrid").innerHTML = '<p class="muted">读取中…</p>';
  showInlineMessage("workbenchMessage", "", "");
  setBookActionStatus("正在等待新操作…", "通过下方控制台调整模拟盘口，每次修改都会立即执行纸面检查。");
  ensureWorkbenchChart();
  document.body.classList.add("workbench-open");
  setTimeout(() => $("workbenchClose").focus(), 0);
  await refreshWorkbench();
  setConsoleSide("buy");
}

function closeWorkbench() {
  if ($("workbenchBackdrop").hidden) return;
  $("workbenchBackdrop").hidden = true;
  document.body.classList.remove("workbench-open");
  const returning = workbenchReturnFocus;
  workbenchReturnFocus = null;
  workbenchRuleId = "";
  editingBookLevel = null;
  highlightedBookLevel = null;
  clearTimeout(bookHighlightTimer);
  if (returning?.isConnected) setTimeout(() => returning.focus(), 0);
}

async function runWorkbenchRuleAction(action, button) {
  const run = async () => {
    try {
      await apiPost("trade/rules/action", { rule_id: workbenchRuleId, action });
      showToast(action === "pause" ? "规则已暂停" : "规则已恢复", "success");
      await refreshWorkbench();
      await refreshTrade();
    } catch (error) {
      showToast(apiError(error), "error");
    }
  };
  await (button ? runWithBusy(button, "处理中…", run) : run());
}

function collectBookSide(side, source = workbenchBook[side]) {
  return source
    .map((level) => ({ price: String(level.price || "").trim(), quantity: String(level.quantity || "").trim() }))
    .filter((level) => level.price || level.quantity);
}

function sortBookLevels(side, levels) {
  return [...levels].sort((a, b) => side === "buy" ? Number(b.price) - Number(a.price) : Number(a.price) - Number(b.price));
}

function setBookBusy(busy) {
  bookOperationBusy = Boolean(busy);
  const root = document.querySelector(".workbench-market");
  root.querySelectorAll("button, input").forEach((element) => {
    if (busy) {
      element.dataset.bookWasDisabled = element.disabled ? "1" : "0";
      element.disabled = true;
    } else {
      element.disabled = element.dataset.bookWasDisabled === "1";
      delete element.dataset.bookWasDisabled;
    }
  });
  if (busy) $("bookOrderSubmit").textContent = "执行中…";
  else syncOrderConsole();
}

function clearBookHighlightSoon() {
  clearTimeout(bookHighlightTimer);
  if (!highlightedBookLevel) return;
  bookHighlightTimer = setTimeout(() => {
    highlightedBookLevel = null;
    renderLevelList("buy");
    renderLevelList("sell");
    if (workbenchChart) workbenchChart.render(workbenchData?.book || {}, workbenchData?.rule || {}, { highlight: null });
  }, 1600);
}

async function runBookMutation(task, { successMessage, detail = "", highlight = null } = {}) {
  if (bookOperationBusy) return null;
  setBookBusy(true);
  showInlineMessage("bookOrderMessage", "", "");
  try {
    highlightedBookLevel = highlight;
    const data = await task();
    renderWorkbench(data);
    setBookActionStatus(successMessage || data.message || "盘口已更新", detail || "已立即执行一次模拟检查", "success");
    showToast(successMessage || data.message || "盘口已更新", "success");
    resetOrderConsole();
    await refreshTrade();
    clearBookHighlightSoon();
    return data;
  } catch (error) {
    const message = apiError(error);
    setBookActionStatus("盘口操作失败", message, "error");
    showInlineMessage("bookOrderMessage", message, "error");
    showToast(message, "error");
    return null;
  } finally {
    setBookBusy(false);
  }
}

function saveBookPayload(buyLevels, sellLevels, message, detail, highlight = null) {
  return runBookMutation(
    () => apiPost("trade/rules/workbench", {
      rule_id: workbenchRuleId,
      action: "save_apply",
      buy_levels: collectBookSide("buy", buyLevels),
      sell_levels: collectBookSide("sell", sellLevels),
    }),
    { successMessage: message, detail, highlight },
  );
}

function showBookOrderError(message) {
  showInlineMessage("bookOrderMessage", message, "error");
  setBookActionStatus("盘口操作未执行", message, "error");
  showToast(message, "error");
}

async function submitBookOrder(event) {
  event.preventDefault();
  if (bookOperationBusy) return;
  const side = bookConsoleSide;
  const rawPrice = $("orderPrice").value.trim();
  const rawQuantity = $("orderQuantity").value.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(rawPrice) || Number(rawPrice) <= 0) {
    showBookOrderError("价格必须大于 0，且最多保留两位小数");
    $("orderPrice").focus();
    return;
  }
  if (!/^\d+$/.test(rawQuantity) || Number(rawQuantity) < 1 || Number(rawQuantity) > 1_000_000) {
    showBookOrderError("数量必须是 1 到 1000000 之间的整数");
    $("orderQuantity").focus();
    return;
  }
  const price = Number(rawPrice);
  const quantity = Number(rawQuantity);
  const priceRaw = Math.round(price * 10000);
  const nextBuy = workbenchBook.buy.map((level) => ({ ...level }));
  const nextSell = workbenchBook.sell.map((level) => ({ ...level }));
  let target = side === "buy" ? nextBuy : nextSell;
  let verb = editingBookLevel ? "更新" : "添加";
  if (editingBookLevel) target.splice(editingBookLevel.index, 1);
  if (target.length >= 100) {
    showBookOrderError("单侧最多允许 100 个档位");
    return;
  }
  if (target.some((level) => Number(level.price_raw) === priceRaw)) {
    showBookOrderError(`同侧已存在 ${price.toFixed(2)} GJN 的档位`);
    $("orderPrice").focus();
    return;
  }
  target.push({ price: price.toFixed(2), price_raw: priceRaw, quantity: String(quantity) });
  if (side === "buy") nextBuy.splice(0, nextBuy.length, ...sortBookLevels("buy", target));
  else nextSell.splice(0, nextSell.length, ...sortBookLevels("sell", target));
  const sideText = side === "buy" ? "买单" : "卖单";
  const message = `已${verb}${sideText}一档 ${price.toFixed(2)} GJN × ${quantity}`;
  await saveBookPayload(nextBuy, nextSell, message, "每次盘口变更都会立即执行模拟检查", { side, priceRaw });
}

async function deleteBookLevel(side, index) {
  if (bookOperationBusy) return;
  const level = workbenchBook[side]?.[Number(index)];
  if (!level) return;
  const sideText = side === "buy" ? "买单" : "卖单";
  const confirmed = await openModal({
    title: `删除${sideText}档位`,
    message: `确认删除${sideText} ${level.price} GJN × ${level.quantity}？删除后会立即执行模拟检查。`,
  });
  if (confirmed === null) return;
  const nextBuy = workbenchBook.buy.map((item) => ({ ...item }));
  const nextSell = workbenchBook.sell.map((item) => ({ ...item }));
  const target = side === "buy" ? nextBuy : nextSell;
  target.splice(Number(index), 1);
  await saveBookPayload(
    nextBuy,
    nextSell,
    `已删除${sideText}一档 ${level.price} GJN × ${level.quantity}`,
    "删除操作已立即执行模拟检查",
  );
}

async function workbenchBookAction(action, button) {
  if (bookOperationBusy) return;
  let confirmed = "";
  if (action === "follow_live") {
    confirmed = await openModal({
      title: "恢复跟随真实行情",
      message: "这会删除当前规则保存的模拟盘口，之后自动检查将重新使用真实行情。",
    });
    if (confirmed === null) return;
  }
  if (action === "follow_live") {
    await runBookMutation(
      () => apiPost("trade/rules/workbench", { rule_id: workbenchRuleId, action }),
      { successMessage: "已恢复跟随真实行情", detail: "下一次检查将使用真实市场盘口" },
    );
    return;
  }
  await runBookMutation(async () => {
    const preview = await apiPost("trade/rules/workbench", { rule_id: workbenchRuleId, action });
    return apiPost("trade/rules/workbench", {
      rule_id: workbenchRuleId,
      action: "save_apply",
      buy_levels: collectBookSide("buy", preview.book?.buy_levels || []),
      sell_levels: collectBookSide("sell", preview.book?.sell_levels || []),
    });
  }, {
    successMessage: action === "load_live" ? "已载入真实盘口并执行模拟检查" : "已生成可成交盘口并执行模拟检查",
    detail: action === "load_live" ? "真实盘口已保存为当前模拟覆盖" : "盘口已按规则阈值生成并保存",
  });
}

$("workbenchClose").addEventListener("click", closeWorkbench);
$("workbenchPause").addEventListener("click", () => void runWorkbenchRuleAction("pause", $("workbenchPause")));
$("workbenchResume").addEventListener("click", () => void runWorkbenchRuleAction("resume", $("workbenchResume")));
$("workbenchRefresh").addEventListener("click", () => void runWithBusy($("workbenchRefresh"), "刷新中…", refreshWorkbench));
$("bookLoadLive").addEventListener("click", () => void workbenchBookAction("load_live", $("bookLoadLive")));
$("bookGenerate").addEventListener("click", () => void workbenchBookAction("generate", $("bookGenerate")));
$("bookFollowLive").addEventListener("click", () => void workbenchBookAction("follow_live", $("bookFollowLive")));
$("orderSideBuy").addEventListener("click", () => setConsoleSide("buy"));
$("orderSideSell").addEventListener("click", () => setConsoleSide("sell"));
$("bookOrderCancelEdit").addEventListener("click", resetOrderConsole);
$("bookOrderForm").addEventListener("submit", (event) => void submitBookOrder(event));
$("orderPrice").addEventListener("input", updateOrderPreview);
$("orderQuantity").addEventListener("input", updateOrderPreview);
$("orderPriceDown").addEventListener("click", () => {
  const value = Math.max(0.01, Number($("orderPrice").value || 0) - 0.01);
  $("orderPrice").value = value.toFixed(2);
  updateOrderPreview();
});
$("orderPriceUp").addEventListener("click", () => {
  const value = Math.max(0.01, Number($("orderPrice").value || 0) + 0.01);
  $("orderPrice").value = value.toFixed(2);
  updateOrderPreview();
});
document.querySelectorAll("[data-quantity]").forEach((button) => {
  button.addEventListener("click", () => {
    const quantity = button.dataset.quantity === "rule" ? workbenchData?.rule?.quantity || 1 : Number(button.dataset.quantity);
    $("orderQuantity").value = String(quantity);
    updateOrderPreview();
  });
});
document.querySelector(".level-board").addEventListener("click", (event) => {
  const open = event.target.closest("button[data-open-order]");
  if (open) {
    setConsoleSide(open.dataset.openOrder);
    $("orderConsoleTitle").scrollIntoView({ behavior: "smooth", block: "nearest" });
    setTimeout(() => $("orderPrice").focus(), 0);
    return;
  }
  const edit = event.target.closest("button[data-edit-level]");
  if (edit) {
    beginEditLevel(edit.dataset.editLevel, Number(edit.dataset.index));
    return;
  }
  const remove = event.target.closest("button[data-delete-level]");
  if (remove) void deleteBookLevel(remove.dataset.deleteLevel, Number(remove.dataset.index));
});

// ---------------------------------------------------------------- 自动交易
function renderGlobalMode(data) {
  const live = data.mode === "live";
  const button = $("globalModeSwitch");
  button.setAttribute("aria-checked", live ? "true" : "false");
  $("globalModeText").textContent = live ? "实盘" : "模拟盘";
  $("globalModeLabel").textContent = live
    ? "实盘许可已开启；仍只会执行已切换为实盘的规则"
    : "模拟盘，不会下真实订单";
  const result = data.last_mode_result || {};
  const skipped = Array.isArray(result.skipped) ? result.skipped : [];
  if (skipped.length) {
    showInlineMessage(
      "globalModeResult",
      `最近一次批量切换跳过 ${skipped.length} 条规则：${skipped.map((item) => `${item.rule_id}（${item.reason}）`).join("；")}`,
      "error",
    );
  } else {
    showInlineMessage("globalModeResult", "", "");
  }
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
  const focusReturn = focusSelector(document.activeElement?.closest("#rulesList button[data-action]"));
  if (!rules.length) {
    container.innerHTML = '<p class="muted empty">暂无规则</p>';
    if (focusReturn) {
      container.tabIndex = -1;
      container.focus();
    }
    return;
  }
  container.innerHTML = rules.map((rule) => {
    const runtime = rule.runtime || {};
    const phase = phaseLabels[runtime.phase] || runtime.phase || "未知";
    const hasOrders = Array.isArray(runtime.orders) && runtime.orders.length > 0;
    const actions = [
      actionButton("编辑", "edit", rule.rule_id),
      actionButton(rule.enabled ? "暂停" : "恢复", rule.enabled ? "pause" : "resume", rule.rule_id),
      `<button class="rule-mode-toggle toggle-button" type="button" role="switch" aria-checked="${rule.live ? "true" : "false"}" data-action="${rule.live ? "disarm" : "arm"}" data-rule="${escapeHtml(rule.rule_id)}" aria-label="切换 ${escapeHtml(rule.rule_id)} 的模拟盘或实盘环境">
        <span class="toggle-track"><span class="toggle-thumb"></span></span><b>${rule.live ? "实盘" : "模拟盘"}</b>
      </button>`,
      !rule.live ? actionButton("进入模拟盘", "workbench", rule.rule_id, "primary") : "",
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
  if (focusReturn) {
    const target = focusSelectorTarget(focusReturn);
    if (target) target.focus();
    else {
      container.tabIndex = -1;
      container.focus();
    }
  }
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
  renderGlobalMode(data);
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

async function armRule(ruleId, button = null) {
  const run = async () => {
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
  };
  await (button ? runWithBusy(button, "处理中…", run) : run());
}

async function drillRule(ruleId, button = null) {
  const run = async () => {
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
  };
  await (button ? runWithBusy(button, "演练中…", run) : run());
}

async function runRuleAction(action, ruleId, button = null) {
  if (action === "edit") return editRule(ruleId);
  if (action === "workbench") return openWorkbench(ruleId, button);
  if (action === "arm") return armRule(ruleId, button);
  if (action === "drill") return drillRule(ruleId, button);
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
  const run = async () => {
    try {
      await apiPost("trade/rules/action", { rule_id: ruleId, action });
      showToast("操作完成", "success");
      await refreshTrade();
    } catch (error) {
      showToast(apiError(error), "error");
    }
  };
  await (button ? runWithBusy(button, "处理中…", run) : run());
}

$("rulesList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  void runRuleAction(button.dataset.action, button.dataset.rule, button);
});

$("globalModeSwitch").addEventListener("click", async () => {
  const live = lastOverview?.mode === "live";
  const target = live ? "paper" : "live";
  const rules = lastOverview?.rules || [];
  const summary = rules.length
    ? rules.map((rule) => `${rule.rule_id}（${rule.live ? "当前实盘" : "当前模拟"}）`).join("\n")
    : "当前没有交易规则。";
  const confirmation = await openModal({
    title: live ? "关闭实盘总开关" : "开启实盘总开关",
    message: live
      ? `将尝试把所有实盘规则切回模拟盘；有持仓或挂单的规则会被跳过。\n\n${summary}`
      : `将尝试把所有安全可切换的规则批量切换为实盘。真实资金风险随后仍然存在。\n\n${summary}`,
    requireText: !live,
    placeholder: "LIVE",
    confirmText: live ? "确认关闭实盘" : "确认批量切换实盘",
  });
  if (confirmation === null) return;
  await runWithBusy($("globalModeSwitch"), live ? "切换中…" : "确认实盘…", async () => {
    try {
      const data = await apiPost("trade/mode", { mode: target, confirmation });
      renderTrade(data.overview);
      const skipped = data.result?.skipped || [];
      showToast(
        skipped.length ? `环境已切换，跳过 ${skipped.length} 条规则` : `全局交易环境已切换为${target === "live" ? "实盘" : "模拟盘"}`,
        skipped.length ? "error" : "success",
      );
    } catch (error) {
      showToast(apiError(error), "error");
    }
  });
});

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runWithBusy(formSubmitter(event), "保存中…", async () => {
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
  await runWithBusy(formSubmitter(event), "保存中…", async () => {
    try {
      const saved = await apiPost("trade/rules/save", payload);
      showToast(editId ? "规则已更新" : "规则已创建，正在打开模拟工作台", "success");
      $("ruleUrl").disabled = false;
      resetRuleForm();
      await refreshTrade();
      if (!editId && saved?.rule?.rule_id) await openWorkbench(saved.rule.rule_id);
    } catch (error) {
      showToast(apiError(error), "error");
    }
  });
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
  await runWithBusy($("stopAllBtn"), "急停中…", async () => {
    try {
      await apiPost("trade/stop-all", { confirmation });
      showToast("自动交易已急停", "success");
      await refreshTrade();
    } catch (error) {
      showToast(apiError(error), "error");
    }
  });
});

$("refreshBtn").addEventListener("click", async () => {
  await runWithBusy($("refreshBtn"), "刷新中…", async () => {
    await loadAccount();
    if (activeTab === "trade") await refreshTrade({ resetLogs: true });
    if (activeTab === "monitor") await loadMonitor();
    if (activeTab === "output") await loadOutput();
  });
});

$("workbenchBackdrop").addEventListener("click", (event) => {
  if (event.target === $("workbenchBackdrop")) closeWorkbench();
});
$("workbenchBackdrop").addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const focusables = [...$("workbenchBackdrop").querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled)")]
    .filter((element) => element.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
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
$("modalBackdrop").addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const focusables = [...$("modalBackdrop").querySelectorAll("button:not(:disabled), input:not(:disabled)")]
    .filter((element) => element.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("modalBackdrop").hidden) closeModal(null);
  else if (!$("workbenchBackdrop").hidden) closeWorkbench();
});

await bridge.ready();
document.title = bridge.t("views.trade.title", "Gaijin Market 控制台");
await loadAccount();
await loadMonitor();
if (activeTab === "trade") await refreshTrade({ resetLogs: true });
setInterval(() => {
  void loadAccount();
  if (activeTab === "trade") void refreshTrade();
  if (!$("workbenchBackdrop").hidden) void refreshWorkbench();
}, 3000);
bridge.onContext(() => {
  document.title = bridge.t("views.trade.title", "Gaijin Market 控制台");
});
