const statusEl = document.getElementById("market-status");
const symbolForm = document.getElementById("symbol-form");
const symbolInput = document.getElementById("symbol-input");
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
const updateDataButton = document.getElementById("update-data-button");
const symbolSettingsToggle = document.getElementById("symbol-settings-toggle");
const symbolSettingsPanel = document.getElementById("symbol-settings-panel");
const symbolSettingsClose = document.getElementById("symbol-settings-close");
const showWeekendData = document.getElementById("show-weekend-data");
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
let currentRawMarketData = [];
let currentSymbolSettings = { show_weekend_data: true };
let overviewPage = 1;
let overviewTotalPages = 1;
let overviewItems = [];
let marketOverviewItems = [];
let analysisOverviewItems = [];
const defaultOverviewSort = { key: "display_order", direction: "asc" };
let overviewSort = { ...defaultOverviewSort };
let draggedOverviewSymbol = "";
let overviewDailySyncDone = false;
let overviewLiveTimer;
let overviewLiveRefreshInFlight = false;
let marketOverviewAutoUpdate = false;
let analysisOverviewAutoUpdate = false;
let analysisOverviewTimer;
let analysisOverviewStatusTimer;
let analysisOverviewLoadInFlight;
let lastAnalysisRefreshState = {};
let analysisIndicatorLegendVisible = false;
let overviewLoadInFlight;
const overviewLiveRefreshMs = 5 * 60 * 1000;

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
  overviewLiveToggle.checked = isAnalysis
    ? analysisOverviewAutoUpdate
    : marketOverviewAutoUpdate;
  overviewLiveToggle.parentElement.title = isAnalysis
    ? "定期检查并更新直线趋势线分析"
    : "每5分钟刷新总览最新价格";
  analysisOverviewProgress.hidden = !isAnalysis;
  if (isAnalysis) {
    if (overviewLiveTimer) {
      window.clearInterval(overviewLiveTimer);
      overviewLiveTimer = null;
    }
    if (analysisOverviewAutoUpdate && !analysisOverviewTimer) {
      analysisOverviewTimer = window.setInterval(
        () => startAnalysisOverviewRefresh({ silent: true }),
        overviewLiveRefreshMs,
      );
    }
    if (!marketOverviewPanel.hidden) {
      renderAnalysisProgress(lastAnalysisRefreshState);
    }
  } else {
    if (analysisOverviewTimer) {
      window.clearInterval(analysisOverviewTimer);
      analysisOverviewTimer = null;
    }
    if (marketOverviewAutoUpdate && !overviewLiveTimer) {
      overviewLiveTimer = window.setInterval(
        refreshOverviewLivePrices,
        overviewLiveRefreshMs,
      );
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
  clearTrendlineAnalysis();
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
    currentSymbol = payload.canonical_symbol || payload.symbol || normalized;
    symbolInput.value = payload.symbol || normalized;
    showWeekendData.checked = Boolean(currentSymbolSettings.show_weekend_data);
    renderCurrentMarketData();
    await loadSymbolIndicators();
    updateChartTitle(payload.symbol);
    chartSource.textContent = sourceText(payload.source);

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
  } catch (error) {
    setStatus(error.message || "前端无法连接本地服务，请确认后端仍在运行。", "error");
    chartSource.textContent = "连接失败";
  }
}

async function loadMarketOverview(page = overviewPage) {
  if (overviewLoadInFlight) {
    return overviewLoadInFlight;
  }
  overviewLoadInFlight = loadMarketOverviewInner(page).finally(() => {
    overviewLoadInFlight = null;
  });
  return overviewLoadInFlight;
}

async function loadMarketOverviewInner(page = overviewPage) {
  overviewPage = page;
  if (currentWorkspaceMode === "market") {
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
        if (currentWorkspaceMode === "market") {
          setStatus(error.message || "行情总览日K补齐失败，已保留本地数据。", "warning");
        }
      }
    }
    if (!syncFailed && currentWorkspaceMode === "market") {
      setStatus("行情总览已加载。", "success");
    }
  } catch (error) {
    if (currentWorkspaceMode === "market") {
      setStatus(error.message || "行情总览加载失败。", "error");
      overviewSummary.textContent = "加载失败";
      marketOverviewItems = [];
      overviewItems = [];
      renderOverviewTable([]);
    }
  }
}

async function fetchAndRenderMarketOverview(options = {}) {
  const response = await fetch("/api/market-overview");
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    throw new Error(payload.error?.message || "行情总览加载失败。");
  }

  overviewPage = payload.page;
  overviewTotalPages = payload.total_pages;
  marketOverviewItems = payload.items || [];
  if (currentWorkspaceMode === "market") {
    overviewItems = marketOverviewItems;
    renderOverviewTable(getSortedOverviewItems());
    overviewSummary.textContent = `共 ${payload.total_rows} 个标的`;
    overviewPageText.textContent = "";
    overviewPagination.hidden = true;
    overviewPrev.disabled = true;
    overviewNext.disabled = true;

    if (!options.silent) {
      setStatus("已显示本地行情总览。", "success");
    }
  }
  return payload;
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
  const hasError = Boolean(state.last_error);
  analysisOverviewProgress.hidden = false;
  analysisOverviewProgress.className = `analysis-overview-progress${running ? " is-running" : ""}${hasError ? " is-error" : ""}`;
  analysisOverviewProgress.innerHTML = running
    ? `<span class="analysis-progress-spinner" aria-hidden="true"></span><span>${escapeHtml(analysisProgressText(state))}</span>`
    : `<span>${escapeHtml(hasError ? state.last_error : analysisCompletionText(state))}</span>`;
}

async function syncMarketOverviewDaily() {
  overviewDailySyncDone = true;
  if (currentWorkspaceMode === "market") {
    overviewSummary.textContent = "补齐日K中";
    setStatus("正在自动补齐行情总览日K...", "neutral");
  }
  const response = await fetch("/api/market-overview/sync-daily", { method: "POST" });
  const payload = await parseJsonResponse(response);
  if (!payload.ok) {
    overviewDailySyncDone = false;
    throw new Error(payload.error?.message || "行情总览日K补齐失败。");
  }

  const result = await waitForOverviewSync();
  const failed = (result.items || []).filter((item) => item.status !== "success").length;
  const suffix = failed ? `，${failed} 个标的需要稍后重试` : "";
  if (currentWorkspaceMode === "market") {
    setStatus(`已自动补齐总览日K，更新 ${result.updated_rows || 0} 条${suffix}。`, failed ? "warning" : "success");
  }
}

async function waitForOverviewSync() {
  for (let attempt = 0; attempt < 24; attempt += 1) {
    await sleep(5000);
    const response = await fetch("/api/market-overview/sync-status");
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      throw new Error(payload.error?.message || "行情总览日K补齐状态读取失败。");
    }
    if (!payload.running) {
      if (payload.last_error) {
        throw new Error(payload.last_error);
      }
      return payload.last_result || { items: [], updated_rows: 0 };
    }
  }
  throw new Error("行情总览日K补齐仍在后台进行，本地数据已先显示。");
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
  const headers = [
    { label: "标的代码", key: "display_order" },
    { label: "最新价格", key: "latest_price" },
    { label: "更新时间", key: "latest_price_updated_at" },
    { label: "日涨跌", key: "daily_change_percent" },
    { label: "周涨跌", key: "weekly_percent" },
    { label: "月涨跌", key: "monthly_percent" },
    { label: "YTD", key: "ytd_percent" },
    { label: "", key: "" },
  ];
  const thead = `<thead><tr>${headers.map(renderOverviewHeader).join("")}</tr></thead>`;

  if (!items.length) {
    overviewTable.innerHTML = `${thead}<tbody><tr><td class="empty-cell" colspan="${headers.length}">暂无标的。查询并保存行情后会显示在这里。</td></tr></tbody>`;
    return;
  }

  const rows = items.map((item) => {
    const dailyClass = numberTone(item.daily_change);
    const weeklyClass = numberTone(item.weekly_percent);
    const monthlyClass = numberTone(item.monthly_percent);
    const ytdClass = numberTone(item.ytd_percent);
    return `
      <tr class="overview-row" draggable="true" data-symbol="${escapeHtml(item.symbol)}">
        <td>
          <button class="symbol-link" type="button" data-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.display_symbol || item.symbol)}</button>
        </td>
        <td class="${dailyClass}">${formatOverviewPrice(item.latest_price)}</td>
        <td class="number-neutral">${formatOverviewUpdatedAt(item.latest_price_updated_at)}</td>
        <td class="${dailyClass}">${formatOverviewPercent(item.daily_change_percent)}</td>
        <td class="${weeklyClass}">${formatOverviewPercent(item.weekly_percent)}</td>
        <td class="${monthlyClass}">${formatOverviewPercent(item.monthly_percent)}</td>
        <td class="${ytdClass}">${formatOverviewPercent(item.ytd_percent)}</td>
        <td class="drag-cell">
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
        </td>
      </tr>
    `;
  });
  overviewTable.innerHTML = `${thead}<tbody>${rows.join("")}</tbody>`;
}

function renderAnalysisOverviewTable(items) {
  overviewTable.classList.add("analysis-overview-table");
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
    marketOverviewItems = payload.items || marketOverviewItems.filter((item) => item.symbol !== symbol);
    overviewItems = marketOverviewItems;
    renderOverviewTable(getSortedOverviewItems());
    overviewSummary.textContent = `共 ${payload.total_rows ?? overviewItems.length} 个标的`;
    setStatus(`已从行情总览隐藏 ${symbol}，历史数据仍保留。`, "success");
  } catch (error) {
    setStatus(error.message || "隐藏标的失败。", "error");
  }
}

async function refreshOverviewLivePrices() {
  if (
    overviewLiveRefreshInFlight
    || marketOverviewPanel.hidden
    || currentWorkspaceMode !== "market"
  ) {
    return;
  }
  overviewLiveRefreshInFlight = true;
  try {
    const response = await fetch("/api/market-overview/refresh-prices", { method: "POST" });
    const payload = await parseJsonResponse(response);
    if (!payload.ok) {
      setStatus(payload.error?.message || "总览实时价格刷新失败。", "error");
      return;
    }
    mergeOverviewLivePrices(payload.items || []);
    setStatus("总览自动更新已启动，先显示本地最新价格。", "neutral");
    try {
      const result = await waitForOverviewSync();
      await fetchAndRenderMarketOverview({ silent: true });
      const failed = (result.items || []).filter((item) => item.status !== "success").length;
      setStatus(failed ? `总览价格已部分刷新，${failed} 个标的未更新。` : "总览价格已刷新。", failed ? "warning" : "success");
    } catch (syncError) {
      setStatus(syncError.message || "后台刷新仍在进行，本地数据已先显示。", "warning");
    }
  } catch (error) {
    setStatus(error.message || "总览实时价格刷新失败。", "error");
  } finally {
    overviewLiveRefreshInFlight = false;
  }
}

function mergeOverviewLivePrices(items) {
  const bySymbol = new Map(items.map((item) => [item.symbol, item]));
  marketOverviewItems = marketOverviewItems.map((item) => {
    const live = bySymbol.get(item.symbol);
    if (!live || live.status !== "success" || live.latest_price === null || live.latest_price === undefined) {
      return item;
    }
    const next = {
      ...item,
      latest_price: live.latest_price,
    };
    if (live.daily_change !== null && live.daily_change !== undefined) {
      next.daily_change = live.daily_change;
    }
    if (live.daily_change_percent !== null && live.daily_change_percent !== undefined) {
      next.daily_change_percent = live.daily_change_percent;
    }
    return next;
  });
  overviewItems = marketOverviewItems;
  renderOverviewTable(getSortedOverviewItems());
}

function setOverviewLiveRefresh(enabled) {
  marketOverviewAutoUpdate = enabled;
  if (overviewLiveTimer) {
    window.clearInterval(overviewLiveTimer);
    overviewLiveTimer = null;
  }
  if (!enabled) {
    setStatus("总览自动更新已关闭。", "neutral");
    return;
  }
  refreshOverviewLivePrices();
  overviewLiveTimer = window.setInterval(refreshOverviewLivePrices, overviewLiveRefreshMs);
  setStatus("总览自动更新已开启，每5分钟刷新一次最新价格。", "success");
}

function setAnalysisOverviewAutoUpdate(enabled) {
  analysisOverviewAutoUpdate = enabled;
  if (analysisOverviewTimer) {
    window.clearInterval(analysisOverviewTimer);
    analysisOverviewTimer = null;
  }
  if (!enabled) {
    setStatus("K 线分析自动更新已关闭。", "neutral");
    return;
  }
  startAnalysisOverviewRefresh();
  analysisOverviewTimer = window.setInterval(
    () => startAnalysisOverviewRefresh({ silent: true }),
    overviewLiveRefreshMs,
  );
  setStatus("K 线分析自动更新已开启，每 5 分钟检查一次数据变化。", "success");
}

function renderOverviewHeader(header) {
  if (!header.key) {
    return "<th></th>";
  }
  const isActive = overviewSort.key === header.key;
  const direction = isActive ? overviewSort.direction : "";
  const title = `${header.label}${isActive ? (direction === "asc" ? " 升序" : " 降序") : ""}`;
  return `
    <th>
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
  clearTrendlineAnalysis();
}

async function runTrendlineAnalysis() {
  if (!currentSymbol) {
    setStatus("请先输入并加载一个标的。", "error");
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
  await loadMarketData(symbolInput.value);
});
backToOverview.addEventListener("click", () => {
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
  if (sortButton) {
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
  if (currentWorkspaceMode === "analysis") {
    setAnalysisOverviewAutoUpdate(overviewLiveToggle.checked);
  } else {
    setOverviewLiveRefresh(overviewLiveToggle.checked);
  }
});
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
updateDataButton.addEventListener("click", updateCurrentMarketData);
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
document.addEventListener("indicator-action", handleIndicatorAction);
document.addEventListener("chart-period-change", async () => {
  updateChartTitle(symbolInput.value.trim().toUpperCase() || "行情");
  currentViewCode = getChartPeriod();
  clearTrendlineAnalysis();
  await loadSymbolIndicators();
  if (currentWorkspaceMode === "analysis" && currentSymbol) {
    setStatus("K线周期已切换，请重新点击智能识别。", "neutral");
  }
});

bindNavigation();
bindDatabaseBrowser();
initChart();
initTheme();
startHeartbeat();
loadIndicatorCatalog();
loadMarketOverview();
startAnalysisOverviewRefresh({ silent: true });

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
