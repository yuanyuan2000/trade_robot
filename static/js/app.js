const statusEl = document.getElementById("market-status");
const symbolForm = document.getElementById("symbol-form");
const symbolInput = document.getElementById("symbol-input");
const includeIntradayData = document.getElementById("include-intraday-data");
const marketPageTitle = document.getElementById("market-page-title");
const marketSubtitle = document.getElementById("market-subtitle");
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
const overviewLiveToggle = document.getElementById("overview-live-toggle");
const overviewLiveLabel = document.getElementById("overview-live-label");
const overviewLiveControl = document.getElementById("overview-live-control");
const analysisRefreshAll = document.getElementById("analysis-refresh-all");
const overviewTitle = document.getElementById("overview-title");
const analysisOverviewProgress = document.getElementById("analysis-overview-progress");
const analysisTrendTooltip = document.getElementById("analysis-trend-tooltip");
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
const customIndicatorHalfLife = document.getElementById("custom-indicator-half-life");
const customIndicatorEpsilon = document.getElementById("custom-indicator-epsilon");
const customIndicatorThreshold = document.getElementById("custom-indicator-threshold");
const updateDataButton = document.getElementById("update-data-button");
const marketUpdateProgress = document.getElementById("market-update-progress");
const marketUpdateProgressContent = document.getElementById("market-update-progress-content");
const marketUpdateProgressText = document.getElementById("market-update-progress-text");
const marketUpdateProgressPercent = document.getElementById("market-update-progress-percent");
const marketUpdateProgressBar = document.getElementById("market-update-progress-bar");
const overviewSymbolToggle = document.getElementById("overview-symbol-toggle");
const symbolSettingsToggle = document.getElementById("symbol-settings-toggle");
const symbolSettingsPanel = document.getElementById("symbol-settings-panel");
const symbolSettingsClose = document.getElementById("symbol-settings-close");
const showWeekendData = document.getElementById("show-weekend-data");
const priceAdjustmentMode = document.getElementById("price-adjustment-mode");
const corporateActionEvents = document.getElementById("corporate-action-events");
const analysisControls = document.getElementById("analysis-controls");
const analysisAlgorithm = document.getElementById("analysis-algorithm");
const runAnalysisButton = document.getElementById("run-analysis-button");
const indicatorLegend = document.getElementById("indicator-legend");
const trendlineLegend = document.getElementById("trendline-legend");
let heartbeatTimer;
let currentSymbol = "";
let currentViewCode = "1D";
let currentWorkspaceMode = "market";
let currentSymbolIndicators = [];
let indicatorCatalog = [];
let overviewIndicatorIds = ["", "", ""];
let overviewSelectedIndicators = [];
let currentRawMarketData = [];
let currentSymbolSettings = { show_weekend_data: true };
let currentIntradaySync = { status: "not_initialized", row_count: 0 };
let currentCorporateActions = [];
let overviewPage = 1;
let overviewTotalPages = 1;
let overviewItems = [];
let marketOverviewItems = [];
let analysisOverviewItems = [];
const defaultOverviewSort = { key: "display_order", direction: "asc" };
let overviewSort = { ...defaultOverviewSort };
let draggedOverviewSymbol = "";
let overviewDailySyncDone = false;
let marketOverviewAutoUpdate = false;
let marketLoadRequestId = 0;
let marketOverviewFetchId = 0;
let analysisOverviewStatusTimer;
let analysisOverviewLoadInFlight;
let lastAnalysisRefreshState = {};
let analysisIndicatorLegendVisible = false;
let overviewLoadInFlight;
const overviewIndicatorStorageKey = "trade-overview-indicator-columns";

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

function updateIndicatorLegendVisibility() {
  indicatorLegend.hidden = (
    currentWorkspaceMode === "analysis"
    && !analysisIndicatorLegendVisible
  );
}

function applyWorkspaceMode(mode) {
  currentWorkspaceMode = mode === "analysis" ? "analysis" : "market";
  const isAnalysis = currentWorkspaceMode === "analysis";
  marketPageTitle.textContent = isAnalysis ? "智能分析" : "查看行情";
  marketSubtitle.textContent = isAnalysis ? "K线智能识别与算法分析" : "2020年以来行情数据";
  overviewTitle.textContent = isAnalysis ? "K线分析总览" : "行情总览";
  overviewLiveLabel.textContent = "自动更新";
  overviewLiveToggle.checked = marketOverviewAutoUpdate;
  overviewLiveControl.hidden = isAnalysis;
  analysisRefreshAll.hidden = !isAnalysis;
  overviewLiveControl.title = "每5分钟刷新总览最新价格";
  analysisOverviewProgress.hidden = !isAnalysis;
  if (isAnalysis) {
    if (!marketOverviewPanel.hidden) {
      renderAnalysisProgress(lastAnalysisRefreshState);
    }
  }
  analysisControls.hidden = !isAnalysis;
  trendlineLegend.hidden = !isAnalysis || !trendlineLegend.innerHTML;
  analysisIndicatorLegendVisible = false;
  updateIndicatorLegendVisibility();
  if (isAnalysis) {
    indicatorPanel.hidden = true;
  }
  if (!isAnalysis) {
    clearTrendlineAnalysis();
    if (currentSymbol) {
      loadSymbolIndicators();
    }
  } else if (currentSymbolIndicators.length) {
    currentSymbolIndicators = currentSymbolIndicators.map((indicator) => ({
      ...indicator,
      visible: false,
    }));
    setChartIndicators(currentSymbolIndicators);
  }
}

function setStatus(message, type = "neutral") {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
}

function sourceText(source) {
  const labels = {
    database: "来自本地数据库",
    yahoo: "来自 Yahoo Finance",
    twelvedata: "来自 Twelve Data API",
    api: "来自 Twelve Data API",
    stale_cache: "来自本地旧缓存",
    alpaca: "来自 Alpaca",
    "alpaca-minute": "来自 Alpaca 分钟数据",
    "alpaca/database": "来自 Alpaca 分钟数据库",
  };
  return labels[source] || source || "未知来源";
}

async function loadPeriodMarketData(period, { silent = false } = {}) {
  if (!currentSymbol) {
    return;
  }
  if (!silent) {
    setStatus(`正在加载 ${currentSymbol} ${getChartPeriodLabel()}...`, "neutral");
  }
  const intradayPeriod = /^(?:[1-9]\d{0,2}m|[1-9]\d?h)$/.test(period);
  const params = new URLSearchParams({
    symbol: currentSymbol,
    period,
    limit: intradayPeriod ? "300" : "2000",
    adjustment: priceAdjustmentMode.value,
  });
  const response = await fetch(`/api/market-bars?${params}`);
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    throw new Error(payload.error?.message || "K线数据加载失败。");
  }
  currentRawMarketData = payload.data || [];
  currentSymbolSettings = payload.symbol_settings || currentSymbolSettings;
  currentIntradaySync = payload.sync_state || currentIntradaySync;
  currentCorporateActions = payload.corporate_actions || [];
  renderCorporateActionEvents();
  updateDetailActions();
  renderCurrentMarketData();
  chartSource.textContent = sourceText(payload.source);
  if (payload.warning) {
    setStatus(payload.warning, "warning");
  } else if (!silent) {
    const firstDate = payload.data[0]?.date || "-";
    const lastDate = payload.data.at(-1)?.endDate || payload.data.at(-1)?.date || "-";
    setStatus(
      `已加载 ${payload.symbol} ${getChartPeriodLabel()}，共 ${payload.data.length} 根，范围 ${firstDate} 至 ${lastDate}。`,
      "success",
    );
  }
}

async function loadMarketData(symbol, { includeIntraday = false } = {}) {
  const normalized = symbol.trim().toUpperCase();
  if (!normalized) {
    setStatus("请输入股票代码。", "error");
    return;
  }

  const requestId = ++marketLoadRequestId;
  symbolInput.value = normalized;
  currentSymbol = normalized;
  marketUpdateProgress.hidden = true;
  resetChartPeriod("1D");
  currentViewCode = "1D";
  showMarketDetail();
  updateChartTitle(normalized);
  chartSource.textContent = "加载中";
  clearTrendlineAnalysis();
  setStatus(
    includeIntraday
      ? `正在获取 ${normalized} 2020年以来日线和分钟数据，首次导入可能需要数分钟...`
      : `正在加载 ${normalized} 2020年以来日线行情...`,
    "neutral",
  );

  try {
    let payload;
    if (includeIntraday) {
      const response = await fetch("/api/market-data/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: normalized,
          include_intraday: true,
          background: true,
          query_only: true,
        }),
      });
      const started = await parseJsonResponse(response);
      if (requestId !== marketLoadRequestId) return;
      if (!started.ok) {
        const error = started.error || {};
        setStatus(error.message || "行情数据加载失败。", "error");
        chartSource.textContent = error.code || "加载失败";
        renderCandles([]);
        return;
      }
      renderMarketUpdateProgress(started.job);
      payload = await waitForMarketDataUpdate(started.job.id, {
        shouldRender: () => requestId === marketLoadRequestId,
      });
    } else {
      // The default 1D view is a direct main-database read. Do not create a
      // background minute job or wait for minute-database state/locks here.
      const params = new URLSearchParams({
        symbol: normalized,
        adjustment: priceAdjustmentMode.value,
      });
      payload = await parseJsonResponse(await fetch(`/api/market-data?${params}`));
      if (!payload.ok) {
        const error = payload.error || {};
        setStatus(error.message || "行情数据加载失败。", "error");
        chartSource.textContent = error.code || "加载失败";
        renderCandles([]);
        return;
      }
    }
    if (requestId !== marketLoadRequestId) return;

    currentRawMarketData = payload.data;
    currentSymbolSettings = payload.symbol_settings || { show_weekend_data: true };
    currentIntradaySync = payload.intraday_sync || currentIntradaySync;
    currentCorporateActions = payload.corporate_actions || [];
    renderCorporateActionEvents();
    if (payload.intraday_sync) {
      includeIntradayData.checked = Number(currentIntradaySync.row_count || 0) > 0;
    }
    currentSymbol = payload.canonical_symbol || payload.symbol || normalized;
    symbolInput.value = payload.symbol || normalized;
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    renderCurrentMarketData();
    await loadSymbolIndicators();
    updateChartTitle(payload.symbol);
    chartSource.textContent = sourceText(payload.source);
    updateDetailActions();

    if (payload.warning) {
      setStatus(payload.warning, "warning");
    } else if (currentWorkspaceMode === "analysis") {
      setStatus(`已加载 ${payload.symbol}，可点击智能识别直线趋势线。`, "success");
    } else {
      const firstDate = payload.data[0]?.date || "-";
      const lastDate = payload.data[payload.data.length - 1]?.date || "-";
      setStatus(
        `已加载 ${payload.symbol}，共 ${payload.data.length} 条日线数据，范围 ${firstDate} 至 ${lastDate}。`,
        "success",
      );
    }
    if (currentWorkspaceMode === "analysis") {
      await loadStoredTrendlineAnalysis(currentSymbol);
    }
    if (getChartPeriod() !== "1D") {
      await loadPeriodMarketData(getChartPeriod(), { silent: true });
    }
  } catch (error) {
    if (requestId !== marketLoadRequestId) return;
    setStatus(error.message || "前端无法连接本地服务，请确认后端仍在运行。", "error");
    chartSource.textContent = "连接失败";
  }
}

async function loadMarketOverview(page = overviewPage) {
  if (overviewLoadInFlight) {
    if (currentWorkspaceMode === "market") {
      renderCachedMarketOverview();
    }
    await overviewLoadInFlight;
    if (currentWorkspaceMode === "market") {
      renderCachedMarketOverview();
    }
    return;
  }
  const request = loadMarketOverviewInner(page);
  overviewLoadInFlight = request;
  try {
    await request;
  } finally {
    if (overviewLoadInFlight === request) {
      overviewLoadInFlight = null;
    }
    if (currentWorkspaceMode === "market") {
      renderCachedMarketOverview();
    }
  }
}

async function loadMarketOverviewInner(page = overviewPage) {
  overviewPage = page;
  if (isMarketOverviewActive()) {
    showMarketOverview();
    overviewSummary.textContent = "加载中";
    setStatus("正在加载行情总览...", "neutral");
  }

  try {
    await fetchAndRenderMarketOverview();
    let syncFailed = false;
    if (!overviewDailySyncDone) {
      try {
        await syncMarketOverviewDaily();
        await fetchAndRenderMarketOverview({ silent: true });
      } catch (error) {
        syncFailed = true;
        if (isMarketOverviewActive()) {
          setStatus(error.message || "行情总览更新失败，已保留本地数据。", "warning");
        }
      }
    }
    if (!syncFailed && isMarketOverviewActive()) {
      setStatus("行情总览已加载。", "success");
    }
  } catch (error) {
    if (isMarketOverviewActive()) {
      setStatus(error.message || "行情总览加载失败。", "error");
      overviewSummary.textContent = "加载失败";
      marketOverviewItems = [];
      overviewItems = [];
      renderOverviewTable([]);
    }
  }
}

async function fetchAndRenderMarketOverview(options = {}) {
  const requestId = ++marketOverviewFetchId;
  const params = new URLSearchParams();
  const selectedIds = overviewIndicatorIds.filter(Boolean);
  if (selectedIds.length) {
    params.set("indicator_ids", selectedIds.join(","));
  }
  const query = params.toString();
  const response = await fetch(`/api/market-overview${query ? `?${query}` : ""}`);
  const payload = await parseJsonResponse(response);
  if (requestId !== marketOverviewFetchId) {
    return payload;
  }
  if (!payload.ok) {
    throw new Error(payload.error?.message || "行情总览加载失败。");
  }

  overviewPage = payload.page;
  overviewTotalPages = payload.total_pages;
  marketOverviewItems = payload.items || [];
  overviewSelectedIndicators = payload.selected_indicators || [];
  if (currentWorkspaceMode === "market" && !currentSymbol) {
    renderCachedMarketOverview(payload.total_rows);

    if (!options.silent) {
      setStatus("已显示本地行情总览。", "success");
    }
  }
  return payload;
}

function renderCachedMarketOverview(totalRows = marketOverviewItems.length) {
  if (currentWorkspaceMode !== "market" || currentSymbol) {
    return;
  }
  showMarketOverview();
  overviewItems = marketOverviewItems;
  renderOverviewTable(getSortedOverviewItems());
  overviewSummary.textContent = `共 ${totalRows} 个标的`;
  overviewPageText.textContent = "";
  overviewPagination.hidden = true;
  overviewPrev.disabled = true;
  overviewNext.disabled = true;
}

function isMarketOverviewActive() {
  return currentWorkspaceMode === "market" && !currentSymbol && !marketOverviewPanel.hidden;
}

async function loadAnalysisOverview() {
  if (analysisOverviewLoadInFlight) {
    return analysisOverviewLoadInFlight;
  }
  analysisOverviewLoadInFlight = loadAnalysisOverviewInner().finally(() => {
    analysisOverviewLoadInFlight = null;
  });
  return analysisOverviewLoadInFlight;
}

async function loadAnalysisOverviewInner() {
  if (currentWorkspaceMode === "analysis") {
    showMarketOverview();
    overviewSummary.textContent = "加载中";
    setStatus("正在读取 K 线分析总览...", "neutral");
  }
  try {
    const payload = await fetchAndRenderAnalysisOverview();
    if (currentWorkspaceMode !== "analysis") {
      return;
    }
    renderAnalysisProgress(payload.refresh || {});
    if (payload.refresh?.running) {
      setStatus(analysisProgressText(payload.refresh), "neutral");
      monitorAnalysisOverviewRefresh();
    } else if (payload.refresh?.last_error) {
      setStatus(payload.refresh.last_error, "warning");
    } else {
      setStatus("K 线分析总览已加载。", "success");
    }
  } catch (error) {
    if (currentWorkspaceMode === "analysis") {
      setStatus(error.message || "K 线分析总览加载失败。", "error");
      overviewSummary.textContent = "加载失败";
      analysisOverviewItems = [];
      overviewItems = [];
      renderOverviewTable([]);
    }
  }
}

async function fetchAndRenderAnalysisOverview() {
  const response = await fetch("/api/analysis-overview");
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    throw new Error(payload.error?.message || "K 线分析总览加载失败。");
  }
  analysisOverviewItems = payload.items || [];
  if (currentWorkspaceMode === "analysis") {
    overviewItems = analysisOverviewItems;
    overviewSummary.textContent = `共 ${payload.total_rows} 个标的`;
    overviewPagination.hidden = true;
    renderOverviewTable(getSortedOverviewItems());
  }
  return payload;
}

async function startAnalysisOverviewRefresh(options = {}) {
  try {
    const response = await fetch("/api/analysis-overview/refresh", {
      method: "POST",
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      throw new Error(payload.error?.message || "无法启动 K 线分析更新。");
    }
    renderAnalysisProgress(payload);
    monitorAnalysisOverviewRefresh();
    if (!options.silent && currentWorkspaceMode === "analysis") {
      setStatus(analysisProgressText(payload), "neutral");
    }
  } catch (error) {
    if (!options.silent && currentWorkspaceMode === "analysis") {
      setStatus(error.message || "K 线分析更新启动失败。", "error");
    }
  }
}

function monitorAnalysisOverviewRefresh() {
  if (analysisOverviewStatusTimer) {
    return;
  }
  const poll = async () => {
    try {
      const response = await fetch("/api/analysis-overview/refresh-status");
      const payload = await parseJsonResponse(response);
      if (!payload.ok) {
        throw new Error(payload.error?.message || "分析进度读取失败。");
      }
      renderAnalysisProgress(payload);
      if (currentWorkspaceMode === "analysis" && !marketOverviewPanel.hidden) {
        await fetchAndRenderAnalysisOverview();
        setStatus(
          payload.running
            ? analysisProgressText(payload)
            : analysisCompletionText(payload),
          payload.last_error || payload.last_result?.failed ? "warning" : (payload.running ? "neutral" : "success"),
        );
      }
      if (!payload.running) {
        window.clearInterval(analysisOverviewStatusTimer);
        analysisOverviewStatusTimer = null;
      }
    } catch (error) {
      if (currentWorkspaceMode === "analysis") {
        setStatus(error.message || "分析进度读取失败。", "warning");
      }
    }
  };
  poll();
  analysisOverviewStatusTimer = window.setInterval(poll, 2500);
}

function analysisProgressText(state) {
  const total = Number(state.total || 0);
  const completed = Number(state.completed || 0);
  const workers = Number(state.parallel_workers || 0);
  const remaining = Number(state.remaining || 0);
  const parallel = workers > 1
    ? `，${workers} 个进程并行计算，剩余 ${remaining} 个`
    : "";
  const current = state.current_symbol ? `，正在分析 ${state.current_symbol}` : "";
  return `直线趋势线分析需要一些时间：已完成 ${completed}/${total}${parallel}${current}`;
}

function analysisCompletionText(state) {
  const failed = Number(state.last_result?.failed || 0);
  return failed
    ? `趋势线总览更新完成，${failed} 个标的分析失败。`
    : "趋势线总览更新完成。";
}

function renderAnalysisProgress(state) {
  lastAnalysisRefreshState = state || {};
  if (currentWorkspaceMode !== "analysis") {
    analysisOverviewProgress.hidden = true;
    return;
  }
  const running = Boolean(state.running);
  analysisRefreshAll.disabled = running;
  const hasError = Boolean(state.last_error);
  analysisOverviewProgress.hidden = false;
  analysisOverviewProgress.className = `analysis-overview-progress${running ? " is-running" : ""}${hasError ? " is-error" : ""}`;
  analysisOverviewProgress.innerHTML = running
    ? `<span class="analysis-progress-spinner" aria-hidden="true"></span><span>${escapeHtml(analysisProgressText(state))}</span>`
    : `<span>${escapeHtml(hasError ? state.last_error : analysisCompletionText(state))}</span>`;
}

async function syncMarketOverviewDaily() {
  overviewDailySyncDone = true;
  if (isMarketOverviewActive()) {
    overviewSummary.textContent = "更新行情中";
    setStatus("正在更新行情总览；已初始化标的同步分钟数据，其余标的更新日线...", "neutral");
  }
  const response = await fetch("/api/market-overview/sync-daily", { method: "POST" });
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    overviewDailySyncDone = false;
    throw new Error(payload.error?.message || "行情总览更新失败。");
  }

  const result = await waitForOverviewSync();
  const failed = (result.items || []).filter((item) => item.status !== "success").length;
  const suffix = failed ? `，${failed} 个标的需要稍后重试` : "";
  if (isMarketOverviewActive()) {
    setStatus(`行情总览更新完成，共写入 ${result.updated_rows || 0} 条日线${suffix}。`, failed ? "warning" : "success");
  }
}

async function waitForOverviewSync() {
  for (let attempt = 0; attempt < 24; attempt += 1) {
    await sleep(5000);
    const response = await fetch("/api/market-overview/sync-status");
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      throw new Error(payload.error?.message || "行情总览更新状态读取失败。");
    }
    if (!payload.running) {
      if (payload.last_error) {
        throw new Error(payload.last_error);
      }
      return payload.last_result || { items: [], updated_rows: 0 };
    }
  }
  throw new Error("行情总览更新仍在后台进行，本地数据已先显示。");
}

function sleep(milliseconds) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });
}

function showMarketOverview() {
  marketOverviewPanel.hidden = false;
  marketDetailPanel.hidden = true;
  indicatorPanel.hidden = true;
  symbolSettingsPanel.hidden = true;
  clearTrendlineAnalysis();
  chartSource.textContent = "等待查询";
  analysisOverviewProgress.hidden = currentWorkspaceMode !== "analysis";
}

function showMarketDetail() {
  marketOverviewPanel.hidden = true;
  marketDetailPanel.hidden = false;
}

function renderOverviewTable(items) {
  hideAnalysisTrendTooltip();
  if (currentWorkspaceMode === "analysis") {
    renderAnalysisOverviewTable(items);
    return;
  }
  renderMarketOverviewTable(items);
}

function renderMarketOverviewTable(items) {
  overviewTable.classList.remove("analysis-overview-table");
  overviewTable.classList.add("has-custom-indicators");
  const selectedById = new Map(
    overviewSelectedIndicators.map((indicator) => [String(indicator.id), indicator]),
  );
  const indicatorSlots = overviewIndicatorIds.map((indicatorId) => (
    indicatorId ? selectedById.get(indicatorId) || null : null
  ));
  const headers = [
    { label: "标的代码", key: "display_order" },
    { label: "最新价格", key: "latest_price" },
    { label: "更新时间", key: "latest_price_updated_at" },
    { label: "日涨跌", key: "daily_change_percent" },
    { label: "YTD", key: "ytd_percent" },
  ];
  const indicatorHeaders = indicatorSlots.map((indicator, index) => (
    renderOverviewIndicatorHeader(index, indicator)
  )).join("");
  const actionHeader = '<th class="overview-actions-column"></th>';
  const thead = `<thead><tr>${headers.map(renderOverviewHeader).join("")}${indicatorHeaders}${actionHeader}</tr></thead>`;
  const columnCount = headers.length + indicatorSlots.length + 1;

  if (!items.length) {
    overviewTable.innerHTML = `${thead}<tbody><tr><td class="empty-cell" colspan="${columnCount}">暂无标的。查询并保存行情后会显示在这里。</td></tr></tbody>`;
    return;
  }

  const rows = items.map((item) => {
    const dailyClass = numberTone(item.daily_change);
    const ytdClass = numberTone(item.ytd_percent);
    const indicatorCells = indicatorSlots.map((indicator) => {
      if (!indicator) {
        return '<td class="number-neutral overview-indicator-value"></td>';
      }
      const reading = item.indicator_values?.[String(indicator.id)] || {};
      const value = reading.value;
      const formula = indicator.indicator_type === "RATR"
        ? `（收盘价 - ${indicator.params.period} 个交易日前收盘价）/ 前一日 Wilder ATR(${indicator.params.period})`
        : indicator.indicator_type === "WTME"
          ? `100 × 加权方向收益 /（加权标准化真实波幅 + ${indicator.params.epsilon}），N=${indicator.params.period}，h=${indicator.params.half_life}`
          : indicator.indicator_type === "RAPID_DROP"
            ? `近 ${indicator.params.period} 个连续变化段任一跌幅 ≤ -${indicator.params.threshold_percent}% 时为 1，否则为 0（包含最新未结束 K 线）`
            : indicator.indicator_type === "ATR"
              ? `Wilder ATR(${indicator.params.period})`
              : `${indicator.indicator_type}(${indicator.params.period})`;
      return `<td class="number-neutral overview-indicator-value" title="${escapeHtml(`${formula}；数据日 ${reading.date || "-"}`)}">${formatOverviewIndicator(value, indicator)}</td>`;
    }).join("");
    return `
      <tr class="overview-row" draggable="true" data-symbol="${escapeHtml(item.symbol)}">
        <td>
          <button class="symbol-link" type="button" data-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.display_symbol || item.symbol)}</button>
        </td>
        <td class="${dailyClass}">${formatOverviewPrice(item.latest_price)}</td>
        <td class="number-neutral">${formatOverviewUpdatedAt(item.latest_price_updated_at)}</td>
        <td class="${dailyClass}">${formatOverviewPercent(item.daily_change_percent)}</td>
        <td class="${ytdClass}">${formatOverviewPercent(item.ytd_percent)}</td>
        ${indicatorCells}
        <td class="drag-cell">
          <div class="overview-row-actions">
            <button class="drag-handle" type="button" title="${overviewDragTitle()}" aria-label="${overviewDragTitle()}" draggable="true" data-drag-disabled="${overviewDragDisabledText()}" data-symbol="${escapeHtml(item.symbol)}">
              <span></span><span></span><span></span>
            </button>
            <button class="overview-remove-button" type="button" title="从总览隐藏" aria-label="从总览隐藏" data-symbol="${escapeHtml(item.symbol)}">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M3 6h18" />
                <path d="M8 6V4h8v2" />
                <path d="M6 6l1 15h10l1-15" />
                <path d="M10 11v6" />
                <path d="M14 11v6" />
              </svg>
            </button>
          </div>
        </td>
      </tr>
    `;
  });
  overviewTable.innerHTML = `${thead}<tbody>${rows.join("")}</tbody>`;
}

function renderAnalysisOverviewTable(items) {
  overviewTable.classList.add("analysis-overview-table");
  overviewTable.classList.remove("has-custom-indicators");
  const headers = [
    { label: "标的代码", key: "symbol" },
    { label: "最新价格", key: "latest_price" },
    { label: "更新时间", key: "latest_price_updated_at" },
    { label: "直线趋势线", key: "analysis_score" },
  ];
  const thead = `<thead><tr>${headers.map(renderOverviewHeader).join("")}</tr></thead>`;
  if (!items.length) {
    overviewTable.innerHTML = `${thead}<tbody><tr><td class="empty-cell" colspan="4">暂无分析标的。</td></tr></tbody>`;
    return;
  }
  const rows = items.map((item) => {
    const priceClass = numberTone(item.daily_change);
    const analysis = item.analysis;
    const trendContent = analysis
      ? renderAnalysisTrendSummary(analysis)
      : '<span class="analysis-empty">等待后台分析</span>';
    return `
      <tr class="overview-row analysis-overview-row" data-symbol="${escapeHtml(item.symbol)}" tabindex="0" role="button" aria-label="打开 ${escapeHtml(item.symbol)} K线分析">
        <td class="analysis-symbol">${escapeHtml(item.symbol)}</td>
        <td class="${priceClass}">
          <span class="analysis-price">${formatOverviewPrice(item.latest_price)}</span>
          <span class="analysis-price-change">${formatOverviewPercent(item.daily_change_percent)}</span>
        </td>
        <td class="number-neutral">${formatOverviewUpdatedAt(item.latest_price_updated_at)}</td>
        <td class="analysis-trend-cell">${trendContent}</td>
      </tr>
    `;
  });
  overviewTable.innerHTML = `${thead}<tbody>${rows.join("")}</tbody>`;
}

function renderAnalysisTrendSummary(analysis) {
  const trends = addOverviewTierLabels(analysis.headline_trends || []);
  const events = analysis.events || [];
  const visibleTrends = trends.slice(0, 2);
  const visibleEvents = events.slice(0, 2);
  const trendRows = visibleTrends.length
    ? visibleTrends.map(renderAnalysisTrend).join("")
    : '<span class="analysis-empty">暂无有效趋势</span>';
  const extraTrends = trends.length > 2
    ? `<span class="analysis-more" title="${escapeHtml(trends.slice(2).map(analysisTrendPlainText).join("\n"))}">+${trends.length - 2}</span>`
    : "";
  const eventRows = visibleEvents.map((event) => `
    <span class="analysis-event analysis-event-${escapeHtml(event.type)}" title="${escapeHtml(event.detail || "")}">
      ${analysisEventIcon(event.type)}
      <span>${escapeHtml(event.text)}</span>
    </span>
  `).join("");
  const extraEvents = events.length > 2
    ? `<span class="analysis-more" title="${escapeHtml(events.slice(2).map((event) => `${event.text}：${event.detail || ""}`).join("\n"))}">+${events.length - 2}</span>`
    : "";
  const stale = analysis.stale
    ? '<span class="analysis-stale" title="行情数据已更新，趋势线正在等待重新计算">待更新</span>'
    : "";
  return `
    <div class="analysis-trend-summary">
      <div class="analysis-current-trends">${trendRows}${extraTrends}${stale}</div>
      ${eventRows ? `<div class="analysis-events">${eventRows}${extraEvents}</div>` : ""}
    </div>
  `;
}

function addOverviewTierLabels(trends) {
  const medium = trends
    .slice(0, 2)
    .filter((trend) => trend.tier === "medium")
    .sort(
      (left, right) => Number(right.display_length || 0) - Number(left.display_length || 0),
    );
  const labels = new Map();
  if (medium.length === 2) {
    labels.set(medium[0].id, "中长期");
    labels.set(medium[1].id, "中短期");
  }
  return trends.map((trend) => ({
    ...trend,
    overview_tier_label: trend.overview_tier_label || labels.get(trend.id) || {
      long: "长期",
      medium: "中期",
      short: "短期",
    }[trend.tier] || trend.tier,
  }));
}

function renderAnalysisTrend(trend) {
  const direction = trend.direction === "up" ? "↑" : "↓";
  const tier = trend.overview_tier_label;
  const status = trend.status === "challenging" ? "挑战中" : "趋势中";
  const role = trend.family_role === "stage" ? '<span class="analysis-stage">阶段</span>' : "";
  return `
    <span
      class="analysis-trend analysis-trend-${escapeHtml(trend.status)}"
      tabindex="0"
      data-analysis-trend-tooltip="1"
      data-score="${escapeHtml(Number(trend.score || 0).toFixed(1))}"
      data-touches="${escapeHtml(trend.touches || 0)}"
      data-formation-date="${escapeHtml(formatOverviewDate(trend.formation_date))}"
      data-last-touch-date="${escapeHtml(formatOverviewDate(trend.last_touch_date))}"
      data-current-gap="${escapeHtml(formatAtrGap(trend.current_close_gap))}"
    >
      <span class="analysis-direction analysis-direction-${escapeHtml(trend.direction)}">${direction}</span>
      <span>${escapeHtml(tier)}</span>
      <span>${escapeHtml(status)}</span>
      <strong class="analysis-line-price">${formatOverviewPrice(trend.latest_line_price)}</strong>
      ${role}
    </span>
  `;
}

function analysisTrendPlainText(trend) {
  const direction = trend.direction === "up" ? "上涨" : "下跌";
  const tier = trend.overview_tier_label;
  const status = trend.status === "challenging" ? "挑战中" : "趋势中";
  return `${direction}${tier} ${status} 点位 ${formatOverviewPrice(trend.latest_line_price)}`;
}

function showAnalysisTrendTooltip(target, clientX, clientY) {
  analysisTrendTooltip.innerHTML = `
    <strong>趋势结构</strong>
    <div class="analysis-tooltip-grid">
      <span>评分</span><b>${escapeHtml(target.dataset.score)}</b>
      <span>有效触点</span><b>${escapeHtml(target.dataset.touches)}</b>
      <span>形成日期</span><b>${escapeHtml(target.dataset.formationDate)}</b>
      <span>最近触点</span><b>${escapeHtml(target.dataset.lastTouchDate)}</b>
      <span>距趋势线</span><b>${escapeHtml(target.dataset.currentGap)}</b>
    </div>
  `;
  analysisTrendTooltip.hidden = false;
  positionAnalysisTrendTooltip(target, clientX, clientY);
}

function positionAnalysisTrendTooltip(target, clientX, clientY) {
  const targetRect = target.getBoundingClientRect();
  const x = Number.isFinite(clientX) ? clientX : targetRect.right;
  const y = Number.isFinite(clientY) ? clientY : targetRect.bottom;
  const tooltipRect = analysisTrendTooltip.getBoundingClientRect();
  const left = Math.min(
    x + 14,
    window.innerWidth - tooltipRect.width - 10,
  );
  const top = Math.min(
    y + 14,
    window.innerHeight - tooltipRect.height - 10,
  );
  analysisTrendTooltip.style.left = `${Math.max(10, left)}px`;
  analysisTrendTooltip.style.top = `${Math.max(10, top)}px`;
}

function hideAnalysisTrendTooltip() {
  analysisTrendTooltip.hidden = true;
}

function analysisEventIcon(type) {
  if (type === "ended") return '<span aria-hidden="true">■</span>';
  if (type === "formed" || type === "stage_formed") return '<span aria-hidden="true">◆</span>';
  if (type === "challenge_started") return '<span aria-hidden="true">▲</span>';
  if (type === "challenge_resolved") return '<span aria-hidden="true">↺</span>';
  return '<span aria-hidden="true">●</span>';
}

function formatOverviewDate(value) {
  return value ? String(value).slice(5) : "-";
}

function formatAtrGap(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2)} ATR` : "-";
}

async function hideOverviewSymbol(symbol) {
  try {
    const response = await fetch(`/api/market-overview/${encodeURIComponent(symbol)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show_in_overview: false }),
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "隐藏标的失败。", "error");
      return;
    }
    marketOverviewItems = marketOverviewItems.filter((item) => item.symbol !== symbol);
    renderCachedMarketOverview(payload.total_rows ?? marketOverviewItems.length);
    await fetchAndRenderMarketOverview({ silent: true });
    setStatus(`已从行情总览隐藏 ${symbol}，历史数据仍保留。`, "success");
  } catch (error) {
    setStatus(error.message || "隐藏标的失败。", "error");
  }
}

async function setOverviewLiveRefresh(enabled) {
  try {
    const response = await fetch("/api/market-overview/auto-refresh", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled) }),
    });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) throw new Error(payload.error?.message || "自动更新设置失败。");
    marketOverviewAutoUpdate = Boolean(payload.auto_enabled);
    overviewLiveToggle.checked = marketOverviewAutoUpdate;
    setStatus(
      marketOverviewAutoUpdate
        ? "总览自动更新已开启，由服务端每5分钟统一更新一次。"
        : "总览自动更新已关闭；正式决策事件仍会按时独立获取行情。",
      marketOverviewAutoUpdate ? "success" : "neutral",
    );
  } catch (error) {
    overviewLiveToggle.checked = marketOverviewAutoUpdate;
    setStatus(error.message || "自动更新设置失败。", "error");
  }
}

async function loadOverviewRefreshPreference() {
  try {
    const response = await fetch("/api/market-overview/sync-status");
    const payload = await parseJsonResponse(response);
    if (!payload.ok) return;
    marketOverviewAutoUpdate = Boolean(payload.auto_enabled);
    overviewLiveToggle.checked = marketOverviewAutoUpdate;
  } catch (_error) {
    // Keep the existing switch value when the coordinator is unavailable.
  }
}

function renderOverviewHeader(header) {
  if (!header.key) {
    return `<th class="${escapeHtml(header.className || "")}"></th>`;
  }
  const isActive = overviewSort.key === header.key;
  const direction = isActive ? overviewSort.direction : "";
  const title = `${header.label}${isActive ? (direction === "asc" ? " 升序" : " 降序") : ""}`;
  return `
    <th class="${escapeHtml(header.className || "")}">
      <span class="overview-th-content">
        <span>${escapeHtml(header.label)}</span>
        <button
          class="overview-sort-button ${isActive ? "is-active" : ""}"
          type="button"
          title="${escapeHtml(title)}"
          aria-label="${escapeHtml(title)}"
          data-sort-key="${escapeHtml(header.key)}"
          data-sort-direction="${escapeHtml(direction)}"
        >
          <span class="sort-triangle sort-triangle-up"></span>
          <span class="sort-triangle sort-triangle-down"></span>
        </button>
      </span>
    </th>
  `;
}

function getSortedOverviewItems() {
  const direction = overviewSort.direction === "desc" ? -1 : 1;
  return [...overviewItems].sort((left, right) => {
    const result = compareOverviewValues(left, right, overviewSort.key);
    return result * direction;
  });
}

function compareOverviewValues(left, right, key) {
  if (key === "display_order") {
    return compareNullableNumbers(left.display_order, right.display_order)
      || compareNullableNumbers(left.id, right.id)
      || compareStrings(left.symbol, right.symbol);
  }
  if (key === "symbol") {
    return compareStrings(left.symbol, right.symbol);
  }
  if (key === "analysis_score") {
    return compareNullableNumbers(
      analysisOverviewSortScore(left.analysis),
      analysisOverviewSortScore(right.analysis),
    );
  }
  if (key.startsWith("indicator_")) {
    const indicatorId = key.slice("indicator_".length);
    return compareNullableNumbers(
      left.indicator_values?.[indicatorId]?.value,
      right.indicator_values?.[indicatorId]?.value,
    );
  }
  const numericResult = compareNullableNumbers(left[key], right[key]);
  if (numericResult !== 0) {
    return numericResult;
  }
  return compareStrings(left.symbol, right.symbol);
}

function analysisOverviewSortScore(analysis) {
  const displayedScores = (analysis?.headline_trends || [])
    .slice(0, 2)
    .map((trend) => Number(trend.score))
    .filter(Number.isFinite);
  if (displayedScores.length) {
    return Math.max(...displayedScores);
  }
  if (Array.isArray(analysis?.headline_trends)) {
    return 0;
  }
  const fallback = Number(analysis?.highest_score);
  return Number.isFinite(fallback) ? fallback : 0;
}

function compareNullableNumbers(left, right) {
  const leftNumber = Number(left);
  const rightNumber = Number(right);
  const leftMissing = left === null || left === undefined || Number.isNaN(leftNumber);
  const rightMissing = right === null || right === undefined || Number.isNaN(rightNumber);
  if (leftMissing && rightMissing) {
    return 0;
  }
  if (leftMissing) {
    return 1;
  }
  if (rightMissing) {
    return -1;
  }
  return leftNumber - rightNumber;
}

function compareStrings(left, right) {
  return String(left || "").localeCompare(String(right || ""), "en-US", {
    numeric: true,
    sensitivity: "base",
  });
}

function applyOverviewSort(key) {
  if (key === defaultOverviewSort.key) {
    const nextDirection = overviewSort.key === key && overviewSort.direction === "asc" ? "desc" : "asc";
    overviewSort = { key, direction: nextDirection };
    renderOverviewTable(getSortedOverviewItems());
    return;
  }

  if (overviewSort.key !== key) {
    overviewSort = { key, direction: "desc" };
  } else if (overviewSort.direction === "desc") {
    overviewSort = { key, direction: "asc" };
  } else {
    overviewSort = { ...defaultOverviewSort };
  }
  renderOverviewTable(getSortedOverviewItems());
}

function isOverviewManualOrderMode() {
  return overviewSort.key === defaultOverviewSort.key && overviewSort.direction === defaultOverviewSort.direction;
}

function overviewDragTitle() {
  return isOverviewManualOrderMode() ? "拖动排序" : "切回标的代码升序后可拖动排序";
}

function overviewDragDisabledText() {
  return isOverviewManualOrderMode() ? "" : "true";
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

function formatOverviewIndicator(value, indicator) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  const number = Number(value);
  if (indicator.indicator_type === "RAPID_DROP") {
    return number >= 0.5 ? "1" : "0";
  }
  if (["RATR", "WTME"].includes(indicator.indicator_type)) {
    return `${number >= 0 ? "+" : ""}${number.toFixed(2)}`;
  }
  return number.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function formatOverviewUpdatedAt(value) {
  if (!value) {
    return "-";
  }
  const normalized = String(value).replace(/\+00:00$/, "Z");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).replace(/\//g, "-");
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
  const updatingSymbol = currentSymbol;
  chartSource.textContent = "更新中";
  const includeIntraday = includeIntradayData.checked;
  setStatus(
    includeIntraday
      ? `正在检查并补齐 ${currentSymbol} 的日线和分钟数据...`
      : `正在检查并补齐 ${currentSymbol} 的日线数据...`,
    "neutral",
  );

  try {
    const response = await fetch("/api/market-data/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: currentSymbol,
        include_intraday: includeIntraday,
        background: true,
      }),
    });
    const started = await parseJsonResponse(response);
    if (!started.ok) {
      const error = started.error || {};
      setStatus(error.message || "行情数据更新失败。", "error");
      chartSource.textContent = error.code || "更新失败";
      return;
    }
    renderMarketUpdateProgress(started.job);
    const payload = await waitForMarketDataUpdate(started.job.id, {
      shouldRender: () => currentSymbol === updatingSymbol,
    });
    if (currentSymbol !== updatingSymbol) return;

    currentRawMarketData = payload.data;
    currentCorporateActions = payload.corporate_actions || [];
    renderCorporateActionEvents();
    currentSymbolSettings = payload.symbol_settings || currentSymbolSettings;
    currentIntradaySync = payload.intraday_sync || currentIntradaySync;
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    renderCurrentMarketData();
    await loadSymbolIndicators();
    updateChartTitle(payload.symbol);
    chartSource.textContent = sourceText(payload.source);
    updateDetailActions();
    if (getChartPeriod() !== "1D") {
      await loadPeriodMarketData(getChartPeriod(), { silent: true });
    }

    const firstDate = payload.data[0]?.date || "-";
    const lastDate = payload.data[payload.data.length - 1]?.date || "-";
    const actionText = includeIntraday && payload.source === "alpaca" && payload.derived_daily
      ? "已从 Alpaca 更新分钟数据并重建日线"
      : payload.source === "alpaca"
        ? "已从 Alpaca 更新日线"
      : payload.source === "api"
        ? "已从 API 更新"
        : "数据库已完整";
    if (payload.warning) {
      setStatus(payload.warning, "warning");
    } else {
      setStatus(
        `${actionText}：${payload.symbol} 共 ${payload.data.length} 条数据，范围 ${firstDate} 至 ${lastDate}。`,
        "success",
      );
    }
  } catch (error) {
    if (currentSymbol === updatingSymbol) {
      setStatus(error.message || "行情数据更新失败。", "error");
      chartSource.textContent = "更新失败";
    }
  } finally {
    updateDataButton.disabled = false;
  }
}

function formatMarketUpdateDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]}年${match[2]}月${match[3]}日` : "";
}

function renderMarketUpdateProgress(job) {
  if (!job) return;
  const progress = Math.max(0, Math.min(1, Number(job.progress || 0)));
  const percent = Math.round(progress * 100);
  const dateText = formatMarketUpdateDate(job.current_date);
  let message = job.message || "正在更新行情数据";
  if (job.running && dateText) {
    message = `已更新至 ${dateText}`;
    if (Number(job.pages || 0) > 0) {
      message += ` · ${Number(job.pages)} 页 / ${Number(job.rows || 0).toLocaleString()} 条分钟线`;
    }
  } else if (!job.running && !job.error && dateText) {
    message = `更新完成，最新数据为 ${dateText}`;
  }
  marketUpdateProgress.hidden = false;
  marketUpdateProgressContent.hidden = false;
  corporateActionEvents.hidden = true;
  marketUpdateProgress.classList.toggle("is-error", Boolean(job.error));
  marketUpdateProgressText.textContent = job.error?.message || message;
  marketUpdateProgressPercent.textContent = `${percent}%`;
  marketUpdateProgressBar.style.width = `${percent}%`;
  const track = marketUpdateProgress.querySelector("[role='progressbar']");
  track.setAttribute("aria-valuenow", String(percent));
  track.setAttribute("aria-valuetext", marketUpdateProgressText.textContent);
}

async function waitForMarketDataUpdate(jobId, options = {}) {
  while (true) {
    const response = await fetch(`/api/market-data/update-status/${encodeURIComponent(jobId)}`);
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      throw new Error(payload.error?.message || "行情更新进度读取失败。");
    }
    const job = payload.job;
    if (!options.shouldRender || options.shouldRender()) {
      renderMarketUpdateProgress(job);
    }
    if (job.running) {
      await sleep(3000);
      continue;
    }
    if (job.error) {
      throw new Error(job.error.message || "行情数据更新失败。");
    }
    return job.result;
  }
}

function updateDetailActions() {
  const inOverview = Boolean(currentSymbolSettings.show_in_overview);
  overviewSymbolToggle.classList.toggle("is-active", inOverview);
  overviewSymbolToggle.title = inOverview ? "从行情总览移除" : "加入行情总览";
  overviewSymbolToggle.setAttribute("aria-label", overviewSymbolToggle.title);
  overviewSymbolToggle.innerHTML = inOverview
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l4 4 10-10" /></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>';

}

async function toggleCurrentOverview() {
  if (!currentSymbol) {
    return;
  }
  const nextValue = !Boolean(currentSymbolSettings.show_in_overview);
  overviewSymbolToggle.disabled = true;
  try {
    const response = await fetch(
      `/api/market-overview/${encodeURIComponent(currentSymbol)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ show_in_overview: nextValue }),
      },
    );
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      throw new Error(payload.error?.message || "行情总览设置失败。");
    }
    currentSymbolSettings = payload.symbol_settings || currentSymbolSettings;
    updateDetailActions();
    setStatus(
      nextValue
        ? `已将 ${currentSymbol} 加入行情总览。`
        : `已将 ${currentSymbol} 从行情总览移除。`,
      "success",
    );
  } catch (error) {
    setStatus(error.message || "行情总览设置失败。", "error");
  } finally {
    overviewSymbolToggle.disabled = false;
  }
}

function renderCurrentMarketData() {
  const rows = currentSymbolSettings.show_weekend_data
    ? currentRawMarketData
    : currentRawMarketData.filter((row) => !isWeekendDate(row.date));
  renderCandles(rows);
  clearTrendlineAnalysis();
}

function renderCorporateActionEvents() {
  const labels = {
    forward_split: "正向拆股",
    reverse_split: "反向拆股",
    cash_dividend: "现金分红",
    name_change: "代码变更",
    spin_off: "分拆上市",
    cash_merger: "现金并购",
  };
  if (!currentCorporateActions.length) {
    corporateActionEvents.hidden = true;
    corporateActionEvents.textContent = "";
    marketUpdateProgressContent.hidden = true;
    marketUpdateProgress.hidden = true;
    return;
  }
  const items = currentCorporateActions.slice(-8).map((action) => {
    let detail = "";
    if (["forward_split", "reverse_split"].includes(action.action_type)) {
      detail = ` ${action.old_rate || 1}:${action.new_rate || 1}`;
    } else if (action.action_type === "cash_dividend") {
      detail = ` 每股 ${action.cash_rate}`;
    }
    return `${action.effective_date} ${labels[action.action_type] || action.action_type}${detail}`;
  });
  const omitted = currentCorporateActions.length - items.length;
  corporateActionEvents.textContent = `${omitted > 0 ? `另有 ${omitted} 项 · ` : ""}${items.join(" · ")}`;
  corporateActionEvents.hidden = false;
  marketUpdateProgressContent.hidden = true;
  marketUpdateProgress.hidden = false;
}

async function runTrendlineAnalysis() {
  if (!currentSymbol) {
    setStatus("请先输入并加载一个标的。", "error");
    return;
  }
  if (/^(?:[1-9]\d{0,2}m|[1-9]\d?h)$/.test(getChartPeriod())) {
    setStatus("智能趋势线当前仍仅支持日线周期；分时K线可正常使用 MA/EMA 指标。", "warning");
    return;
  }
  if (analysisAlgorithm.value !== "trendlines") {
    setStatus("当前算法暂未实现。", "error");
    return;
  }

  runAnalysisButton.disabled = true;
  setStatus(`正在识别 ${currentSymbol} 最新150根${getChartPeriodLabel()}的直线趋势线...`, "neutral");
  try {
    const params = new URLSearchParams({
      symbol: currentSymbol,
      period: getChartPeriod(),
      limit: "150",
      show_weekend_data: currentSymbolSettings.show_weekend_data ? "1" : "0",
      adjustment: priceAdjustmentMode.value,
    });
    const response = await fetch(`/api/analysis/trendlines?${params}`);
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "趋势线识别失败。", "error");
      clearTrendlineAnalysis();
      return;
    }
    const trendlines = decorateTrendlines(payload.trends || []);
    setChartTrendlines(trendlines);
    renderTrendlineLegend(trendlines);
    const count = trendlines.length;
    const suffix = payload.message ? ` ${payload.message}` : "";
    setStatus(
      count ? `已识别 ${count} 条直线趋势线。` : `未识别出满足阈值的趋势线。${suffix}`,
      count ? "success" : "warning",
    );
  } catch (error) {
    clearTrendlineAnalysis();
    setStatus(error.message || "趋势线识别失败。", "error");
  } finally {
    runAnalysisButton.disabled = false;
  }
}

async function loadStoredTrendlineAnalysis(symbol) {
  try {
    const params = new URLSearchParams({ symbol });
    const response = await fetch(`/api/analysis-overview/snapshot?${params}`);
    const result = await parseJsonResponse(response);
    const snapshot = result.ok ? result.snapshot : null;
    if (!snapshot?.payload) {
      clearTrendlineAnalysis();
      setStatus(`${symbol} 尚无分析快照，可点击智能识别立即计算。`, "neutral");
      return;
    }
    if (
      Boolean(snapshot.show_weekend_data)
      !== Boolean(currentSymbolSettings.show_weekend_data)
    ) {
      clearTrendlineAnalysis();
      setStatus(`${symbol} 的图表设置已变化，请重新点击智能识别。`, "neutral");
      return;
    }
    const trendlines = decorateTrendlines(snapshot.payload.trends || []);
    setChartTrendlines(trendlines);
    renderTrendlineLegend(trendlines);
    const stale = snapshot.latest_data_date !== currentRawMarketData.at(-1)?.date;
    setStatus(
      stale
        ? `已显示 ${symbol} 的上次趋势线结果，后台正在更新。`
        : `已加载 ${symbol} 的趋势线分析，共 ${trendlines.length} 条。`,
      stale ? "warning" : "success",
    );
  } catch (error) {
    clearTrendlineAnalysis();
    setStatus(error.message || "趋势线快照加载失败。", "warning");
  }
}

function clearTrendlineAnalysis() {
  if (typeof clearChartTrendlines === "function") {
    clearChartTrendlines();
  }
  trendlineLegend.innerHTML = "";
  trendlineLegend.hidden = true;
}

function renderTrendlineLegend(trendlines) {
  const sortedTrendlines = [...trendlines].sort(
    (left, right) => Number(right.tier_score || 0) - Number(left.tier_score || 0),
  );
  if (!sortedTrendlines.length || currentWorkspaceMode !== "analysis") {
    trendlineLegend.innerHTML = "";
    trendlineLegend.hidden = true;
    return;
  }

  trendlineLegend.innerHTML = sortedTrendlines.map((line) => {
    const visible = line.visible !== false;
    const visibilityClass = visible ? "" : " is-hidden";
    const lineStyleClass = Number(line.tier_score || 0) >= 75 ? "" : " is-dashed";
    const latestPoint = line.active
      ? `<span class="trendline-point">@${formatOverviewPrice(line.projection_end_price)}</span>`
      : "";
    return `
    <div class="trendline-row${visibilityClass}" data-trendline-id="${escapeHtml(line.id)}">
      <button class="legend-button${visibilityClass}" type="button" data-action="toggle-trendline" title="${visible ? "隐藏" : "显示"}">
        ${eyeIcon(visible)}
      </button>
      <span class="trendline-swatch${lineStyleClass}" style="border-top-color:${escapeHtml(line.color)}"></span>
      <span class="trendline-name">${escapeHtml(trendlineName(line))}</span>
      <span class="trendline-value">${Number(line.tier_score).toFixed(1)}${latestPoint}</span>
    </div>
  `;
  }).join("");
  trendlineLegend.hidden = false;
  if (typeof updateTrendlineLegendPlacement === "function") {
    updateTrendlineLegendPlacement();
  }
}

function decorateTrendlines(trendlines) {
  const ranked = [...trendlines].sort(
    (left, right) => Number(right.tier_score || 0) - Number(left.tier_score || 0),
  );
  const colorById = new Map(
    ranked.map((line, index) => [line.id, trendlineColor(index)]),
  );
  return trendlines.map((line, index) => {
    return {
      ...line,
      color: colorById.get(line.id) || trendlineColor(index),
      color_index: index,
    };
  });
}

function trendlineColor(index) {
  const colors = [
    "#2563eb",
    "#dc2626",
    "#0f9d8a",
    "#7c3aed",
    "#d97706",
    "#0891b2",
    "#be185d",
    "#65a30d",
  ];
  return colors[index % colors.length];
}

function trendlineName(line) {
  const tierLabels = { long: "L", medium: "M", short: "S-now" };
  const directionLabels = { up: "上涨", down: "下跌" };
  const statusLabels = {
    trending: "趋势中",
    challenging: "挑战中",
    broken: "已结束",
  };
  const tierLabel = line.tier === "short" && !line.active
    ? "S"
    : (tierLabels[line.tier] || line.tier);
  return `${tierLabel} ${directionLabels[line.direction] || line.direction} ${statusLabels[line.status] || line.status}`;
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
  try {
    const response = await fetch("/api/indicators?favorite=1");
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      favoriteIndicators.textContent = "收藏指标加载失败";
      return;
    }

    indicatorCatalog = payload.indicators;
    renderFavoriteIndicators();
    renderOverviewIndicatorControls();
  } catch (error) {
    favoriteIndicators.textContent = "收藏指标加载失败";
    renderOverviewIndicatorControls();
  }
}

function initializeOverviewIndicatorSelection() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(overviewIndicatorStorageKey) || "[]");
    if (Array.isArray(stored)) {
      overviewIndicatorIds = [0, 1, 2].map((index) => {
        const value = Number(stored[index]);
        return Number.isInteger(value) && value > 0 ? String(value) : "";
      });
    }
  } catch (error) {
    overviewIndicatorIds = ["", "", ""];
  }
}

function renderOverviewIndicatorControls() {
  const favoriteIds = new Set(indicatorCatalog.map((indicator) => String(indicator.id)));
  let changed = false;
  overviewIndicatorIds = overviewIndicatorIds.map((indicatorId) => {
    if (indicatorId && !favoriteIds.has(indicatorId)) {
      changed = true;
      return "";
    }
    return indicatorId;
  });
  const seen = new Set();
  overviewIndicatorIds = overviewIndicatorIds.map((indicatorId) => {
    if (indicatorId && seen.has(indicatorId)) {
      changed = true;
      return "";
    }
    if (indicatorId) seen.add(indicatorId);
    return indicatorId;
  });
  while (overviewIndicatorIds.length < 3) {
    overviewIndicatorIds.push("");
    changed = true;
  }
  overviewIndicatorIds = overviewIndicatorIds.slice(0, 3);
  if (changed) {
    saveOverviewIndicatorSelection();
  }
  if (currentWorkspaceMode === "market" && !currentSymbol && !marketOverviewPanel.hidden) {
    renderCachedMarketOverview();
  }
}

function renderOverviewIndicatorSelect(index) {
  const selectedId = overviewIndicatorIds[index] || "";
  const selectedIndicator = indicatorCatalog.find(
    (indicator) => String(indicator.id) === selectedId,
  );
  const options = indicatorCatalog.map((indicator) => {
    const value = String(indicator.id);
    return `<option value="${value}"${value === selectedId ? " selected" : ""}>${escapeHtml(indicator.name)}</option>`;
  }).join("");
  return `
    <select
      id="overview-indicator-${index + 1}"
      class="overview-indicator-select"
      data-overview-indicator-index="${index}"
      aria-label="行情总览指标列 ${index + 1}"
      title="${escapeHtml(selectedIndicator?.name || `选择指标列 ${index + 1}`)}"
    >
      <option value=""${selectedId ? "" : " selected"}></option>
      ${options}
    </select>
  `;
}

function renderOverviewIndicatorHeader(index, indicator) {
  const sortKey = indicator ? `indicator_${indicator.id}` : "";
  const isActive = Boolean(sortKey) && overviewSort.key === sortKey;
  const direction = isActive ? overviewSort.direction : "";
  const sortTitle = indicator
    ? `${indicator.name}${isActive ? (direction === "asc" ? " 升序" : " 降序") : ""}`
    : "请先选择指标";
  return `
    <th class="overview-indicator-column">
      <span class="overview-indicator-header">
        ${renderOverviewIndicatorSelect(index)}
        <button
          class="overview-sort-button ${isActive ? "is-active" : ""}"
          type="button"
          title="${escapeHtml(sortTitle)}"
          aria-label="${escapeHtml(sortTitle)}"
          data-sort-key="${escapeHtml(sortKey)}"
          data-sort-direction="${escapeHtml(direction)}"
          ${sortKey ? "" : "disabled"}
        >
          <span class="sort-triangle sort-triangle-up"></span>
          <span class="sort-triangle sort-triangle-down"></span>
        </button>
      </span>
    </th>
  `;
}

function saveOverviewIndicatorSelection() {
  window.localStorage.setItem(
    overviewIndicatorStorageKey,
    JSON.stringify(overviewIndicatorIds),
  );
}

async function changeOverviewIndicatorColumn(index, value) {
  const normalized = value ? String(Number(value)) : "";
  const duplicateIndex = overviewIndicatorIds.findIndex((item, itemIndex) => (
    itemIndex !== index && item && item === normalized
  ));
  if (duplicateIndex >= 0) {
    overviewIndicatorIds[duplicateIndex] = "";
  }
  overviewIndicatorIds[index] = normalized;
  saveOverviewIndicatorSelection();
  renderOverviewIndicatorControls();
  overviewSort = { ...defaultOverviewSort };
  try {
    await fetchAndRenderMarketOverview();
  } catch (error) {
    setStatus(error.message || "总览指标列加载失败。", "error");
  }
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

  currentSymbolIndicators = currentWorkspaceMode === "analysis"
    ? payload.indicators.map((indicator) => ({ ...indicator, visible: false }))
    : payload.indicators;
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
  const params = { period };
  if (indicatorType === "WTME") {
    params.half_life = Number(customIndicatorHalfLife.value);
    params.epsilon = Number(customIndicatorEpsilon.value);
  } else if (indicatorType === "RAPID_DROP") {
    params.threshold_percent = Number(customIndicatorThreshold.value);
  }
  const displayName = indicatorType === "RATR"
    ? `相对ATR${period}`
    : indicatorType === "WTME"
      ? `WTME${period}(h=${params.half_life})`
      : indicatorType === "RAPID_DROP"
        ? `急跌过滤${period}日${params.threshold_percent}%`
        : `${indicatorType}${period}`;
  const response = await fetch(
    `/api/symbols/${encodeURIComponent(currentSymbol)}/chart-views/${encodeURIComponent(currentViewCode)}/indicators`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        indicator_type: indicatorType,
        params,
        name: displayName,
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
  setStatus(`已添加 ${displayName}。`, "success");
}

function updateCustomIndicatorFields() {
  const isWtme = customIndicatorType.value === "WTME";
  const isRapidDrop = customIndicatorType.value === "RAPID_DROP";
  customIndicatorHalfLife.hidden = !isWtme;
  customIndicatorEpsilon.hidden = !isWtme;
  customIndicatorThreshold.hidden = !isRapidDrop;
  customIndicatorPeriod.min = isRapidDrop ? "1" : "2";
  customIndicatorPeriod.setAttribute(
    "aria-label",
    isRapidDrop ? "急跌观察变化段数" : "指标周期",
  );
}

async function handleIndicatorAction(event) {
  const { action, symbolIndicatorId, indicatorId } = event.detail;
  const symbolIndicator = currentSymbolIndicators.find((item) => item.id === symbolIndicatorId);
  if (!symbolIndicator) {
    return;
  }

  if (action === "toggle-visible") {
    if (currentWorkspaceMode === "analysis") {
      currentSymbolIndicators = currentSymbolIndicators.map((indicator) => (
        indicator.id === symbolIndicatorId
          ? { ...indicator, visible: !indicator.visible }
          : indicator
      ));
      setChartIndicators(currentSymbolIndicators);
      return;
    }
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
      applyWorkspaceMode(button.dataset.mode || "market");

      if (button.dataset.view === "database-view") {
        loadTables();
      } else if (button.dataset.view === "realtime-view") {
        if (typeof loadRealtimeTasks === "function") {
          loadRealtimeTasks();
        }
      } else if (button.dataset.view === "market-view" && currentWorkspaceMode === "analysis") {
        if (!currentSymbol) {
          loadAnalysisOverview();
        } else {
          showMarketDetail();
          loadStoredTrendlineAnalysis(currentSymbol);
        }
      } else if (button.dataset.view === "market-view") {
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
  shutdownNotice.hidden = false;

  if (heartbeatTimer) {
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }

  try {
    await fetch("/api/system/shutdown", { method: "POST", keepalive: true });
  } catch (error) {
    // The server may close the connection while exiting.
  }

  document.querySelectorAll("button, input, select").forEach((element) => {
    element.disabled = true;
  });
}

symbolForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadMarketData(symbolInput.value, {
    includeIntraday: includeIntradayData.checked,
  });
});
backToOverview.addEventListener("click", () => {
  marketLoadRequestId += 1;
  currentSymbol = "";
  symbolInput.value = "";
  renderCandles([]);
  setChartIndicators([]);
  clearTrendlineAnalysis();
  if (currentWorkspaceMode === "analysis") {
    loadAnalysisOverview();
  } else {
    loadMarketOverview(overviewPage);
  }
});
overviewPrev.addEventListener("click", () => {
  loadMarketOverview(Math.max(1, overviewPage - 1));
});
overviewNext.addEventListener("click", () => {
  loadMarketOverview(Math.min(overviewTotalPages, overviewPage + 1));
});
overviewTable.addEventListener("click", (event) => {
  const sortButton = event.target.closest(".overview-sort-button");
  if (sortButton && !sortButton.disabled) {
    event.stopPropagation();
    applyOverviewSort(sortButton.dataset.sortKey);
    return;
  }
  if (event.target.closest(".drag-handle")) {
    return;
  }
  const removeButton = event.target.closest(".overview-remove-button");
  if (removeButton) {
    event.stopPropagation();
    hideOverviewSymbol(removeButton.dataset.symbol);
    return;
  }
  const row = event.target.closest("tr[data-symbol]");
  if (!row) {
    return;
  }
  loadMarketData(row.dataset.symbol);
});
overviewTable.addEventListener("change", (event) => {
  const select = event.target.closest(".overview-indicator-select");
  if (!select) return;
  const index = Number(select.dataset.overviewIndicatorIndex);
  if (Number.isInteger(index) && index >= 0 && index < 3) {
    changeOverviewIndicatorColumn(index, select.value);
  }
});
overviewTable.addEventListener("mouseover", (event) => {
  const target = event.target.closest("[data-analysis-trend-tooltip]");
  if (target) {
    showAnalysisTrendTooltip(target, event.clientX, event.clientY);
  }
});
overviewTable.addEventListener("mousemove", (event) => {
  const target = event.target.closest("[data-analysis-trend-tooltip]");
  if (target && !analysisTrendTooltip.hidden) {
    positionAnalysisTrendTooltip(target, event.clientX, event.clientY);
  }
});
overviewTable.addEventListener("mouseout", (event) => {
  const target = event.target.closest("[data-analysis-trend-tooltip]");
  if (target && !target.contains(event.relatedTarget)) {
    hideAnalysisTrendTooltip();
  }
});
overviewTable.addEventListener("focusin", (event) => {
  const target = event.target.closest("[data-analysis-trend-tooltip]");
  if (target) {
    showAnalysisTrendTooltip(target);
  }
});
overviewTable.addEventListener("focusout", (event) => {
  if (event.target.closest("[data-analysis-trend-tooltip]")) {
    hideAnalysisTrendTooltip();
  }
});
overviewTable.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const row = event.target.closest("tr[data-symbol]");
  if (!row || currentWorkspaceMode !== "analysis") {
    return;
  }
  event.preventDefault();
  loadMarketData(row.dataset.symbol);
});
overviewTable.addEventListener("dragstart", (event) => {
  const handle = event.target.closest(".drag-handle");
  const row = event.target.closest("tr[data-symbol]");
  if (!handle || !row) {
    event.preventDefault();
    return;
  }
  if (!isOverviewManualOrderMode()) {
    event.preventDefault();
    setStatus("请先切回标的代码升序，再拖动保存默认排序。", "neutral");
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
overviewLiveToggle.addEventListener("change", () => {
  setOverviewLiveRefresh(overviewLiveToggle.checked);
});
analysisRefreshAll.addEventListener("click", () => startAnalysisOverviewRefresh());
themeToggle.addEventListener("click", () => {
  const nextTheme = document.body.classList.contains("theme-dark") ? "light" : "dark";
  applyTheme(nextTheme);
});
indicatorPanelToggle.addEventListener("click", () => {
  indicatorPanel.hidden = !indicatorPanel.hidden;
  symbolSettingsPanel.hidden = true;
  if (!indicatorPanel.hidden) {
    if (currentWorkspaceMode === "analysis") {
      analysisIndicatorLegendVisible = true;
      updateIndicatorLegendVisibility();
    }
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
customIndicatorType.addEventListener("change", updateCustomIndicatorFields);
updateDataButton.addEventListener("click", updateCurrentMarketData);
overviewSymbolToggle.addEventListener("click", toggleCurrentOverview);
runAnalysisButton.addEventListener("click", runTrendlineAnalysis);
trendlineLegend.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action='toggle-trendline']");
  if (!button) {
    return;
  }
  const row = button.closest(".trendline-row");
  const lineId = row?.dataset.trendlineId;
  if (!lineId || typeof setChartTrendlineVisible !== "function") {
    return;
  }
  const line = getChartTrendlines().find((item) => item.id === lineId);
  if (!line) {
    return;
  }
  setChartTrendlineVisible(lineId, line.visible === false);
  renderTrendlineLegend(getChartTrendlines());
});
symbolSettingsToggle.addEventListener("click", () => {
  symbolSettingsPanel.hidden = !symbolSettingsPanel.hidden;
  indicatorPanel.hidden = true;
});
symbolSettingsClose.addEventListener("click", () => {
  symbolSettingsPanel.hidden = true;
});
showWeekendData.addEventListener("change", saveSymbolSettings);
priceAdjustmentMode.addEventListener("change", async () => {
  if (!currentSymbol) return;
  clearTrendlineAnalysis();
  try {
    await loadPeriodMarketData(getChartPeriod());
    await loadSymbolIndicators();
  } catch (error) {
    setStatus(error.message || "切换复权方式失败。", "error");
  }
});
document.addEventListener("indicator-action", handleIndicatorAction);
document.addEventListener("chart-period-change", async (event) => {
  updateChartTitle(symbolInput.value.trim().toUpperCase() || "行情");
  currentViewCode = getChartPeriod();
  clearTrendlineAnalysis();
  try {
    await loadPeriodMarketData(event.detail.period);
    await loadSymbolIndicators();
    if (currentWorkspaceMode === "analysis" && currentSymbol) {
      setStatus("K线周期已切换，请重新点击智能识别。", "neutral");
    }
  } catch (error) {
    renderCandles([]);
    setStatus(error.message || "K线周期切换失败。", "error");
  }
});

bindNavigation();
bindDatabaseBrowser();
initBacktest();
initRealtime();
initChart();
initTheme();
updateCustomIndicatorFields();
startHeartbeat();
initializeOverviewIndicatorSelection();
loadOverviewRefreshPreference();
loadIndicatorCatalog().finally(() => loadMarketOverview());

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
