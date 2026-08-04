const rt = { tasks: [], strategies: [], channels: [], current: null, refreshTimer: null, lastListRefreshAt: 0, lastLogSignature: null, listRefreshInFlight: false };

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
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", { hour12: false });
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
  const key = strategy?.code_key;
  if (key === "rapid_drop_atr_rotation") {
    return "代码策略默认正文按事件生成：\n\n风险检查时间：逐项列出通过/被过滤标的、最差单日涨跌、ATR 急跌结果及已持仓卖出建议。\n\n轮动选标时间：按排名列出 ATR 动量评分、过滤原因、最终目标和调仓建议。\n\n数据提示：列出被 IEX 忽略的标的。";
  }
  if (key === "sevenstar_etf_rotation") {
    return "代码策略默认正文：\n七星 ETF 趋势轮动：按加权趋势评分排名，列出过滤原因与最终目标/防御标的。\n\n建议：列出趋势评分、排名、过滤原因和最终调仓建议。\n数据提示：列出被 IEX 忽略的标的。";
  }
  return "代码策略默认正文：显示策略评分、排名、过滤原因、调仓建议和数据提示。";
}

async function loadRealtimeStrategies() {
  const payload = await rtJson(await fetch("/api/backtest/strategies"));
  rt.strategies = payload.strategies || [];
  const select = document.getElementById("realtime-create-strategy");
  select.innerHTML = rt.strategies.map((strategy) => `<option value="${strategy.id}">${rtEscape(strategy.name)} · ${strategy.design_mode === "code" ? "代码" : "非代码"}</option>`).join("");
}

async function loadRealtimeChannels() {
  const payload = await rtJson(await fetch("/api/realtime/email-channels"));
  rt.channels = payload.channels || [];
  const select = document.getElementById("realtime-channel");
  select.innerHTML = `<option value="">未选择</option>${rt.channels.map((channel) => `<option value="${channel.id}">${rtEscape(channel.name)} · ${rtEscape(channel.sender_email)}</option>`).join("")}`;
}

function refreshRealtimeDetailStatus(task) {
  if (!task || rtDetailPage.hidden) return;
  document.getElementById("realtime-detail-title").textContent = `#${task.id} ${task.name}`;
  document.getElementById("realtime-detail-subtitle").textContent = `${task.strategy_snapshot?.name || "策略"} · ${rtStateLabel(task.runtime_state)}`;
  document.getElementById("realtime-revision").textContent = `任务 revision ${task.revision} · 策略 revision ${task.source_strategy_revision}`;
  const running = ["starting", "running", "degraded", "stopping"].includes(task.runtime_state);
  document.getElementById("realtime-start").hidden = running;
  document.getElementById("realtime-stop").hidden = !running;
  // Do not call renderRealtimeDetail here: it would overwrite unsaved form
  // values while the five-second status refresh is running.
  renderRealtimeLogs(task.events || rt.current?.events || [], task.notifications || rt.current?.notifications || []);
}

async function loadRealtimeTaskStatus() {
  if (!rt.current || rtDetailPage.hidden) return;
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}`));
    if (!rt.current.revision || Number(payload.task.revision || 0) >= Number(rt.current.revision || 0)) {
      rt.current = { ...rt.current, ...payload.task };
    }
    refreshRealtimeDetailStatus(rt.current);
  } catch (error) {
    rtSetStatus(error.message, "error", true);
  }
}

async function loadRealtimeTasks({ silent = false, refreshDetail = true } = {}) {
  if (rt.listRefreshInFlight) return;
  rt.listRefreshInFlight = true;
  try {
    const payload = await rtJson(await fetch("/api/realtime/tasks"));
    rt.tasks = payload.tasks || [];
    rt.lastListRefreshAt = Date.now();
    renderRealtimeCards();
    if (!silent) rtSetStatus("实时决策任务已加载。", "success");
    if (rt.current) {
      const current = rt.tasks.find((task) => task.id === rt.current.id);
      if (current && rtDetailPage.hidden === false) {
        if (!rt.current.revision || Number(current.revision || 0) >= Number(rt.current.revision || 0)) {
          rt.current = { ...rt.current, ...current };
        }
        if (refreshDetail) refreshRealtimeDetailStatus(rt.current);
      }
    }
  } catch (error) {
    rtSetStatus(error.message, "error");
  } finally {
    rt.listRefreshInFlight = false;
  }
}

function renderRealtimeCards() {
  document.getElementById("realtime-task-count").textContent = `${rt.tasks.length} 个任务`;
  if (!rt.tasks.length) {
    rtCards.innerHTML = '<div class="realtime-empty">暂无任务，请点击“创建新任务”。</div>';
    return;
  }
  rtCards.innerHTML = rt.tasks.map((task) => {
    const running = ["starting", "running", "degraded", "stopping"].includes(task.runtime_state);
    const latest = task.latest_run;
    return `<article class="realtime-task-card" data-rt-task-id="${task.id}">
      <h4>#${task.id} ${rtEscape(task.name)}</h4>
      <p>${rtEscape(task.strategy_snapshot?.name || "未知策略")} · revision ${task.source_strategy_revision}</p>
      <p class="realtime-state-${rtEscape(task.runtime_state)}">${rtStateLabel(task.runtime_state)}${task.last_error_message ? ` · ${rtEscape(task.last_error_message)}` : ""}</p>
      <div class="realtime-card-metrics">
        <div class="realtime-card-metric"><span>运行时长</span><strong>${running ? rtDuration(task.run_started_at) : "—"}</strong></div>
        <div class="realtime-card-metric"><span>成功邮件</span><strong>${task.successful_notification_count || 0}</strong></div>
        <div class="realtime-card-metric"><span>下次触发</span><strong>${task.next_event_at ? rtDate(task.next_event_at) : "—"}</strong></div>
      </div>
      <div class="realtime-card-actions">
        <button type="button" data-rt-action="${running ? "stop" : "start"}">${running ? "终止" : "运行"}</button>
        <button type="button" data-rt-action="delete">删除</button>
      </div>
    </article>`;
  }).join("");
}

async function openRealtimeTask(taskId) {
  try {
    const payload = await rtJson(await fetch(`/api/realtime/tasks/${taskId}`));
    rt.current = payload.task;
    rt.lastLogSignature = null;
    rtListPage.hidden = true;
    rtDetailPage.hidden = false;
    renderRealtimeDetail(true);
    rtSetStatus("任务详情已加载。", "success", true);
  } catch (error) {
    rtSetStatus(error.message, "error", true);
  }
}

function renderRealtimeDetail(full = true) {
  const task = rt.current;
  if (!task) return;
  document.getElementById("realtime-detail-title").textContent = `#${task.id} ${task.name}`;
  document.getElementById("realtime-detail-subtitle").textContent = `${task.strategy_snapshot?.name || "策略"} · ${rtStateLabel(task.runtime_state)}`;
  document.getElementById("realtime-revision").textContent = `任务 revision ${task.revision} · 策略 revision ${task.source_strategy_revision}`;
  document.getElementById("realtime-name").value = task.name;
  document.getElementById("realtime-follow").checked = Boolean(task.follow_strategy);
  document.getElementById("realtime-capital").value = task.settings?.initial_capital ?? 100000;
  document.getElementById("realtime-leverage").value = task.settings?.leverage_multiplier ?? 1;
  document.getElementById("realtime-strategy-summary").textContent = `${task.strategy_snapshot?.name || ""}\n设计模式：${task.strategy_snapshot?.design_mode || ""}\n选标模式：${task.strategy_snapshot?.selection_mode || ""}\n标的：${(task.strategy_snapshot?.definition?.symbols || []).map((item) => item.symbol).join(", ")}`;
  document.getElementById("realtime-definition-json").value = JSON.stringify(task.strategy_snapshot?.definition || {}, null, 2);
  const notification = task.notification_settings || {};
  document.getElementById("realtime-mail-enabled").checked = Boolean(notification.enabled);
  document.getElementById("realtime-channel").value = notification.channel_id || "";
  document.getElementById("realtime-recipients").value = Array.isArray(notification.recipients) ? notification.recipients.join(", ") : (notification.recipients || "");
  document.getElementById("realtime-subject-template").value = notification.subject_template || "";
  document.getElementById("realtime-body-template").value = notification.body_template || "";
  document.getElementById("realtime-body-template-help").hidden = task.strategy_snapshot?.design_mode !== "visual";
  const preview = document.getElementById("realtime-code-body-preview");
  if (task.strategy_snapshot?.design_mode === "code" && !notification.body_template) {
    preview.hidden = false;
    preview.textContent = rtCodeBodyPreview(task.strategy_snapshot);
  } else {
    preview.hidden = true;
    preview.textContent = "";
  }
  const running = ["starting", "running", "degraded", "stopping"].includes(task.runtime_state);
  document.getElementById("realtime-start").hidden = running;
  document.getElementById("realtime-stop").hidden = !running;
  [
    "realtime-name", "realtime-capital", "realtime-leverage",
    "realtime-mail-enabled", "realtime-channel", "realtime-recipients",
    "realtime-subject-template", "realtime-body-template", "realtime-save",
  ].forEach((id) => { document.getElementById(id).disabled = running; });
  document.getElementById("realtime-definition-json").disabled = Boolean(task.follow_strategy) || running;
  document.getElementById("realtime-follow").disabled = running;
  renderRealtimeLogs(task.events || [], task.notifications || []);
}

function renderRealtimeLogs(events, notifications) {
  const logSignature = JSON.stringify([
    ...events.map((item) => ["event", item.id, item.status, item.updated_at, item.completed_at]),
    ...notifications.map((item) => ["mail", item.id, item.status, item.updated_at, item.sent_at]),
  ]);
  if (rt.lastLogSignature === logSignature) return;
  const openKeys = new Set(
    [...document.querySelectorAll("#realtime-log-output details[open]")]
      .map((item) => item.dataset.logKey),
  );
  rt.lastLogSignature = logSignature;
  const items = [
    ...events.map((event) => ({
      key: `event:${event.id}`,
      time: event.completed_at || event.scheduled_at,
      title: `${event.trading_date} ${event.event_name} · ${event.status}`,
      body: event.error_message || {
        decision: event.decision,
        calculation: event.calculation,
        data_manifest: event.data_manifest,
      },
    })),
    ...notifications.map((item) => ({ key: `mail:${item.id}`, time: item.sent_at || item.created_at, title: `邮件 ${item.status} · ${item.recipient}`, body: item.body || item.error_message })),
  ].sort((a, b) => String(b.time).localeCompare(String(a.time)));
  document.getElementById("realtime-log-count").textContent = `${items.length} 条`;
  document.getElementById("realtime-log-output").innerHTML = items.length ? items.map((item) => `<details class="realtime-log-item" data-log-key="${rtEscape(item.key)}"${openKeys.has(item.key) ? " open" : ""}><summary>${rtEscape(rtDate(item.time))} · ${rtEscape(item.title)}</summary><pre>${rtEscape(typeof item.body === "string" ? item.body : JSON.stringify(item.body || {}, null, 2))}</pre></details>`).join("") : '<div class="realtime-empty">暂无决策或邮件日志。</div>';
}

async function saveRealtimeTask() {
  const task = rt.current;
  if (!task) return;
  const follow = document.getElementById("realtime-follow").checked;
  const snapshot = structuredClone(task.strategy_snapshot);
  if (!follow) {
    try { snapshot.definition = JSON.parse(document.getElementById("realtime-definition-json").value); } catch (error) { rtSetStatus("策略定义 JSON 格式错误。", "error", true); return; }
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
    const response = await fetch(`/api/realtime/tasks/${task.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    rt.current = (await rtJson(response)).task;
    renderRealtimeDetail(true);
    rtSetStatus("设置已保存；运行中的计算继续使用当前运行快照。", "success", true);
    await loadRealtimeTasks({ silent: true });
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

async function createRealtimeTask(event) {
  event.preventDefault();
  try {
    const response = await fetch("/api/realtime/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strategy_id: Number(document.getElementById("realtime-create-strategy").value), name: document.getElementById("realtime-create-name").value.trim(), follow_strategy: document.getElementById("realtime-create-follow").checked }) });
    const task = (await rtJson(response)).task;
    rtCreateDialog.close();
    await loadRealtimeTasks();
    await openRealtimeTask(task.id);
  } catch (error) { rtSetStatus(error.message, "error"); }
}

async function createRealtimeChannel(event) {
  event.preventDefault();
  try {
    const response = await fetch("/api/realtime/email-channels", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: document.getElementById("realtime-channel-name").value.trim(), provider: document.getElementById("realtime-channel-provider").value, sender_email: document.getElementById("realtime-channel-sender").value.trim(), username: document.getElementById("realtime-channel-sender").value.trim(), secret: document.getElementById("realtime-channel-secret").value }) });
    const channel = (await rtJson(response)).channel;
    rtChannelDialog.close();
    document.getElementById("realtime-channel-secret").value = "";
    await loadRealtimeChannels();
    document.getElementById("realtime-channel").value = channel.id;
  } catch (error) { rtSetStatus(error.message, "error", true); }
}

function initRealtime() {
  document.getElementById("realtime-new-task").addEventListener("click", async () => { await loadRealtimeStrategies(); rtCreateDialog.showModal(); });
  document.getElementById("realtime-create-form").addEventListener("submit", createRealtimeTask);
  document.getElementById("realtime-channel-form").addEventListener("submit", createRealtimeChannel);
  document.getElementById("realtime-new-channel").addEventListener("click", () => rtChannelDialog.showModal());
  document.getElementById("realtime-test-channel").addEventListener("click", async () => {
    const channelId = Number(document.getElementById("realtime-channel").value);
    const channel = rt.channels.find((item) => Number(item.id) === channelId);
    const defaultRecipient = document.getElementById("realtime-recipients").value.split(/[,;\s]+/)[0] || channel?.sender_email || "";
    const recipient = window.prompt("测试邮件收件地址：", defaultRecipient);
    if (!channelId || !recipient) return;
    try {
      await rtJson(await fetch(`/api/realtime/email-channels/${channelId}/test`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ recipient }) }));
      rtSetStatus("测试邮件已提交，请检查收件箱。", "success", true);
    } catch (error) { rtSetStatus(error.message, "error", true); }
  });
  document.getElementById("realtime-save").addEventListener("click", saveRealtimeTask);
  document.getElementById("realtime-body-template").addEventListener("input", (event) => {
    const preview = document.getElementById("realtime-code-body-preview");
    if (rt.current?.strategy_snapshot?.design_mode === "code") {
      preview.hidden = Boolean(event.target.value.trim());
    }
  });
  document.getElementById("realtime-back").addEventListener("click", async () => { rtDetailPage.hidden = true; rtListPage.hidden = false; rt.current = null; await loadRealtimeTasks(); });
  document.getElementById("realtime-start").addEventListener("click", async () => { try { rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/start`, { method: "POST" }))).task; renderRealtimeDetail(); await loadRealtimeTasks({ silent: true }); } catch (error) { rtSetStatus(error.message, "error", true); } });
  document.getElementById("realtime-stop").addEventListener("click", async () => { if (!confirm("终止当前实时决策任务？")) return; try { rt.current = (await rtJson(await fetch(`/api/realtime/tasks/${rt.current.id}/stop`, { method: "POST" }))).task; renderRealtimeDetail(); await loadRealtimeTasks({ silent: true }); } catch (error) { rtSetStatus(error.message, "error", true); } });
  rtCards.addEventListener("click", async (event) => {
    const card = event.target.closest("[data-rt-task-id]");
    if (!card) return;
    const id = Number(card.dataset.rtTaskId);
    const action = event.target.closest("[data-rt-action]")?.dataset.rtAction;
    if (action === "start" || action === "stop") {
      try { await rtJson(await fetch(`/api/realtime/tasks/${id}/${action}`, { method: "POST" })); await loadRealtimeTasks(); } catch (error) { rtSetStatus(error.message, "error"); }
      return;
    }
    if (action === "delete") {
      if (!confirm("删除任务？历史决策和邮件审计将保留。")) return;
      try { await rtJson(await fetch(`/api/realtime/tasks/${id}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) })); await loadRealtimeTasks(); } catch (error) { rtSetStatus(error.message, "error"); }
      return;
    }
    await openRealtimeTask(id);
  });
  loadRealtimeChannels();
  loadRealtimeTasks();
  rt.refreshTimer = window.setInterval(() => {
    if (rtDetailPage.hidden) {
      // Duration is a presentation value; repaint locally every second and
      // only fetch fresh runtime state periodically.
      renderRealtimeCards();
      if (Date.now() - rt.lastListRefreshAt >= 15000) loadRealtimeTasks({ silent: true });
    } else {
      loadRealtimeTaskStatus();
    }
  }, 1000);
}
