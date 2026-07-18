const statusEl = document.getElementById("market-status");
const symbolForm = document.getElementById("symbol-form");
const symbolInput = document.getElementById("symbol-input");
const chartTitle = document.getElementById("chart-title");
const chartSource = document.getElementById("chart-source");
const shutdownButton = document.getElementById("shutdown-button");
const shutdownNotice = document.getElementById("shutdown-notice");
const indicatorPanelToggle = document.getElementById("indicator-panel-toggle");
const indicatorPanel = document.getElementById("indicator-panel");
const indicatorPanelClose = document.getElementById("indicator-panel-close");
const favoriteIndicators = document.getElementById("favorite-indicators");
const customIndicatorForm = document.getElementById("custom-indicator-form");
const customIndicatorType = document.getElementById("custom-indicator-type");
const customIndicatorPeriod = document.getElementById("custom-indicator-period");
let heartbeatTimer;
let currentSymbol = "";
let currentViewCode = "1D";
let currentSymbolIndicators = [];
let indicatorCatalog = [];

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
  updateChartTitle(normalized);
  chartSource.textContent = "加载中";
  setStatus(`正在加载 ${normalized} 最近一年日线行情...`, "neutral");

  try {
    const response = await fetch(`/api/market-data/${encodeURIComponent(normalized)}`);
    const payload = await response.json();

    if (!payload.ok) {
      const error = payload.error || {};
      setStatus(error.message || "行情数据加载失败。", "error");
      chartSource.textContent = error.code || "加载失败";
      renderCandles([]);
      return;
    }

    renderCandles(payload.data);
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
    setStatus("前端无法连接本地服务，请确认后端仍在运行。", "error");
    chartSource.textContent = "连接失败";
  }
}

async function loadIndicatorCatalog() {
  const response = await fetch("/api/indicators?favorite=1");
  const payload = await response.json();
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
  const payload = await response.json();
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
  const payload = await response.json();
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
  const payload = await response.json();
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
  const result = await response.json();
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
  const result = await response.json();
  if (!result.ok) {
    setStatus(result.error?.message || "指标收藏状态保存失败。", "error");
  }
}

async function removeSymbolIndicator(symbolIndicatorId) {
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators/${symbolIndicatorId}`,
    { method: "DELETE" },
  );
  const result = await response.json();
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
shutdownButton.addEventListener("click", shutdownSystem);
indicatorPanelToggle.addEventListener("click", () => {
  indicatorPanel.hidden = !indicatorPanel.hidden;
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
document.addEventListener("indicator-action", handleIndicatorAction);
document.addEventListener("chart-period-change", async () => {
  updateChartTitle(symbolInput.value.trim().toUpperCase() || "行情");
  currentViewCode = getChartPeriod();
  await loadSymbolIndicators();
});

bindNavigation();
bindDatabaseBrowser();
initChart();
startHeartbeat();
loadIndicatorCatalog();
loadMarketData(symbolInput.value);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
