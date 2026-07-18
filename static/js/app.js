const statusEl = document.getElementById("market-status");
const symbolForm = document.getElementById("symbol-form");
const symbolInput = document.getElementById("symbol-input");
const chartTitle = document.getElementById("chart-title");
const chartSource = document.getElementById("chart-source");
const marketOverviewPanel = document.getElementById("market-overview-panel");
const marketDetailPanel = document.getElementById("market-detail-panel");
const overviewTable = document.getElementById("overview-table");
const overviewSummary = document.getElementById("overview-summary");
const overviewPagination = document.getElementById("overview-pagination");
const overviewPrev = document.getElementById("overview-prev");
const overviewNext = document.getElementById("overview-next");
const overviewPageText = document.getElementById("overview-page-text");
const backToOverview = document.getElementById("back-to-overview");
const themeToggle = document.getElementById("theme-toggle");
const shutdownButton = document.getElementById("shutdown-button");
const shutdownNotice = document.getElementById("shutdown-notice");
const indicatorPanelToggle = document.getElementById("indicator-panel-toggle");
const indicatorPanel = document.getElementById("indicator-panel");
const indicatorPanelClose = document.getElementById("indicator-panel-close");
const favoriteIndicators = document.getElementById("favorite-indicators");
const customIndicatorForm = document.getElementById("custom-indicator-form");
const customIndicatorType = document.getElementById("custom-indicator-type");
const customIndicatorPeriod = document.getElementById("custom-indicator-period");
const updateDataButton = document.getElementById("update-data-button");
const symbolSettingsToggle = document.getElementById("symbol-settings-toggle");
const symbolSettingsPanel = document.getElementById("symbol-settings-panel");
const symbolSettingsClose = document.getElementById("symbol-settings-close");
const showWeekendData = document.getElementById("show-weekend-data");
let heartbeatTimer;
let currentSymbol = "";
let currentViewCode = "1D";
let currentSymbolIndicators = [];
let indicatorCatalog = [];
let currentRawMarketData = [];
let currentSymbolSettings = { show_weekend_data: true };
let overviewPage = 1;
let overviewTotalPages = 1;
let draggedOverviewSymbol = "";

function applyTheme(theme) {
  const nextTheme = theme === "light" ? "light" : "dark";
  document.body.classList.toggle("theme-dark", nextTheme === "dark");
  document.body.classList.toggle("theme-light", nextTheme === "light");
  themeToggle.classList.toggle("is-active", nextTheme === "dark");
  themeToggle.title = nextTheme === "dark" ? "切换为日间模式" : "切换为夜间模式";
  themeToggle.setAttribute("aria-label", themeToggle.title);
  window.localStorage.setItem("trade-theme", nextTheme);
  if (typeof drawChart === "function") {
    drawChart();
  }
}

function initTheme() {
  applyTheme(window.localStorage.getItem("trade-theme") || "dark");
}

function updateChartTitle(symbol) {
  chartTitle.textContent = `${symbol} ${getChartPeriodLabel()}`;
}

function setStatus(message, type = "neutral") {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
}

function sourceText(source) {
  const labels = {
    database: "来自本地数据库",
    api: "来自 Twelve Data API",
    stale_cache: "来自本地旧缓存",
  };
  return labels[source] || source || "未知来源";
}

async function loadMarketData(symbol) {
  const normalized = symbol.trim().toUpperCase();
  if (!normalized) {
    setStatus("请输入股票代码。", "error");
    return;
  }

  symbolInput.value = normalized;
  currentSymbol = normalized;
  showMarketDetail();
  updateChartTitle(normalized);
  chartSource.textContent = "加载中";
  setStatus(`正在加载 ${normalized} 2020年以来日线行情...`, "neutral");

  try {
    const params = new URLSearchParams({ symbol: normalized });
    const response = await fetch(`/api/market-data?${params}`);
    const payload = await parseJsonResponse(response);

    if (!payload.ok) {
      const error = payload.error || {};
      setStatus(error.message || "行情数据加载失败。", "error");
      chartSource.textContent = error.code || "加载失败";
      renderCandles([]);
      return;
    }

    currentRawMarketData = payload.data;
    currentSymbolSettings = payload.symbol_settings || { show_weekend_data: true };
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    renderCurrentMarketData();
    await loadSymbolIndicators();
    updateChartTitle(payload.symbol);
    chartSource.textContent = sourceText(payload.source);

    if (payload.warning) {
      setStatus(payload.warning, "warning");
    } else {
      const firstDate = payload.data[0]?.date || "-";
      const lastDate = payload.data[payload.data.length - 1]?.date || "-";
      setStatus(
        `已加载 ${payload.symbol}，共 ${payload.data.length} 条日线数据，范围 ${firstDate} 至 ${lastDate}。`,
        "success",
      );
    }
  } catch (error) {
    setStatus(error.message || "前端无法连接本地服务，请确认后端仍在运行。", "error");
    chartSource.textContent = "连接失败";
  }
}

async function loadMarketOverview(page = overviewPage) {
  overviewPage = page;
  showMarketOverview();
  overviewSummary.textContent = "加载中";
  setStatus("正在加载行情总览...", "neutral");

  try {
    const params = new URLSearchParams({
      page: String(overviewPage),
      page_size: "100",
    });
    const response = await fetch(`/api/market-overview?${params}`);
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "行情总览加载失败。", "error");
      renderOverviewTable([]);
      return;
    }

    overviewPage = payload.page;
    overviewTotalPages = payload.total_pages;
    renderOverviewTable(payload.items || []);
    overviewSummary.textContent = `共 ${payload.total_rows} 个标的`;
    overviewPageText.textContent = `第 ${payload.page} 页 / 共 ${payload.total_pages} 页`;
    overviewPagination.hidden = payload.total_rows <= payload.page_size;
    overviewPrev.disabled = payload.page <= 1;
    overviewNext.disabled = payload.page >= payload.total_pages;
    setStatus("行情总览已加载。", "success");
  } catch (error) {
    setStatus(error.message || "行情总览加载失败。", "error");
    overviewSummary.textContent = "加载失败";
    renderOverviewTable([]);
  }
}

function showMarketOverview() {
  marketOverviewPanel.hidden = false;
  marketDetailPanel.hidden = true;
  indicatorPanel.hidden = true;
  symbolSettingsPanel.hidden = true;
  chartSource.textContent = "等待查询";
}

function showMarketDetail() {
  marketOverviewPanel.hidden = true;
  marketDetailPanel.hidden = false;
}

function renderOverviewTable(items) {
  const headers = [
    "标的代码",
    "最新价格",
    "当日涨跌",
    "YTD",
    "",
  ];
  const thead = `<thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead>`;

  if (!items.length) {
    overviewTable.innerHTML = `${thead}<tbody><tr><td class="empty-cell" colspan="${headers.length}">暂无标的。查询并保存行情后会显示在这里。</td></tr></tbody>`;
    return;
  }

  const rows = items.map((item) => {
    const dailyClass = numberTone(item.daily_change);
    const ytdClass = numberTone(item.ytd_percent);
    return `
      <tr class="overview-row" draggable="true" data-symbol="${escapeHtml(item.symbol)}">
        <td>
          <button class="symbol-link" type="button" data-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)}</button>
        </td>
        <td class="${dailyClass}">${formatOverviewPrice(item.latest_price)}</td>
        <td class="${dailyClass}">${formatOverviewSignedNumber(item.daily_change)} ${formatOverviewPercent(item.daily_change_percent)}</td>
        <td class="${ytdClass}">${formatOverviewPercent(item.ytd_percent)}</td>
        <td class="drag-cell">
          <button class="drag-handle" type="button" title="拖动排序" aria-label="拖动排序" draggable="true" data-symbol="${escapeHtml(item.symbol)}">
            <span></span><span></span><span></span>
          </button>
        </td>
      </tr>
    `;
  });
  overviewTable.innerHTML = `${thead}<tbody>${rows.join("")}</tbody>`;
}

function numberTone(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "number-neutral";
  }
  return Number(value) >= 0 ? "number-up" : "number-down";
}

function formatOverviewPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function formatOverviewSignedNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  const number = Number(value);
  const prefix = number >= 0 ? "+" : "";
  return `${prefix}${number.toFixed(2)}`;
}

function formatOverviewPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  const number = Number(value);
  const prefix = number >= 0 ? "+" : "";
  return `(${prefix}${number.toFixed(2)}%)`;
}

async function saveOverviewOrder() {
  const symbols = Array.from(overviewTable.querySelectorAll("tbody tr[data-symbol]"))
    .map((row) => row.dataset.symbol);
  if (!symbols.length) {
    return;
  }

  try {
    const response = await fetch("/api/market-overview/order", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "总览排序保存失败。", "error");
      return;
    }
    setStatus("总览排序已保存。", "success");
    await loadMarketOverview(overviewPage);
  } catch (error) {
    setStatus(error.message || "总览排序保存失败。", "error");
  }
}

function moveOverviewRow(targetRow) {
  const sourceRow = Array.from(overviewTable.querySelectorAll("tr[data-symbol]"))
    .find((row) => row.dataset.symbol === draggedOverviewSymbol);
  if (!sourceRow || !targetRow || sourceRow === targetRow) {
    return;
  }
  const body = targetRow.parentElement;
  const rows = Array.from(body.querySelectorAll("tr[data-symbol]"));
  const sourceIndex = rows.indexOf(sourceRow);
  const targetIndex = rows.indexOf(targetRow);
  if (sourceIndex < targetIndex) {
    body.insertBefore(sourceRow, targetRow.nextSibling);
  } else {
    body.insertBefore(sourceRow, targetRow);
  }
}

async function updateCurrentMarketData() {
  if (!currentSymbol) {
    setStatus("请先加载一个标的。", "error");
    return;
  }

  updateDataButton.disabled = true;
  chartSource.textContent = "更新中";
  setStatus(`正在检查并更新 ${currentSymbol} 自 2020-01-01 以来的数据...`, "neutral");

  try {
    const response = await fetch("/api/market-data/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: currentSymbol }),
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      const error = payload.error || {};
      setStatus(error.message || "行情数据更新失败。", "error");
      chartSource.textContent = error.code || "更新失败";
      return;
    }

    currentRawMarketData = payload.data;
    currentSymbolSettings = payload.symbol_settings || currentSymbolSettings;
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    renderCurrentMarketData();
    await loadSymbolIndicators();
    updateChartTitle(payload.symbol);
    chartSource.textContent = sourceText(payload.source);

    const firstDate = payload.data[0]?.date || "-";
    const lastDate = payload.data[payload.data.length - 1]?.date || "-";
    const actionText = payload.source === "api" ? "已从 API 更新" : "数据库已完整";
    setStatus(
      `${actionText}：${payload.symbol} 共 ${payload.data.length} 条数据，范围 ${firstDate} 至 ${lastDate}。`,
      "success",
    );
  } catch (error) {
    setStatus(error.message || "行情数据更新失败。", "error");
    chartSource.textContent = "更新失败";
  } finally {
    updateDataButton.disabled = false;
  }
}

function renderCurrentMarketData() {
  const rows = currentSymbolSettings.show_weekend_data
    ? currentRawMarketData
    : currentRawMarketData.filter((row) => !isWeekendDate(row.date));
  renderCandles(rows);
}

function isWeekendDate(dateText) {
  const day = new Date(`${dateText}T00:00:00`).getDay();
  return day === 0 || day === 6;
}

async function saveSymbolSettings() {
  if (!currentSymbol) {
    return;
  }

  const nextSettings = {
    show_weekend_data: showWeekendData.checked,
  };
  currentSymbolSettings = { ...currentSymbolSettings, ...nextSettings };
  renderCurrentMarketData();
  await loadSymbolIndicators();

  try {
    const response = await fetch(`/api/symbols/${encodeURIComponent(currentSymbol)}/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nextSettings),
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "标的设置保存失败。", "error");
      return;
    }
    currentSymbolSettings = payload.symbol_settings;
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    const weekendText = currentSymbolSettings.show_weekend_data ? "显示" : "隐藏";
    setStatus(`已保存 ${currentSymbol} 设置：${weekendText}周末 K 线。`, "success");
  } catch (error) {
    setStatus(error.message || "标的设置保存失败。", "error");
  }
}

async function parseJsonResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error(`后端返回了非 JSON 响应，HTTP ${response.status}。`);
  }
  return response.json();
}

async function loadIndicatorCatalog() {
  const response = await fetch("/api/indicators?favorite=1");
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    favoriteIndicators.textContent = "收藏指标加载失败";
    return;
  }

  indicatorCatalog = payload.indicators;
  renderFavoriteIndicators();
}

async function loadSymbolIndicators() {
  if (!currentSymbol) {
    return;
  }

  currentViewCode = getChartPeriod();
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators`,
  );
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    setStatus(payload.error?.message || "指标配置加载失败。", "warning");
    setChartIndicators([]);
    return;
  }

  currentSymbolIndicators = payload.indicators;
  setChartIndicators(currentSymbolIndicators);
}

function renderFavoriteIndicators() {
  if (!indicatorCatalog.length) {
    favoriteIndicators.textContent = "暂无收藏指标";
    return;
  }

  favoriteIndicators.innerHTML = indicatorCatalog.map((indicator) => (
    `<button class="indicator-chip" type="button" data-indicator-id="${indicator.id}">
      ${escapeHtml(indicator.name)}
    </button>`
  )).join("");
}

async function addIndicatorToCurrentView(indicatorId) {
  if (!currentSymbol) {
    setStatus("请先加载一个标的。", "error");
    return;
  }

  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indicator_id: Number(indicatorId) }),
    },
  );
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    setStatus(payload.error?.message || "添加指标失败。", "error");
    return;
  }

  currentSymbolIndicators = payload.symbol_indicators;
  setChartIndicators(currentSymbolIndicators);
  setStatus(`已添加指标到 ${currentSymbol} ${getChartPeriodLabel()}。`, "success");
}

async function createAndAddIndicator(event) {
  event.preventDefault();
  if (!currentSymbol) {
    setStatus("请先加载一个标的。", "error");
    return;
  }

  const indicatorType = customIndicatorType.value;
  const period = Number(customIndicatorPeriod.value);
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        indicator_type: indicatorType,
        params: { period },
        name: `${indicatorType}${period}`,
      }),
    },
  );
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    setStatus(payload.error?.message || "创建指标失败。", "error");
    return;
  }

  currentSymbolIndicators = payload.symbol_indicators;
  setChartIndicators(currentSymbolIndicators);
  await loadIndicatorCatalog();
  setStatus(`已添加 ${indicatorType}${period}。`, "success");
}

async function handleIndicatorAction(event) {
  const { action, symbolIndicatorId, indicatorId } = event.detail;
  const symbolIndicator = currentSymbolIndicators.find((item) => item.id === symbolIndicatorId);
  if (!symbolIndicator) {
    return;
  }

  if (action === "toggle-visible") {
    await patchSymbolIndicator(symbolIndicatorId, { visible: !symbolIndicator.visible });
    return;
  }

  if (action === "toggle-favorite") {
    await patchIndicator(indicatorId, { is_favorite: !symbolIndicator.is_favorite });
    await loadSymbolIndicators();
    await loadIndicatorCatalog();
    return;
  }

  if (action === "remove") {
    await removeSymbolIndicator(symbolIndicatorId);
  }
}

async function patchSymbolIndicator(symbolIndicatorId, payload) {
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators/${symbolIndicatorId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const result = await parseJsonResponse(response);
  if (!result.ok) {
    setStatus(result.error?.message || "指标设置保存失败。", "error");
    return;
  }
  currentSymbolIndicators = result.symbol_indicators;
  setChartIndicators(currentSymbolIndicators);
}

async function patchIndicator(indicatorId, payload) {
  const response = await fetch(`/api/indicators/${indicatorId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await parseJsonResponse(response);
  if (!result.ok) {
    setStatus(result.error?.message || "指标收藏状态保存失败。", "error");
  }
}

async function removeSymbolIndicator(symbolIndicatorId) {
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators/${symbolIndicatorId}`,
    { method: "DELETE" },
  );
  const result = await parseJsonResponse(response);
  if (!result.ok) {
    setStatus(result.error?.message || "移除指标失败。", "error");
    return;
  }
  currentSymbolIndicators = result.symbol_indicators;
  setChartIndicators(currentSymbolIndicators);
}

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
      button.classList.add("active");
      document.getElementById(button.dataset.view).classList.add("active");

      if (button.dataset.view === "database-view") {
        loadTables();
      } else if (button.dataset.view === "market-view" && marketOverviewPanel.hidden) {
        loadMarketOverview(overviewPage);
      }
    });
  });
}

function startHeartbeat() {
  const beat = () => {
    fetch("/api/session/heartbeat", { method: "POST", keepalive: true }).catch(() => {});
  };
  beat();
  heartbeatTimer = window.setInterval(beat, 5000);

  window.addEventListener("beforeunload", () => {
    navigator.sendBeacon("/api/session/close");
  });
}

async function shutdownSystem() {
  shutdownButton.disabled = true;
  shutdownButton.querySelector("span:last-child").textContent = "正在退出";
  setStatus("正在退出系统...", "warning");

  if (heartbeatTimer) {
    window.clearInterval(heartbeatTimer);
  }

  try {
    await fetch("/api/system/shutdown", { method: "POST", keepalive: true });
  } catch (error) {
    // The server may close the connection while exiting.
  }

  document.querySelectorAll("button, input, select").forEach((element) => {
    element.disabled = true;
  });
  shutdownNotice.hidden = false;
}

symbolForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadMarketData(symbolInput.value);
});
backToOverview.addEventListener("click", () => {
  currentSymbol = "";
  symbolInput.value = "";
  renderCandles([]);
  setChartIndicators([]);
  loadMarketOverview(overviewPage);
});
overviewPrev.addEventListener("click", () => {
  loadMarketOverview(Math.max(1, overviewPage - 1));
});
overviewNext.addEventListener("click", () => {
  loadMarketOverview(Math.min(overviewTotalPages, overviewPage + 1));
});
overviewTable.addEventListener("click", (event) => {
  if (event.target.closest(".drag-handle")) {
    return;
  }
  const row = event.target.closest("tr[data-symbol]");
  if (!row) {
    return;
  }
  loadMarketData(row.dataset.symbol);
});
overviewTable.addEventListener("dragstart", (event) => {
  const handle = event.target.closest(".drag-handle");
  const row = event.target.closest("tr[data-symbol]");
  if (!handle || !row) {
    event.preventDefault();
    return;
  }
  draggedOverviewSymbol = row.dataset.symbol;
  row.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggedOverviewSymbol);
});
overviewTable.addEventListener("dragover", (event) => {
  const row = event.target.closest("tr[data-symbol]");
  if (!row || !draggedOverviewSymbol) {
    return;
  }
  event.preventDefault();
  moveOverviewRow(row);
});
overviewTable.addEventListener("dragend", async () => {
  const row = overviewTable.querySelector(".is-dragging");
  if (row) {
    row.classList.remove("is-dragging");
  }
  if (draggedOverviewSymbol) {
    draggedOverviewSymbol = "";
    await saveOverviewOrder();
  }
});
shutdownButton.addEventListener("click", shutdownSystem);
themeToggle.addEventListener("click", () => {
  const nextTheme = document.body.classList.contains("theme-dark") ? "light" : "dark";
  applyTheme(nextTheme);
});
indicatorPanelToggle.addEventListener("click", () => {
  indicatorPanel.hidden = !indicatorPanel.hidden;
  symbolSettingsPanel.hidden = true;
  if (!indicatorPanel.hidden) {
    loadIndicatorCatalog();
  }
});
indicatorPanelClose.addEventListener("click", () => {
  indicatorPanel.hidden = true;
});
favoriteIndicators.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-indicator-id]");
  if (button) {
    addIndicatorToCurrentView(button.dataset.indicatorId);
  }
});
customIndicatorForm.addEventListener("submit", createAndAddIndicator);
updateDataButton.addEventListener("click", updateCurrentMarketData);
symbolSettingsToggle.addEventListener("click", () => {
  symbolSettingsPanel.hidden = !symbolSettingsPanel.hidden;
  indicatorPanel.hidden = true;
});
symbolSettingsClose.addEventListener("click", () => {
  symbolSettingsPanel.hidden = true;
});
showWeekendData.addEventListener("change", saveSymbolSettings);
document.addEventListener("indicator-action", handleIndicatorAction);
document.addEventListener("chart-period-change", async () => {
  updateChartTitle(symbolInput.value.trim().toUpperCase() || "行情");
  currentViewCode = getChartPeriod();
  await loadSymbolIndicators();
});

bindNavigation();
bindDatabaseBrowser();
initChart();
initTheme();
startHeartbeat();
loadIndicatorCatalog();
loadMarketData("SPY");

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
