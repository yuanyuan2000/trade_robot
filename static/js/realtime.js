const rt = {
  tasks: [], strategies: [], channels: [], current: null,
  activePanel: "dashboard", dashboard: null,
  sort: null, listRefreshInFlight: false, dashboardInFlight: false,
  lastListRefreshAt: 0, refreshTimer: null, dashboardTimer: null,
  logs: { events: [], notifications: [], kind: "all", beforeEventId: null, beforeNotificationId: null },
};

const rtListPage = document.getElementById("realtime-list-page");
const rtDetailPage = document.getElementById("realtime-detail-page");
const rtCards = document.getElementById("realtime-task-cards");
const rtStatus = document.getElementById("realtime-status");
const rtDetailStatus = document.getElementById("realtime-detail-status");
const rtCreateDialog = document.getElementById("realtime-create-dialog");
const rtChannelDialog = document.getElementById("realtime-channel-dialog");

function rtEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

async function rtJson(response) {
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error?.message || "实时决策请求失败。");
  }
  return payload;
}

function rtSetStatus(message, type = "neutral", detail = false) {
  const element = detail ? rtDetailStatus : rtStatus;
  element.textContent = message;
  element.className = `status ${type}`;
}

function rtDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function rtDuration(started) {
  if (!started) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(started).getTime()) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}小时${m}分${s}秒`;
}

function rtStateLabel(state) {
  return { stopped: "已停止", starting: "启动中", running: "运行中", degraded: "降级运行", stopping: "终止中", error: "异常" }[state] || state;
}

function rtCodeBodyPreview(strategy) {
  const labels = {
    rapid_drop_atr_rotation: "风险节点列出急跌过滤；选标节点列出价格位移、ATR、评分、排名与调仓建议。",
    rapid_drop_wtme_rotation: "风险节点列出急跌过滤；选标节点列出 Rw、Aw、WTME 评分、排名与调仓建议。",
    sevenstar_etf_rotation: "列出长期趋势、R²、短动量、过滤原因、七星评分、排名与目标。",
  };
  return `代码策略默认邮件正文：\n${labels[strategy?.code_key] || "显示策略评分、过滤原因、排名和调仓建议。"}\n\n正式邮件只使用任务候选池，并在事件时点独立获取行情。`;
}

function rtVisualDecisionSchedule(strategy) {
  const definition = strategy?.definition || {};
  const events = (definition.rules || []).filter((rule) => rule.enabled).map((rule) => rule.when);
  if (strategy?.selection_mode === "competition" && definition.competition?.when) events.push(definition.competition.when);
  const describe = (event) => event === "OPEN" ? "OPEN（美东 09:30）" : event === "CLOSE" ? "CLOSE（美东收盘）" : `${event}（美东时间）`;
  const unique = [...new Set(events)];
  return unique.length ? `当前决策时点：${unique.map(describe).join("、")}` : "当前任务没有已启用的决策时点。";
}

async function loadRealtimeStrategies() {
  const payload = await rtJson(await fetch("/api/backtest/strategies"));
  rt.strategies = payload.strategies || [];
  document.getElementById("realtime-create-strategy").innerHTML = rt.strategies.map((strategy) => (
    `<option value="${strategy.id}">${rtEscape(strategy.name)} · ${strategy.design_mode === "code" ? "代码" : "非代码"}</option>`
  )).join("");
}

async function loadRealtimeChannels() {
  const payload = await rtJson(await fetch("/api/realtime/email-channels"));
  rt.channels = payload.channels || [];
  document.getElementById("realtime-channel").innerHTML = `<option value="">未选择</option>${rt.channels.map((channel) => (
    `<option value="${channel.id}">${rtEscape(channel.name)} · ${rtEscape(channel.sender_email)}</option>`
  )).join("")}`;
}

function renderRealtimeCards() {
  document.getElementById("realtime-task-count").textContent = `${rt.tasks.length} 个任务`;
  if (!rt.tasks.length) {
    rtCards.innerHTML = '<div class="realtime-empty">暂无任务，请点击“创建新任务”。</div>';
    return;
  }
  rtCards.innerHTML = rt.tasks.map((task) => {
    const running = ["starting", "running", "degraded"].includes(task.runtime_state);
    const stopping = task.runtime_state === "stopping";
    const competition = task.strategy_snapshot?.selection_mode === "competition";
    const recommendations = task.overview_recommendations || [];
    const recommendationHtml = competition ? `<div class="realtime-card-recommendations">
      <span class="realtime-recommendation-label">推荐</span>
      ${recommendations.length ? recommendations.map((item, index) => `<span class="realtime-recommendation realtime-recommendation-${index + 1}" title="面板第 ${item.rank} 名 · 评分 ${rtEscape(rtFormatMetric(item.score, "number"))}">${rtEscape(item.display_symbol || item.symbol)}</span>`).join("") : '<span class="realtime-recommendation-empty">暂无合格标的</span>'}
    </div>` : "";
    return `<article class="realtime-task-card" data-rt-task-id="${task.id}">
      <h4>#${task.id} ${rtEscape(task.name)}</h4>
      <p>${rtEscape(task.strategy_snapshot?.name || "未知策略")} · revision ${task.source_strategy_revision}</p>
      <p class="realtime-state-${rtEscape(task.runtime_state)}">${rtStateLabel(task.runtime_state)}${task.last_error_message ? ` · ${rtEscape(task.last_error_message)}` : ""}</p>
      ${recommendationHtml}
      <div class="realtime-card-metrics">
        <div class="realtime-card-metric"><span>运行时长</span><strong>${running ? rtDuration(task.run_started_at) : "—"}</strong></div>
        <div class="realtime-card-metric"><span>成功邮件</span><strong>${task.successful_notification_count || 0}</strong></div>
        <div class="realtime-card-metric"><span>下次触发</span><strong>${task.next_event_at ? rtDate(task.next_event_at) : "—"}</strong></div>
      </div>
      <div class="realtime-card-actions">
        <button type="button" data-rt-action="${running ? "stop" : "start"}"${stopping ? " disabled" : ""}>${stopping ? "终止中" : running ? "终止" : "运行"}</button>
        <button type="button" data-rt-action="delete">删除</button>
      </div>
    </article>`;
  }).join("");
}

async function loadRealtimeTasks({ silent = false } = {}) {
  if (rt.listRefreshInFlight) return;
  rt.listRefreshInFlight = true;
  try {
    const payload = await rtJson(await fetch("/api/realtime/tasks"));
    rt.tasks = payload.tasks || [];
    rt.lastListRefreshAt = Date.now();
    renderRealtimeCards();
    if (!silent) rtSetStatus("实时决策任务已加载。", "success");
  } catch (error) {
    rtSetStatus(error.message, "error");
  } finally {
    rt.listRefreshInFlight = false;
  }
}

function refreshRealtimeDetailStatus(task) {
  if (!task || rtDetailPage.hidden) return;
  document.getElementById("realtime-detail-title").textContent = `#${task.id} ${task.name}`;
  document.getElementById("realtime-detail-subtitle").textContent = `${task.strategy_snapshot?.name || "策略"} · ${rtStateLabel(task.runtime_state)}`;
  document.getElementById("realtime-revision").textContent = `任务 revision ${task.revision} · 策略 revision ${task.source_strategy_revision}`;
  const running = ["starting", "running", "degraded"].includes(task.runtime_state);
  const stopping = task.runtime_state === "stopping";
  document.getElementById("realtime-start").hidden = running || stopping;
  document.getElementById("realtime-stop").hidden = !running;
}

async function loadRealtimeTaskStatus() {
  if (!rt.current || rtDetailPage.hidden) return;
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}`));
    rt.current = { ...rt.current, ...payload.task };
    refreshRealtimeDetailStatus(rt.current);
  } catch (error) {
    rtSetStatus(error.message, "error", true);
  }
}

function selectRealtimePanel(panel) {
  rt.activePanel = panel;
  document.querySelectorAll("[data-realtime-panel]").forEach((element) => {
    element.hidden = element.dataset.realtimePanel !== panel;
  });
  document.querySelectorAll("[data-realtime-tab]").forEach((button) => {
    const active = button.dataset.realtimeTab === panel;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (panel === "dashboard") loadRealtimeDashboard();
  if (panel === "logs") loadRealtimeLogs(true);
}

function renderRealtimeDetail() {
  const task = rt.current;
  if (!task) return;
  refreshRealtimeDetailStatus(task);
  document.getElementById("realtime-name").value = task.name;
  document.getElementById("realtime-follow").checked = Boolean(task.follow_strategy);
  document.getElementById("realtime-capital").value = task.settings?.initial_capital ?? 100000;
  document.getElementById("realtime-leverage").value = task.settings?.leverage_multiplier ?? 1;
  document.getElementById("realtime-strategy-summary").textContent = `${task.strategy_snapshot?.name || ""}\n设计模式：${task.strategy_snapshot?.design_mode || ""}\n选标模式：${task.strategy_snapshot?.selection_mode || ""}\n正式候选池：${(task.strategy_snapshot?.definition?.symbols || []).map((item) => item.symbol).join(", ")}`;
  document.getElementById("realtime-definition-json").value = JSON.stringify(task.strategy_snapshot?.definition || {}, null, 2);
  const visual = task.strategy_snapshot?.design_mode === "visual";
  document.getElementById("realtime-panel-script-fields").hidden = !visual;
  document.getElementById("realtime-code-panel-note").hidden = visual;
  document.getElementById("realtime-panel-script").value = task.panel_settings?.script || "";
  document.getElementById("realtime-panel-revision").textContent = visual ? `面板 revision ${task.panel_revision}` : "";
  const notification = task.notification_settings || {};
  document.getElementById("realtime-mail-enabled").checked = Boolean(notification.enabled);
  document.getElementById("realtime-channel").value = notification.channel_id || "";
  document.getElementById("realtime-recipients").value = Array.isArray(notification.recipients) ? notification.recipients.join(", ") : (notification.recipients || "");
  document.getElementById("realtime-subject-template").value = notification.subject_template || "";
  document.getElementById("realtime-body-template").value = notification.body_template || "";
  document.getElementById("realtime-body-template-help").hidden = !visual;
  document.getElementById("realtime-template-current-schedule").textContent = rtVisualDecisionSchedule(task.strategy_snapshot);
  const preview = document.getElementById("realtime-code-body-preview");
  preview.hidden = visual || Boolean(notification.body_template);
  preview.textContent = preview.hidden ? "" : rtCodeBodyPreview(task.strategy_snapshot);
  const running = ["starting", "running", "degraded", "stopping"].includes(task.runtime_state);
  ["realtime-name", "realtime-capital", "realtime-leverage", "realtime-follow", "realtime-definition-json", "realtime-save"].forEach((id) => {
    document.getElementById(id).disabled = running || (id === "realtime-definition-json" && Boolean(task.follow_strategy));
  });
  ["realtime-mail-enabled", "realtime-channel", "realtime-recipients", "realtime-subject-template", "realtime-body-template", "realtime-save-mail"].forEach((id) => {
    document.getElementById(id).disabled = running;
  });
}

async function openRealtimeTask(taskId) {
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${taskId}`));
    rt.current = payload.task;
    rt.dashboard = null;
    rt.sort = null;
    rt.logs = { events: [], notifications: [], kind: "all", beforeEventId: null, beforeNotificationId: null };
    rtListPage.hidden = true;
    rtDetailPage.hidden = false;
    renderRealtimeDetail();
    selectRealtimePanel("dashboard");
    rtSetStatus("面板只读取行情总览内部数据库；正式决策与邮件仍使用任务候选池。", "neutral", true);
    window.clearInterval(rt.dashboardTimer);
    rt.dashboardTimer = window.setInterval(() => {
      if (!rtDetailPage.hidden && rt.activePanel === "dashboard") loadRealtimeDashboard();
    }, 60_000);
  } catch (error) {
    rtSetStatus(error.message, "error", true);
  }
}

function rtFormatMetric(value, format) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  if (format === "boolean") return value ? "是" : "否";
  const number = Number(value);
  if (!Number.isFinite(number)) return rtEscape(value);
  if (format === "percent") return `${(number * 100).toFixed(2)}%`;
  if (format === "price") return number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 6 });
}

function rtSortRows(rows) {
  const sort = rt.sort || { key: "symbol", direction: "asc" };
  return [...rows].sort((left, right) => {
    const read = (row) => sort.key in row ? row[sort.key] : row.metrics?.[sort.key];
    const a = read(left); const b = read(right);
    if (a === null || a === undefined) return b === null || b === undefined ? left.symbol.localeCompare(right.symbol) : 1;
    if (b === null || b === undefined) return -1;
    const comparison = typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b), "zh-CN");
    return (sort.direction === "desc" ? -comparison : comparison) || left.symbol.localeCompare(right.symbol);
  });
}

function rtFilteredDashboardRows() {
  const query = document.getElementById("realtime-dashboard-search").value.trim().toLowerCase();
  const filter = document.getElementById("realtime-dashboard-status-filter").value;
  const candidateOnly = document.getElementById("realtime-dashboard-candidate-only").checked;
  return rtSortRows((rt.dashboard?.rows || []).filter((row) => {
    if (candidateOnly && !row.is_candidate) return false;
    if (query && !`${row.symbol} ${row.display_symbol} ${row.name || ""}`.toLowerCase().includes(query)) return false;
    if (filter === "eligible" && row.status !== "通过") return false;
    if (filter === "filtered" && row.status !== "已过滤") return false;
    if (filter === "signal" && row.status !== "有信号") return false;
    if (filter === "unavailable" && row.status !== "不可计算") return false;
    return true;
  }));
}

function rtSortHeader(key, label, help = "") {
  const active = rt.sort?.key === key;
  const arrow = active ? (rt.sort.direction === "asc" ? "↑" : "↓") : "↕";
  return `<button type="button" class="realtime-sort-button${active ? " is-active" : ""}" data-rt-sort="${rtEscape(key)}">${rtEscape(label)} <span>${arrow}</span></button>${help ? `<span class="realtime-metric-help" tabindex="0" title="${rtEscape(help)}" aria-label="${rtEscape(help)}">?</span>` : ""}`;
}

function renderRealtimeDashboardTable() {
  const dashboard = rt.dashboard;
  if (!dashboard) return;
  const rows = rtFilteredDashboardRows();
  const columns = dashboard.columns || [];
  const headers = [
    ["symbol", "标的", "展示行情总览中的全部标的；蓝点表示属于当前任务候选池。"],
    ["status", "策略状态", "仅代表当前价下的面板观察结果，不等同于正式邮件决策。"],
    ["latest_price", "最新价格", "直接读取行情总览内部数据库。"],
    ...columns.map((column) => [column.key, column.label, column.help]),
    ["rank", "面板排名", "只在行情总览展示范围内排名，不改变正式任务目标。"],
    ["price_updated_at", "更新时间", "内部数据库中该价格的更新时间。"],
  ];
  document.getElementById("realtime-dashboard-table").innerHTML = `<table class="realtime-dashboard-table">
    <thead><tr>${headers.map(([key, label, help]) => `<th>${rtSortHeader(key, label, help)}</th>`).join("")}<th>明细</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td><span class="realtime-symbol-cell">${row.is_candidate ? '<i class="realtime-candidate-dot" title="属于当前任务候选池" aria-label="属于当前任务候选池"></i>' : '<i class="realtime-candidate-dot is-empty" aria-hidden="true"></i>'}<button class="realtime-symbol-link" type="button" data-rt-open-symbol="${rtEscape(row.symbol)}" title="查看 ${rtEscape(row.display_symbol || row.symbol)} K线详情"><strong>${rtEscape(row.display_symbol || row.symbol)}</strong>${row.name && row.name !== row.display_symbol ? `<small>${rtEscape(row.name)}</small>` : ""}</button></span></td>
      <td><span class="realtime-status-pill realtime-status-${row.status === "通过" ? "ok" : row.status === "不可计算" ? "na" : row.status === "观察" ? "watch" : "filter"}">${rtEscape(row.status)}</span>${row.reason && row.reason !== "—" ? `<small class="realtime-reason">${rtEscape(row.reason)}</small>` : ""}</td>
      <td>${rtFormatMetric(row.latest_price, "price")}</td>
      ${columns.map((column) => `<td>${rtFormatMetric(row.metrics?.[column.key], column.format)}</td>`).join("")}
      <td>${row.rank ? `<strong>#${row.rank}</strong>${row.selected_for_target ? '<span class="realtime-target-badge">面板目标</span>' : ""}` : "—"}</td>
      <td><span class="realtime-updated-at">${rtEscape(rtDate(row.price_updated_at || row.data_date))}</span></td>
      <td class="realtime-details-cell"><button class="realtime-details-button" type="button" data-rt-details-symbol="${rtEscape(row.display_symbol || row.symbol)}" data-rt-details="${rtEscape(JSON.stringify(row.details || {}, null, 2))}">查看</button></td>
    </tr>`).join("") : `<tr><td colspan="${headers.length + 1}" class="realtime-empty">没有符合筛选条件的标的。</td></tr>`}</tbody>
  </table>`;
}

function renderRealtimeDashboard() {
  const data = rt.dashboard;
  if (!data) return;
  const refresh = data.overviewRefresh || {};
  document.getElementById("realtime-overview-auto-toggle").checked = Boolean(refresh.auto_enabled);
  const summary = data.summary || {};
  const meta = [
    `内部数据库计算于 ${rtDate(data.calculated_at)}`,
    `总览标的 ${summary.total ?? 0}`,
    `候选池 ${summary.candidates ?? 0}`,
    `通过 ${summary.eligible ?? 0}`,
    `已过滤 ${summary.filtered ?? 0}`,
    `不可计算 ${summary.unavailable ?? 0}`,
  ];
  if (refresh.running) meta.push("行情总览正在更新");
  document.getElementById("realtime-dashboard-meta").textContent = meta.join(" · ");
  renderRealtimeDashboardTable();
}

async function loadRealtimeDashboard(force = false) {
  if (!rt.current || rt.dashboardInFlight || rt.activePanel !== "dashboard") return;
  rt.dashboardInFlight = true;
  const button = document.getElementById("realtime-dashboard-refresh");
  button.disabled = true;
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/dashboard${force ? "?force=1" : ""}`));
    rt.dashboard = { ...payload.dashboard, overviewRefresh: payload.overview_refresh };
    if (!rt.sort) rt.sort = { ...rt.dashboard.default_sort };
    renderRealtimeDashboard();
    if (force) rtSetStatus("已基于内部数据库重新计算；未调用外部行情 API。", "success", true);
  } catch (error) {
    rtSetStatus(error.message, "error", true);
    document.getElementById("realtime-dashboard-table").innerHTML = `<div class="realtime-empty">${rtEscape(error.message)}</div>`;
  } finally {
    button.disabled = false;
    rt.dashboardInFlight = false;
  }
}

async function saveRealtimeTask() {
  const task = rt.current;
  if (!task) return;
  const follow = document.getElementById("realtime-follow").checked;
  const snapshot = structuredClone(task.strategy_snapshot);
  if (!follow) {
    try { snapshot.definition = JSON.parse(document.getElementById("realtime-definition-json").value); }
    catch (_error) { rtSetStatus("策略定义 JSON 格式错误。", "error", true); return; }
  }
  const payload = {
    revision: task.revision,
    name: document.getElementById("realtime-name").value.trim(),
    follow_strategy: follow,
    strategy_snapshot: snapshot,
    settings: { ...task.settings, initial_capital: Number(document.getElementById("realtime-capital").value), leverage_multiplier: Number(document.getElementById("realtime-leverage").value) },
    notification_settings: {
      enabled: document.getElementById("realtime-mail-enabled").checked,
      channel_id: Number(document.getElementById("realtime-channel").value) || null,
      recipients: document.getElementById("realtime-recipients").value,
      subject_template: document.getElementById("realtime-subject-template").value,
      body_template: document.getElementById("realtime-body-template").value,
    },
  };
  try {
    rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${task.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }))).task;
    renderRealtimeDetail();
    rt.dashboard = null; rt.sort = null;
    rtSetStatus("设置已保存；运行中的正式计算继续使用当前运行快照。", "success", true);
    await loadRealtimeTasks({ silent: true });
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

async function saveRealtimePanelScript() {
  if (!rt.current) return;
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/panel`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ panel_revision: rt.current.panel_revision, script: document.getElementById("realtime-panel-script").value }),
    }));
    rt.current = payload.task;
    renderRealtimeDetail();
    rt.dashboard = null; rt.sort = null;
    rtSetStatus("面板脚本已保存，仅影响实时展示，不改变正式决策逻辑。", "success", true);
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

function renderRealtimeLogs() {
  const items = [
    ...rt.logs.events.map((event) => ({ key: `event:${event.id}`, id: event.id, time: event.completed_at || event.scheduled_at, title: `${event.trading_date} ${event.event_name} · ${event.status}`, body: event.error_message || { decision: event.decision, calculation: event.calculation, data_manifest: event.data_manifest } })),
    ...rt.logs.notifications.map((item) => ({ key: `mail:${item.id}`, id: item.id, time: item.sent_at || item.created_at, title: `邮件 ${item.status} · ${item.recipient}`, body: item.body || item.error_message })),
  ].sort((a, b) => String(b.time).localeCompare(String(a.time)));
  document.getElementById("realtime-log-count").textContent = `${items.length} 条`;
  document.getElementById("realtime-log-output").innerHTML = items.length ? items.map((item) => `<details class="realtime-log-item"><summary>${rtEscape(rtDate(item.time))} · ${rtEscape(item.title)}</summary><pre>${rtEscape(typeof item.body === "string" ? item.body : JSON.stringify(item.body || {}, null, 2))}</pre></details>`).join("") : '<div class="realtime-empty">暂无对应日志。</div>';
}

async function loadRealtimeLogs(reset = false) {
  if (!rt.current) return;
  if (reset) rt.logs = { events: [], notifications: [], kind: document.getElementById("realtime-log-kind").value, beforeEventId: null, beforeNotificationId: null };
  const params = new URLSearchParams({ kind: rt.logs.kind, limit: "50" });
  if (rt.logs.beforeEventId) params.set("before_event_id", rt.logs.beforeEventId);
  if (rt.logs.beforeNotificationId) params.set("before_notification_id", rt.logs.beforeNotificationId);
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/logs?${params}`));
    rt.logs.events.push(...(payload.events || []));
    rt.logs.notifications.push(...(payload.notifications || []));
    const eventIds = (payload.events || []).map((item) => Number(item.id));
    const notificationIds = (payload.notifications || []).map((item) => Number(item.id));
    if (eventIds.length) rt.logs.beforeEventId = Math.min(...eventIds);
    if (notificationIds.length) rt.logs.beforeNotificationId = Math.min(...notificationIds);
    const canLoadEvents = ["all", "decision"].includes(rt.logs.kind) && eventIds.length === 50;
    const canLoadMail = ["all", "mail"].includes(rt.logs.kind) && notificationIds.length === 50;
    document.getElementById("realtime-log-more").hidden = !canLoadEvents && !canLoadMail;
    renderRealtimeLogs();
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

async function createRealtimeTask(event) {
  event.preventDefault();
  try {
    const response = await fetch("/api/realtime/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strategy_id: Number(document.getElementById("realtime-create-strategy").value), name: document.getElementById("realtime-create-name").value.trim(), follow_strategy: document.getElementById("realtime-create-follow").checked }) });
    const task = (await rtJson(response)).task;
    rtCreateDialog.close(); await loadRealtimeTasks(); await openRealtimeTask(task.id);
  } catch (error) { rtSetStatus(error.message, "error"); }
}

async function createRealtimeChannel(event) {
  event.preventDefault();
  try {
    const response = await fetch("/api/realtime/email-channels", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: document.getElementById("realtime-channel-name").value.trim(), provider: document.getElementById("realtime-channel-provider").value, sender_email: document.getElementById("realtime-channel-sender").value.trim(), username: document.getElementById("realtime-channel-sender").value.trim(), secret: document.getElementById("realtime-channel-secret").value }) });
    const channel = (await rtJson(response)).channel;
    rtChannelDialog.close(); document.getElementById("realtime-channel-secret").value = "";
    await loadRealtimeChannels(); document.getElementById("realtime-channel").value = channel.id;
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

function initRealtime() {
  window.addEventListener("market-overview-auto-refresh-changed", (event) => {
    document.getElementById("realtime-overview-auto-toggle").checked = Boolean(event.detail?.enabled);
  });
  document.getElementById("realtime-new-task").addEventListener("click", async () => { await loadRealtimeStrategies(); rtCreateDialog.showModal(); });
  document.getElementById("realtime-create-form").addEventListener("submit", createRealtimeTask);
  document.getElementById("realtime-channel-form").addEventListener("submit", createRealtimeChannel);
  document.querySelectorAll("[data-realtime-tab]").forEach((button) => button.addEventListener("click", () => selectRealtimePanel(button.dataset.realtimeTab)));
  document.addEventListener("return-to-realtime-dashboard", () => {
    if (rt.current) selectRealtimePanel("dashboard");
  });
  ["realtime-dashboard-search", "realtime-dashboard-status-filter", "realtime-dashboard-candidate-only"].forEach((id) => document.getElementById(id).addEventListener("input", renderRealtimeDashboardTable));
  document.getElementById("realtime-dashboard-table").addEventListener("click", (event) => {
    const symbolButton = event.target.closest("[data-rt-open-symbol]");
    if (symbolButton) {
      document.dispatchEvent(new CustomEvent("open-market-detail", {
        detail: { symbol: symbolButton.dataset.rtOpenSymbol, returnContext: "realtime-dashboard" },
      }));
      return;
    }
    const detailsButton = event.target.closest("[data-rt-details]");
    if (detailsButton) {
      document.getElementById("realtime-details-title").textContent = `${detailsButton.dataset.rtDetailsSymbol} 明细`;
      document.getElementById("realtime-details-content").textContent = detailsButton.dataset.rtDetails;
      document.getElementById("realtime-details-dialog").showModal();
      return;
    }
    const button = event.target.closest("[data-rt-sort]"); if (!button) return;
    const key = button.dataset.rtSort;
    rt.sort = rt.sort?.key === key ? { key, direction: rt.sort.direction === "asc" ? "desc" : "asc" } : { key, direction: ["symbol", "status", "price_updated_at"].includes(key) ? "asc" : "desc" };
    renderRealtimeDashboardTable();
  });
  document.getElementById("realtime-dashboard-refresh").addEventListener("click", () => loadRealtimeDashboard(true));
  document.getElementById("realtime-details-close").addEventListener("click", () => document.getElementById("realtime-details-dialog").close());
  document.getElementById("realtime-overview-auto-toggle").addEventListener("change", async (event) => {
    try {
      const payload = await rtJson(await fetch("/api/market-overview/auto-refresh", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: event.target.checked }) }));
      event.target.checked = Boolean(payload.auto_enabled);
      window.dispatchEvent(new CustomEvent("market-overview-auto-refresh-changed", { detail: { enabled: Boolean(payload.auto_enabled) } }));
      rtSetStatus(payload.auto_enabled ? "行情总览自动更新已开启。" : "行情总览自动更新已关闭；正式决策仍会独立取数。", "success", true);
    } catch (error) { event.target.checked = !event.target.checked; rtSetStatus(error.message, "error", true); }
  });
  document.getElementById("realtime-save").addEventListener("click", saveRealtimeTask);
  document.getElementById("realtime-save-mail").addEventListener("click", saveRealtimeTask);
  document.getElementById("realtime-panel-save").addEventListener("click", saveRealtimePanelScript);
  document.getElementById("realtime-panel-validate").addEventListener("click", async () => {
    try { await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/panel/validate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ script: document.getElementById("realtime-panel-script").value }) })); rtSetStatus("面板脚本校验通过。", "success", true); }
    catch (error) { rtSetStatus(error.message, "error", true); }
  });
  document.getElementById("realtime-panel-regenerate").addEventListener("click", async () => {
    if (!confirm("恢复自动生成的面板列？当前自定义脚本将被替换。")) return;
    try { rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/panel/regenerate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ panel_revision: rt.current.panel_revision }) }))).task; renderRealtimeDetail(); rt.dashboard = null; rt.sort = null; rtSetStatus("已恢复自动生成的通用面板脚本。", "success", true); }
    catch (error) { rtSetStatus(error.message, "error", true); }
  });
  document.getElementById("realtime-log-kind").addEventListener("change", () => loadRealtimeLogs(true));
  document.getElementById("realtime-log-more").addEventListener("click", () => loadRealtimeLogs(false));
  document.getElementById("realtime-new-channel").addEventListener("click", () => rtChannelDialog.showModal());
  document.getElementById("realtime-test-channel").addEventListener("click", async () => {
    const channelId = Number(document.getElementById("realtime-channel").value);
    const channel = rt.channels.find((item) => Number(item.id) === channelId);
    const recipient = window.prompt("测试邮件收件地址：", document.getElementById("realtime-recipients").value.split(/[,;\s]+/)[0] || channel?.sender_email || "");
    if (!channelId || !recipient) return;
    try { await rtJson(await fetch(`/api/realtime/email-channels/${channelId}/test`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ recipient }) })); rtSetStatus("测试邮件已提交。", "success", true); }
    catch (error) { rtSetStatus(error.message, "error", true); }
  });
  document.getElementById("realtime-body-template").addEventListener("input", (event) => {
    const preview = document.getElementById("realtime-code-body-preview");
    if (rt.current?.strategy_snapshot?.design_mode === "code") preview.hidden = Boolean(event.target.value.trim());
  });
  document.getElementById("realtime-follow").addEventListener("change", (event) => {
    document.getElementById("realtime-definition-json").disabled = event.target.checked;
  });
  document.getElementById("realtime-back").addEventListener("click", async () => { window.clearInterval(rt.dashboardTimer); rtDetailPage.hidden = true; rtListPage.hidden = false; rt.current = null; await loadRealtimeTasks(); });
  document.getElementById("realtime-start").addEventListener("click", async () => { try { rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/start`, { method: "POST" }))).task; renderRealtimeDetail(); await loadRealtimeTasks({ silent: true }); } catch (error) { rtSetStatus(error.message, "error", true); } });
  document.getElementById("realtime-stop").addEventListener("click", async (event) => {
    if (!confirm("终止当前实时决策任务？")) return;
    event.currentTarget.disabled = true;
    try {
      rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/stop`, { method: "POST" }))).task;
      renderRealtimeDetail();
      rtSetStatus(
        rt.current.runtime_state === "stopping"
          ? "当前关键事件正在处理；完成本次决策与通知提交后将自动停止。"
          : "任务已终止。",
        rt.current.runtime_state === "stopping" ? "neutral" : "success",
        true,
      );
      loadRealtimeTasks({ silent: true });
    } catch (error) {
      event.currentTarget.disabled = false;
      rtSetStatus(error.message, "error", true);
    }
  });
  rtCards.addEventListener("click", async (event) => {
    const card = event.target.closest("[data-rt-task-id]"); if (!card) return;
    const id = Number(card.dataset.rtTaskId); const action = event.target.closest("[data-rt-action]")?.dataset.rtAction;
    if (action === "start" || action === "stop") {
      const button = event.target.closest("[data-rt-action]");
      button.disabled = true;
      try {
        const payload = await rtJson(await fetch(`/api/realtime/tasks/${id}/${action}`, { method: "POST" }));
        rt.tasks = rt.tasks.map((item) => item.id === id ? { ...item, ...payload.task } : item);
        renderRealtimeCards();
        const stopping = payload.task.runtime_state === "stopping";
        rtSetStatus(
          action === "stop"
            ? stopping ? "当前关键事件完成后将自动停止。" : "任务已终止。"
            : "任务正在启动。",
          stopping ? "neutral" : "success",
        );
        loadRealtimeTasks({ silent: true });
      } catch (error) {
        button.disabled = false;
        rtSetStatus(error.message, "error");
      }
      return;
    }
    if (action === "delete") { if (!confirm("删除任务？历史决策和邮件审计将保留。")) return; try { await rtJson(await fetch(`/api/realtime/tasks/${id}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) })); await loadRealtimeTasks(); } catch (error) { rtSetStatus(error.message, "error"); } return; }
    await openRealtimeTask(id);
  });
  loadRealtimeChannels(); loadRealtimeTasks();
  rt.refreshTimer = window.setInterval(() => {
    if (rtDetailPage.hidden) { renderRealtimeCards(); if (Date.now() - rt.lastListRefreshAt >= 15_000) loadRealtimeTasks({ silent: true }); }
    else if (Date.now() - rt.lastListRefreshAt >= 15_000) { loadRealtimeTaskStatus(); rt.lastListRefreshAt = Date.now(); }
  }, 1000);
}
