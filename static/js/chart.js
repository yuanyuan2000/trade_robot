const chartState = {
  container: null,
  canvas: null,
  ctx: null,
  tooltip: null,
  trendlineTooltip: null,
  legend: null,
  rawRows: [],
  candles: [],
  indicators: [],
  indicatorSeries: [],
  trendlines: [],
  keyZones: [],
  period: "1D",
  firstVisible: 0,
  visibleCount: 0,
  minVisibleCount: 8,
  dragStartX: 0,
  dragStartFirstVisible: 0,
  isDragging: false,
  hoverIndex: null,
  hoverX: null,
  hoverY: null,
  hoverTrendlineId: null,
  hoverKeyZoneId: null,
  dpr: 1,
  defaultVisibleCount: 150,
  layout: {
    left: 72,
    top: 14,
    right: 72,
    bottom: 34,
  },
};

const periodLabels = {
  "1m": "1分钟K",
  "15m": "15分钟K",
  "1h": "1小时K",
  "4h": "4小时K",
  "1D": "日K",
  "3D": "3日K",
  "1W": "周K",
  "1M": "月K",
};

function initChart() {
  chartState.container = document.getElementById("chart");
  chartState.canvas = document.getElementById("kline-canvas");
  chartState.ctx = chartState.canvas.getContext("2d");
  chartState.tooltip = document.getElementById("ohlcv-tooltip");
  chartState.trendlineTooltip = document.getElementById("trendline-tooltip");
  chartState.legend = document.getElementById("indicator-legend");

  bindChartEvents();
  bindPeriodButtons();
  bindLegendEvents();

  const resizeObserver = new ResizeObserver(() => {
    resizeCanvas();
    clampViewport();
    drawChart();
  });
  resizeObserver.observe(chartState.container);

  resizeCanvas();
  drawChart();
}

function renderCandles(rows) {
  chartState.rawRows = rows.map((row) => ({
    date: row.date,
    endDate: row.endDate,
    open: Number(row.open),
    high: Number(row.high),
    low: Number(row.low),
    close: Number(row.close),
    volume: Number(row.volume || 0),
    is_complete: row.is_complete !== false && Number(row.is_complete ?? 1) !== 0,
    updated_at: row.updated_at,
    price_basis: row.price_basis,
  }));
  rebuildPeriodCandles();
  resetViewportToFullRange();
  drawChart();
}

function setChartIndicators(indicators) {
  chartState.indicators = indicators.map((indicator) => ({
    ...indicator,
    visible: Boolean(indicator.visible),
    is_favorite: Boolean(indicator.is_favorite),
  }));
  recalculateIndicators();
  renderIndicatorLegend();
  drawChart();
}

function setChartTrendlines(trendlines) {
  chartState.trendlines = Array.isArray(trendlines)
    ? trendlines.map((line) => ({ visible: true, ...line, visible: line.visible !== false }))
    : [];
  chartState.hoverTrendlineId = null;
  hideTrendlineTooltip();
  drawChart();
}

function clearChartTrendlines() {
  setChartTrendlines([]);
}

function setChartTrendlineVisible(lineId, visible) {
  const line = chartState.trendlines.find((item) => item.id === lineId);
  if (!line) {
    return;
  }
  line.visible = Boolean(visible);
  drawChart();
}

function getChartTrendlines() {
  return chartState.trendlines.map((line) => ({ ...line }));
}

function setChartKeyZones(zones) {
  const normalized = Array.isArray(zones)
    ? zones.map((zone) => ({
      visible: true,
      ...zone,
      visible: zone.visible !== false,
      project_center_to_current: false,
    }))
    : [];
  const nearestByRole = new Map();
  for (const zone of normalized) {
    const role = zone.current_role;
    const distance = Number(zone.distance_from_current_atr);
    if (
      zone.active === false
      || !["support", "resistance"].includes(role)
      || !Number.isFinite(distance)
      || distance >= 5
    ) {
      continue;
    }
    const current = nearestByRole.get(role);
    if (!current || distance < Number(current.distance_from_current_atr)) {
      nearestByRole.set(role, zone);
    }
  }
  for (const zone of nearestByRole.values()) {
    zone.project_center_to_current = true;
  }
  chartState.keyZones = normalized;
  chartState.hoverKeyZoneId = null;
  hideTrendlineTooltip();
  drawChart();
}

function clearChartKeyZones() {
  setChartKeyZones([]);
}

function setChartKeyZoneVisible(zoneId, visible) {
  const zone = chartState.keyZones.find((item) => item.id === zoneId);
  if (!zone) {
    return;
  }
  zone.visible = Boolean(visible);
  drawChart();
}

function getChartKeyZones() {
  return chartState.keyZones.map((zone) => ({ ...zone }));
}

function getKeyZoneDisplayBounds(zone) {
  return {
    start: Number(
      zone.display_start_index
      ?? zone.start_index
      ?? zone.formation_index,
    ),
    end: Number(
      zone.display_end_index
      ?? zone.latest_confirmed_index
      ?? zone.projection_end_index,
    ),
  };
}

function bindPeriodButtons() {
  document.querySelectorAll(".period-button").forEach((button) => {
    button.addEventListener("click", () => {
      selectChartPeriod(button.dataset.period, button);
    });
  });
  const customForm = document.getElementById("custom-bar-form");
  const customValue = document.getElementById("custom-bar-value");
  const customUnit = document.getElementById("custom-bar-unit");
  customUnit?.addEventListener("change", () => {
    const maximum = customUnit.value === "m" ? 390 : 365;
    customValue.max = String(maximum);
    if (Number(customValue.value) > maximum) {
      customValue.value = String(maximum);
    }
  });
  customForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = Number(customValue.value);
    const unit = customUnit.value;
    const max = unit === "m" ? 390 : 365;
    if (!Number.isInteger(value) || value < 1 || value > max) {
      return;
    }
    selectChartPeriod(`${value}${unit}`);
  });
}

function selectChartPeriod(period, activeButton = null) {
  chartState.period = period;
  if (!periodLabels[period]) {
    const match = period.match(/^(\d+)(m|D)$/);
    periodLabels[period] = match?.[2] === "m"
      ? `${match[1]}分钟K`
      : `${match?.[1] || period}日K`;
  }
  document.querySelectorAll(".period-button").forEach((item) => {
    item.classList.toggle("active", item === activeButton);
  });
  hideTooltip();
  document.dispatchEvent(new CustomEvent("chart-period-change", {
    detail: { period: chartState.period, label: periodLabels[chartState.period] },
  }));
}

function resetChartPeriod(period = "1D") {
  chartState.period = period;
  document.querySelectorAll(".period-button").forEach((item) => {
    item.classList.toggle("active", item.dataset.period === period);
  });
  hideTooltip();
}

function bindLegendEvents() {
  chartState.legend.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    const row = button.closest(".legend-row");
    document.dispatchEvent(new CustomEvent("indicator-action", {
      detail: {
        action: button.dataset.action,
        symbolIndicatorId: Number(row.dataset.symbolIndicatorId),
        indicatorId: Number(row.dataset.indicatorId),
      },
    }));
  });
}

function bindChartEvents() {
  const canvas = chartState.canvas;

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (!chartState.candles.length) {
      return;
    }

    const direction = event.deltaY > 0 ? 1 : -1;
    const zoomFactor = direction > 0 ? 1.18 : 0.84;
    const oldCount = chartState.visibleCount || chartState.candles.length;
    const nextCount = Math.round(oldCount * zoomFactor);
    const chartRect = chartState.canvas.getBoundingClientRect();
    const pointerRatio = getPlotRatio(event.clientX - chartRect.left);
    const anchor = chartState.firstVisible + oldCount * pointerRatio;

    chartState.visibleCount = clamp(
      nextCount,
      getMinimumVisibleCount(),
      getMaximumVisibleCount(),
    );
    chartState.firstVisible = Math.round(anchor - chartState.visibleCount * pointerRatio);
    clampViewport();
    drawChart();
  }, { passive: false });

  canvas.addEventListener("mousedown", (event) => {
    chartState.isDragging = true;
    chartState.dragStartX = event.clientX;
    chartState.dragStartFirstVisible = chartState.firstVisible;
    canvas.classList.add("dragging");
  });

  window.addEventListener("mouseup", () => {
    chartState.isDragging = false;
    canvas.classList.remove("dragging");
  });

  window.addEventListener("mousemove", (event) => {
    const rect = canvas.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left
      && event.clientX <= rect.right
      && event.clientY >= rect.top
      && event.clientY <= rect.bottom;

    if (chartState.isDragging) {
      const candleWidth = getCandleSlotWidth();
      const deltaSlots = Math.round((chartState.dragStartX - event.clientX) / candleWidth);
      chartState.firstVisible = chartState.dragStartFirstVisible + deltaSlots;
      clampViewport();
      drawChart();
      return;
    }

    if (!inside) {
      chartState.hoverIndex = null;
      chartState.hoverX = null;
      chartState.hoverY = null;
      chartState.hoverTrendlineId = null;
      hideTooltip();
      drawChart();
      return;
    }

    updateHover(event.clientX - rect.left, event.clientY - rect.top);
  });

  canvas.addEventListener("mouseleave", () => {
    if (!chartState.isDragging) {
      chartState.hoverIndex = null;
      chartState.hoverX = null;
      chartState.hoverY = null;
      chartState.hoverTrendlineId = null;
      hideTooltip();
      drawChart();
    }
  });
}

function rebuildPeriodCandles() {
  chartState.candles = chartState.rawRows.map((row) => ({ ...row }));
  recalculateIndicators();
}

function aggregateByCount(rows, size) {
  const result = [];
  for (let index = 0; index < rows.length; index += size) {
    result.push(mergeRows(rows.slice(index, index + size)));
  }
  return result.filter(Boolean);
}

function aggregateByCalendar(rows, period) {
  const groups = new Map();

  for (const row of rows) {
    const key = period === "1W" ? getWeekKey(row.date) : row.date.slice(0, 7);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(row);
  }

  return Array.from(groups.values()).map(mergeRows).filter(Boolean);
}

function mergeRows(rows) {
  if (!rows.length) {
    return null;
  }

  const first = rows[0];
  const last = rows[rows.length - 1];
  return {
    date: first.date,
    endDate: last.date,
    open: first.open,
    high: Math.max(...rows.map((row) => row.high)),
    low: Math.min(...rows.map((row) => row.low)),
    close: last.close,
    volume: rows.reduce((sum, row) => sum + row.volume, 0),
  };
}

function getWeekKey(dateText) {
  const date = new Date(`${dateText}T00:00:00`);
  const day = date.getDay() || 7;
  date.setDate(date.getDate() + 4 - day);
  const yearStart = new Date(date.getFullYear(), 0, 1);
  const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
  return `${date.getFullYear()}-W${String(week).padStart(2, "0")}`;
}

function resetViewportToFullRange() {
  chartState.visibleCount = getDefaultVisibleCount();
  chartState.firstVisible = Math.max(0, chartState.candles.length - chartState.visibleCount);
  clampViewport();
}

function resizeCanvas() {
  const rect = chartState.container.getBoundingClientRect();
  chartState.dpr = window.devicePixelRatio || 1;
  chartState.canvas.width = Math.max(1, Math.floor(rect.width * chartState.dpr));
  chartState.canvas.height = Math.max(1, Math.floor(rect.height * chartState.dpr));
  chartState.canvas.style.width = `${rect.width}px`;
  chartState.canvas.style.height = `${rect.height}px`;
  chartState.ctx.setTransform(chartState.dpr, 0, 0, chartState.dpr, 0, 0);
}

function drawChart() {
  const ctx = chartState.ctx;
  const width = chartState.canvas.clientWidth;
  const height = chartState.canvas.clientHeight;
  const theme = getChartTheme();
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = theme.background;
  ctx.fillRect(0, 0, width, height);

  const fullPlot = getPlotArea();
  const oscillatorSeries = getVisibleOscillatorSeries();
  const oscillatorHeight = oscillatorSeries.length
    ? Math.max(100, Math.min(150, fullPlot.height * 0.28))
    : 0;
  const oscillatorGap = oscillatorSeries.length ? 16 : 0;
  const plot = oscillatorSeries.length
    ? {
        ...fullPlot,
        bottom: fullPlot.bottom - oscillatorHeight - oscillatorGap,
        height: fullPlot.height - oscillatorHeight - oscillatorGap,
      }
    : fullPlot;
  const oscillatorPlot = oscillatorSeries.length
    ? {
        ...fullPlot,
        top: plot.bottom + oscillatorGap,
        height: oscillatorHeight,
      }
    : null;
  drawGrid(ctx, plot, theme);
  if (oscillatorPlot) {
    drawGrid(ctx, oscillatorPlot, theme);
  }

  if (!chartState.candles.length) {
    drawEmptyState(ctx, width, height);
    return;
  }

  const visible = getVisibleCandles();
  const priceRange = getPriceRange(visible);
  drawValueAxes(ctx, plot, visible, priceRange, theme);
  drawTimeAxis(ctx, oscillatorPlot || plot, visible, theme);
  drawKeyZones(ctx, plot, priceRange, theme);
  drawCandleSeries(ctx, plot, visible, priceRange, theme);
  drawIndicators(ctx, plot, priceRange);
  if (oscillatorPlot) {
    drawOscillatorIndicators(ctx, oscillatorPlot, oscillatorSeries, theme);
  }
  drawTrendlines(ctx, plot, priceRange, theme);
  drawCrosshair(ctx, plot, visible, priceRange, theme, oscillatorPlot || plot, oscillatorPlot);
  renderIndicatorLegend();
  updateTrendlineLegendPlacement();
}

function getChartTheme() {
  const styles = getComputedStyle(document.body);
  return {
    background: styles.getPropertyValue("--chart-bg").trim() || "#ffffff",
    grid: styles.getPropertyValue("--chart-grid").trim() || "#edf2f5",
    axis: styles.getPropertyValue("--chart-axis").trim() || "#d9e0e6",
    label: styles.getPropertyValue("--chart-label").trim() || "#65717f",
    crosshair: styles.getPropertyValue("--chart-crosshair").trim() || "rgba(28, 38, 48, 0.35)",
    up: styles.getPropertyValue("--success").trim() || "#23745a",
    down: styles.getPropertyValue("--danger").trim() || "#b54747",
    danger: styles.getPropertyValue("--danger").trim() || "#b54747",
  };
}

function drawGrid(ctx, plot, theme) {
  ctx.strokeStyle = theme.grid;
  ctx.lineWidth = 1;

  for (let i = 0; i <= 4; i += 1) {
    const y = plot.top + (plot.height * i) / 4;
    drawLine(ctx, plot.left, y, plot.right, y);
  }

  for (let i = 0; i <= 5; i += 1) {
    const x = plot.left + (plot.width * i) / 5;
    drawLine(ctx, x, plot.top, x, plot.bottom);
  }

  ctx.strokeStyle = theme.axis;
  drawLine(ctx, plot.left, plot.bottom, plot.right, plot.bottom);
  drawLine(ctx, plot.left, plot.top, plot.left, plot.bottom);
  drawLine(ctx, plot.right, plot.top, plot.right, plot.bottom);
}

function drawCandleSeries(ctx, plot, visible, priceRange, theme) {
  const slot = plot.width / chartState.visibleCount;
  const candleWidth = Math.max(2, Math.min(12, slot * 0.62));

  visible.forEach((candle, visibleIndex) => {
    const x = plot.left + slot * (visibleIndex + 0.5);
    const openY = priceToY(candle.open, plot, priceRange);
    const closeY = priceToY(candle.close, plot, priceRange);
    const highY = priceToY(candle.high, plot, priceRange);
    const lowY = priceToY(candle.low, plot, priceRange);
    const isUp = candle.close >= candle.open;
    const color = isUp ? theme.up : theme.down;

    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    drawLine(ctx, x, highY, x, lowY);

    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1, Math.abs(closeY - openY));
    ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
  });
}

function drawValueAxes(ctx, plot, visible, range, theme) {
  const basePrice = visible[0]?.open || 0;
  ctx.fillStyle = theme.label;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";

  for (let i = 0; i <= 4; i += 1) {
    const price = range.max - (range.max - range.min) * (i / 4);
    const y = plot.top + (plot.height * i) / 4;
    ctx.textAlign = "right";
    ctx.fillText(formatPrice(price), plot.left - 10, y);

    ctx.textAlign = "left";
    const percent = basePrice ? (price / basePrice - 1) * 100 : 0;
    ctx.fillText(formatPercent(percent), plot.right + 10, y);
  }
}

function drawPriceAxis(ctx, plot, range) {
  ctx.fillStyle = "#65717f";
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  for (let i = 0; i <= 4; i += 1) {
    const price = range.max - (range.max - range.min) * (i / 4);
    const y = plot.top + (plot.height * i) / 4;
    ctx.fillText(formatPrice(price), plot.right + 10, y);
  }
}

function drawTimeAxis(ctx, plot, visible, theme) {
  ctx.fillStyle = theme.label;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";

  if (!visible.length) {
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const ticks = getTimeTicks(visible);
  for (const tick of ticks) {
    const x = plot.left + slot * (tick.index + 0.5);
    ctx.fillText(tick.label, x, plot.bottom + 10);
  }
}

function drawCrosshair(ctx, plot, visible, priceRange, theme, timeAxisPlot = plot, oscillatorPlot = null) {
  if (chartState.hoverX === null || chartState.hoverY === null) {
    return;
  }
  if (chartState.hoverY < plot.top || chartState.hoverY > timeAxisPlot.bottom) {
    return;
  }

  const x = clamp(chartState.hoverX, plot.left, plot.right);
  const inPricePlot = chartState.hoverY <= plot.bottom;
  const activePlot = inPricePlot ? plot : oscillatorPlot;
  if (!activePlot) return;
  const y = clamp(chartState.hoverY, activePlot.top, activePlot.bottom);
  const slot = plot.width / chartState.visibleCount;
  const visibleIndex = clamp(
    Math.floor((x - plot.left) / slot),
    0,
    Math.max(0, visible.length - 1),
  );
  const candle = visible[visibleIndex];

  ctx.strokeStyle = theme.crosshair;
  ctx.setLineDash([4, 4]);
  drawLine(ctx, x, plot.top, x, timeAxisPlot.bottom);
  drawLine(ctx, activePlot.left, y, activePlot.right, y);
  ctx.setLineDash([]);

  if (inPricePlot) {
    const price = yToPrice(y, plot, priceRange);
    if (Number.isFinite(price)) {
      drawCrosshairAxisLabels(ctx, plot, visible, theme, x, y, price, candle, timeAxisPlot.bottom);
    }
  } else {
    drawCrosshairDateLabel(ctx, plot, theme, x, candle, timeAxisPlot.bottom);
  }
}

function drawCrosshairDateLabel(ctx, plot, theme, x, candle, timeAxisBottom) {
  const dateText = crosshairDateText(candle);
  if (!dateText) return;
  const labelHeight = 20;
  ctx.save();
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  const dateWidth = ctx.measureText(dateText).width + 14;
  const dateX = clamp(x - dateWidth / 2, plot.left, plot.right - dateWidth);
  const dateY = timeAxisBottom + 6;
  ctx.fillStyle = theme.background;
  ctx.strokeStyle = theme.crosshair;
  ctx.fillRect(dateX, dateY, dateWidth, labelHeight);
  ctx.strokeRect(dateX, dateY, dateWidth, labelHeight);
  ctx.fillStyle = theme.label;
  ctx.textAlign = "center";
  ctx.fillText(dateText, dateX + dateWidth / 2, dateY + labelHeight / 2);
  ctx.restore();
}

function drawCrosshairAxisLabels(ctx, plot, visible, theme, x, y, price, candle, timeAxisBottom) {
  const basePrice = visible[0]?.open || 0;
  const percent = basePrice ? (price / basePrice - 1) * 100 : 0;
  const priceText = formatPrice(price);
  const percentText = formatPercent(percent);
  const dateText = crosshairDateText(candle);
  const labelHeight = 20;

  ctx.save();
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.lineWidth = 1;

  const priceWidth = ctx.measureText(priceText).width + 12;
  const percentWidth = ctx.measureText(percentText).width + 12;
  const labelY = clamp(y - labelHeight / 2, plot.top, plot.bottom - labelHeight);

  ctx.fillStyle = theme.background;
  ctx.strokeStyle = theme.crosshair;
  ctx.fillRect(plot.left - priceWidth - 6, labelY, priceWidth, labelHeight);
  ctx.strokeRect(plot.left - priceWidth - 6, labelY, priceWidth, labelHeight);
  ctx.fillRect(plot.right + 6, labelY, percentWidth, labelHeight);
  ctx.strokeRect(plot.right + 6, labelY, percentWidth, labelHeight);

  ctx.fillStyle = theme.label;
  ctx.textAlign = "right";
  ctx.fillText(priceText, plot.left - 12, labelY + labelHeight / 2);
  ctx.textAlign = "left";
  ctx.fillText(percentText, plot.right + 12, labelY + labelHeight / 2);

  if (dateText) {
    const dateWidth = ctx.measureText(dateText).width + 14;
    const dateX = clamp(
      x - dateWidth / 2,
      plot.left,
      plot.right - dateWidth,
    );
    const dateY = timeAxisBottom + 6;
    ctx.fillStyle = theme.background;
    ctx.strokeStyle = theme.crosshair;
    ctx.fillRect(dateX, dateY, dateWidth, labelHeight);
    ctx.strokeRect(dateX, dateY, dateWidth, labelHeight);
    ctx.fillStyle = theme.label;
    ctx.textAlign = "center";
    ctx.fillText(
      dateText,
      dateX + dateWidth / 2,
      dateY + labelHeight / 2,
    );
  }
  ctx.restore();
}

function crosshairDateText(candle) {
  if (!candle?.date) {
    return "";
  }
  if (candle.endDate && candle.endDate !== candle.date) {
    return `${candle.date} 至 ${candle.endDate}`;
  }
  return candle.date;
}

function drawIndicators(ctx, plot, priceRange) {
  const slot = plot.width / chartState.visibleCount;
  for (const series of chartState.indicatorSeries) {
    if (!series.visible || isOscillatorIndicator(series)) {
      continue;
    }

    ctx.strokeStyle = series.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let drawing = false;

    for (let dataIndex = chartState.firstVisible; dataIndex < chartState.firstVisible + chartState.visibleCount; dataIndex += 1) {
      const value = series.values[dataIndex];
      const visibleIndex = dataIndex - chartState.firstVisible;
      if (value === null || value === undefined || dataIndex >= chartState.candles.length) {
        drawing = false;
        continue;
      }

      const x = plot.left + slot * (visibleIndex + 0.5);
      const y = priceToY(value, plot, priceRange);
      if (!drawing) {
        ctx.moveTo(x, y);
        drawing = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }
}

function isOscillatorIndicator(series) {
  return ["ATR", "VOLAT", "RATR", "LINEAR_FIT", "WTME", "RAPID_DROP", "RSI", "MACD"].includes(series.indicator_type);
}

function getVisibleOscillatorSeries() {
  return chartState.indicatorSeries.filter((series) => (
    series.visible && isOscillatorIndicator(series)
  ));
}

function drawOscillatorIndicators(ctx, plot, seriesList, theme) {
  const slot = plot.width / chartState.visibleCount;
  const ranges = {
    ATR: getIndicatorRange(seriesList, "ATR"),
    VOLAT: getIndicatorRange(seriesList, "VOLAT"),
    RATR: getIndicatorRange(seriesList, "RATR", true),
    LINEAR_FIT: seriesList.some((series) => series.indicator_type === "LINEAR_FIT") ? { min: 0, max: 1 } : null,
    WTME: getIndicatorRange(seriesList, "WTME", true),
    RAPID_DROP: getIndicatorRange(seriesList, "RAPID_DROP", true),
    RSI: seriesList.some((series) => series.indicator_type === "RSI") ? { min: 0, max: 100 } : null,
    MACD: getIndicatorRange(seriesList, "MACD", true),
  };

  for (const series of seriesList) {
    const range = ranges[series.indicator_type];
    if (!range) continue;
    if (series.indicator_type === "MACD") {
      drawMacdSeries(ctx, plot, series, range, theme, slot);
      continue;
    }
    ctx.strokeStyle = series.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let drawing = false;
    for (let dataIndex = chartState.firstVisible; dataIndex < chartState.firstVisible + chartState.visibleCount; dataIndex += 1) {
      const value = series.values[dataIndex];
      if (!Number.isFinite(value) || dataIndex >= chartState.candles.length) {
        drawing = false;
        continue;
      }
      const visibleIndex = dataIndex - chartState.firstVisible;
      const x = plot.left + slot * (visibleIndex + 0.5);
      const y = priceToY(value, plot, range);
      if (!drawing) {
        ctx.moveTo(x, y);
        drawing = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }

  for (const indicatorType of ["RATR", "WTME", "MACD"]) {
    const range = ranges[indicatorType];
    if (range && range.min < 0 && range.max > 0) {
      ctx.save();
      ctx.strokeStyle = theme.axis;
      ctx.setLineDash([3, 3]);
      const zeroY = priceToY(0, plot, range);
      drawLine(ctx, plot.left, zeroY, plot.right, zeroY);
      ctx.restore();
    }
  }
  if (ranges.RSI) {
    ctx.save();
    ctx.strokeStyle = theme.axis;
    ctx.setLineDash([3, 3]);
    drawLine(ctx, plot.left, priceToY(70, plot, ranges.RSI), plot.right, priceToY(70, plot, ranges.RSI));
    drawLine(ctx, plot.left, priceToY(30, plot, ranges.RSI), plot.right, priceToY(30, plot, ranges.RSI));
    ctx.restore();
  }
  drawOscillatorAxes(ctx, plot, ranges, theme);
}

function drawMacdSeries(ctx, plot, series, range, theme, slot) {
  const histogram = series.componentValues?.histogram || series.values;
  const zeroY = priceToY(0, plot, range);
  for (let dataIndex = chartState.firstVisible; dataIndex < chartState.firstVisible + chartState.visibleCount; dataIndex += 1) {
    const value = histogram[dataIndex];
    if (!Number.isFinite(value) || dataIndex >= chartState.candles.length) continue;
    const visibleIndex = dataIndex - chartState.firstVisible;
    const x = plot.left + slot * (visibleIndex + 0.5);
    const y = priceToY(value, plot, range);
    ctx.fillStyle = value >= 0 ? theme.up : theme.down;
    ctx.globalAlpha = 0.48;
    ctx.fillRect(x - Math.max(1, slot * 0.28), Math.min(y, zeroY), Math.max(2, slot * 0.56), Math.max(1, Math.abs(zeroY - y)));
  }
  ctx.globalAlpha = 1;
  const drawComponent = (values, color) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let drawing = false;
    for (let dataIndex = chartState.firstVisible; dataIndex < chartState.firstVisible + chartState.visibleCount; dataIndex += 1) {
      const value = values?.[dataIndex];
      if (!Number.isFinite(value) || dataIndex >= chartState.candles.length) { drawing = false; continue; }
      const x = plot.left + slot * (dataIndex - chartState.firstVisible + 0.5);
      const y = priceToY(value, plot, range);
      if (!drawing) { ctx.moveTo(x, y); drawing = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  };
  drawComponent(series.componentValues?.line, series.color);
  drawComponent(series.componentValues?.signal, theme.danger);
}

function getIndicatorRange(seriesList, indicatorType, includeZero = false) {
  const values = [];
  for (const series of seriesList) {
    if (series.indicator_type !== indicatorType) continue;
    for (let index = chartState.firstVisible; index < chartState.firstVisible + chartState.visibleCount; index += 1) {
      if (indicatorType === "MACD") {
        for (const component of Object.values(series.componentValues || {})) {
          if (Number.isFinite(component[index])) values.push(component[index]);
        }
      } else if (Number.isFinite(series.values[index])) values.push(series.values[index]);
    }
  }
  if (!values.length) return null;
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (includeZero) {
    min = Math.min(min, 0);
    max = Math.max(max, 0);
  }
  if (min === max) {
    const padding = Math.max(Math.abs(min) * 0.1, 1);
    min -= padding;
    max += padding;
  } else {
    const padding = (max - min) * 0.08;
    min -= padding;
    max += padding;
  }
  return { min, max };
}

function drawOscillatorAxes(ctx, plot, ranges, theme) {
  ctx.fillStyle = theme.label;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 2; index += 1) {
    const y = plot.top + (plot.height * index) / 2;
    if (ranges.ATR) {
      const value = ranges.ATR.max - (ranges.ATR.max - ranges.ATR.min) * (index / 2);
      ctx.textAlign = "right";
      ctx.fillText(formatPrice(value), plot.left - 10, y);
    }
    if (ranges.RATR) {
      const value = ranges.RATR.max - (ranges.RATR.max - ranges.RATR.min) * (index / 2);
      ctx.textAlign = "left";
      ctx.fillText(value.toFixed(2), plot.right + 10, y);
    }
    if (ranges.VOLAT) {
      const value = ranges.VOLAT.max - (ranges.VOLAT.max - ranges.VOLAT.min) * (index / 2);
      ctx.textAlign = "right";
      ctx.fillText(`${value.toFixed(2)}%`, plot.right - 8, y);
    }
    if (ranges.LINEAR_FIT) {
      const value = 1 - index / 2;
      ctx.textAlign = "left";
      ctx.fillText(value.toFixed(1), plot.left + 8, y);
    }
    if (ranges.WTME) {
      const value = ranges.WTME.max - (ranges.WTME.max - ranges.WTME.min) * (index / 2);
      ctx.textAlign = "right";
      ctx.fillText(value.toFixed(2), plot.right - 8, y);
    }
    if (ranges.RAPID_DROP) {
      const value = ranges.RAPID_DROP.max - (ranges.RAPID_DROP.max - ranges.RAPID_DROP.min) * (index / 2);
      ctx.textAlign = "left";
      ctx.fillText(value.toFixed(1), plot.left + 8, y);
    }
    if (ranges.RSI) {
      ctx.textAlign = "left";
      ctx.fillText((100 - index * 50).toFixed(0), plot.left + 8, y);
    }
  }
  ctx.textBaseline = "top";
  if (ranges.ATR) {
    ctx.textAlign = "left";
    ctx.fillText("ATR", plot.left + 6, plot.top + 4);
  }
  if (ranges.RATR) {
    ctx.textAlign = "right";
    ctx.fillText("相对 ATR", plot.right - 6, plot.top + 4);
  }
  if (ranges.VOLAT) {
    ctx.textAlign = "right";
    ctx.fillText("VOLAT", plot.right - 6, plot.top + (ranges.RATR ? 18 : 4));
  }
  if (ranges.LINEAR_FIT) {
    ctx.textAlign = "left";
    ctx.fillText("R²", plot.left + 6, plot.top + (ranges.RSI ? 18 : 4));
  }
  if (ranges.WTME) {
    ctx.textAlign = "right";
    ctx.fillText("WTME", plot.right - 6, plot.top + (ranges.RATR ? 18 : 4));
  }
  if (ranges.RAPID_DROP) {
    ctx.textAlign = "left";
    ctx.fillText("急跌 0/1", plot.left + 6, plot.top + (ranges.ATR ? 18 : 4));
  }
  if (ranges.RSI) {
    ctx.textAlign = "left";
    ctx.fillText("RSI", plot.left + 6, plot.top + 4);
  }
  if (ranges.MACD) {
    ctx.textAlign = "right";
    ctx.fillText("MACD", plot.right - 6, plot.top + 4);
  }
}

function drawTrendlines(ctx, plot, priceRange, theme) {
  if (!chartState.trendlines.length) {
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const visibleStart = chartState.firstVisible;
  const visibleEnd = chartState.firstVisible + chartState.visibleCount - 1;

  for (const line of chartState.trendlines) {
    if (line.visible === false) {
      continue;
    }
    const startIndex = Number(line.start_index);
    const formationEndIndex = Number(line.formation_end_index ?? line.start_index);
    const endIndex = Number(line.end_index);
    const projectionEndIndex = Number(line.projection_end_index ?? line.end_index);
    if (
      !Number.isFinite(startIndex)
      || !Number.isFinite(formationEndIndex)
      || !Number.isFinite(endIndex)
      || !Number.isFinite(projectionEndIndex)
    ) {
      continue;
    }
    if (projectionEndIndex < visibleStart || startIndex > visibleEnd) {
      continue;
    }

    const color = getTrendlineColor(line, theme);
    const width = getTrendlineWidth(line.tier)
      + (line.id === chartState.hoverTrendlineId ? 1 : 0);
    const dash = getTrendlineDash(line);

    drawTrendlineSegment(
      ctx,
      plot,
      priceRange,
      slot,
      line,
      startIndex,
      Math.min(formationEndIndex, endIndex, projectionEndIndex),
      color,
      Math.max(1.2, width - 0.5),
      dash,
      0.78,
    );

    if (endIndex > formationEndIndex) {
      drawTrendlineSegment(
        ctx,
        plot,
        priceRange,
        slot,
        line,
        formationEndIndex,
        Math.min(endIndex, projectionEndIndex),
        color,
        width,
        dash,
        0.96,
      );
    }

    if (projectionEndIndex > endIndex) {
      drawTrendlineSegment(
        ctx,
        plot,
        priceRange,
        slot,
        line,
        endIndex,
        projectionEndIndex,
        color,
        Math.max(1.4, width - 0.7),
        dash,
        0.76,
      );
    }
  }
  for (const line of chartState.trendlines) {
    drawTrendlineTouches(ctx, plot, priceRange, slot, line, theme);
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
}

function drawKeyZones(ctx, plot, priceRange, theme) {
  if (!chartState.keyZones.length) {
    return;
  }
  const slot = plot.width / chartState.visibleCount;
  const visibleStart = chartState.firstVisible;
  const visibleEnd = chartState.firstVisible + chartState.visibleCount - 1;
  for (const zone of chartState.keyZones) {
    if (zone.visible === false) {
      continue;
    }
    const { start: startIndex, end: endIndex } = getKeyZoneDisplayBounds(zone);
    const low = Number(zone.zone_low);
    const high = Number(zone.zone_high);
    const center = Number(zone.center);
    if (![startIndex, endIndex, low, high, center].every(Number.isFinite)) {
      continue;
    }
    const currentIndex = chartState.candles.length - 1;
    const projectsToCurrent = (
      zone.project_center_to_current === true
      && endIndex < currentIndex
    );
    const evidenceVisible = endIndex >= visibleStart && startIndex <= visibleEnd;
    const projectionVisible = (
      projectsToCurrent
      && currentIndex >= visibleStart
      && endIndex < visibleEnd
    );
    if (!evidenceVisible && !projectionVisible) {
      continue;
    }
    const colors = getKeyZoneColors(zone, theme);
    if (evidenceVisible) {
      const from = clamp(startIndex, visibleStart, visibleEnd);
      const to = clamp(endIndex, visibleStart, visibleEnd);
      const left = indexToX(from, plot, slot) - slot / 2;
      const right = indexToX(to, plot, slot) + slot / 2;
      const top = priceToY(high, plot, priceRange);
      const bottom = priceToY(low, plot, priceRange);
      ctx.save();
      ctx.fillStyle = colors.fill;
      ctx.fillRect(left, top, Math.max(1, right - left), Math.max(1, bottom - top));
      ctx.strokeStyle = colors.stroke;
      ctx.lineWidth = zone.id === chartState.hoverKeyZoneId ? 2.2 : 1.2;
      ctx.setLineDash([]);
      ctx.strokeRect(left, top, Math.max(1, right - left), Math.max(1, bottom - top));
      ctx.setLineDash([5, 4]);
      ctx.globalAlpha = 0.86;
      drawLine(ctx, left, priceToY(center, plot, priceRange), right, priceToY(center, plot, priceRange));
      ctx.restore();
    }
    if (projectionVisible) {
      const projectionStart = Math.max(endIndex, visibleStart);
      const projectionEnd = Math.min(currentIndex, visibleEnd);
      const left = endIndex >= visibleStart
        ? indexToX(projectionStart, plot, slot) + slot / 2
        : plot.left;
      const right = indexToX(projectionEnd, plot, slot) + slot / 2;
      ctx.save();
      ctx.strokeStyle = colors.stroke;
      ctx.lineWidth = zone.id === chartState.hoverKeyZoneId ? 2 : 1.3;
      ctx.setLineDash([5, 4]);
      ctx.globalAlpha = 0.78;
      drawLine(
        ctx,
        left,
        priceToY(center, plot, priceRange),
        right,
        priceToY(center, plot, priceRange),
      );
      ctx.restore();
    }
  }
}

function getKeyZoneColors(zone, theme) {
  if (zone.status === "challenging" || zone.status === "retesting") {
    return { fill: "rgba(245, 158, 11, 0.15)", stroke: "rgba(245, 158, 11, 0.82)" };
  }
  if (zone.current_role === "support") {
    return { fill: "rgba(35, 116, 90, 0.12)", stroke: theme.up || "#23745a" };
  }
  return { fill: "rgba(181, 71, 71, 0.12)", stroke: theme.danger || "#b54747" };
}

function drawTrendlineTouches(ctx, plot, priceRange, slot, line, theme) {
  if (line.visible === false || !Array.isArray(line.touch_indices)) {
    return;
  }
  const projectionEnd = Number(
    line.projection_end_index ?? line.end_index,
  );
  const expectedCount = Math.max(0, Number(line.touches) || 0);
  const touchIndices = [...new Set(
    line.touch_indices
      .map(Number)
      .filter(Number.isFinite),
  )].slice(0, expectedCount);
  const color = getTrendlineColor(line, theme);
  const radius = line.id === chartState.hoverTrendlineId ? 4 : 3.2;

  ctx.save();
  ctx.fillStyle = theme.background;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.setLineDash([]);
  for (const index of touchIndices) {
    if (
      index < chartState.firstVisible
      || index >= chartState.firstVisible + chartState.visibleCount
      || index > projectionEnd
    ) {
      continue;
    }
    const price = trendlinePriceAt(line, index);
    if (!Number.isFinite(price)) {
      continue;
    }
    ctx.beginPath();
    ctx.arc(
      indexToX(index, plot, slot),
      priceToY(price, plot, priceRange),
      radius,
      0,
      Math.PI * 2,
    );
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
}

function drawTrendlineSegment(ctx, plot, priceRange, slot, line, fromIndex, toIndex, color, width, dash, alpha) {
  const clippedStart = clamp(fromIndex, chartState.firstVisible, chartState.firstVisible + chartState.visibleCount - 1);
  const clippedEnd = clamp(toIndex, chartState.firstVisible, chartState.firstVisible + chartState.visibleCount - 1);
  if (clippedEnd < clippedStart) {
    return;
  }

  const startY = trendlinePriceAt(line, clippedStart);
  const endY = trendlinePriceAt(line, clippedEnd);
  if (!Number.isFinite(startY) || !Number.isFinite(endY)) {
    return;
  }

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.globalAlpha = alpha;
  ctx.setLineDash(dash);
  ctx.beginPath();
  ctx.moveTo(indexToX(clippedStart, plot, slot), priceToY(startY, plot, priceRange));
  ctx.lineTo(indexToX(clippedEnd, plot, slot), priceToY(endY, plot, priceRange));
  ctx.stroke();
  ctx.restore();
}

function trendlinePriceAt(line, index) {
  const startIndex = Number(line.start_index);
  const startPrice = Number(line.start_price);
  const slope = Number(line.slope);
  return startPrice + slope * (index - startIndex);
}

function indexToX(index, plot, slot) {
  return plot.left + slot * (index - chartState.firstVisible + 0.5);
}

function getTrendlineColor(line, theme) {
  if (line.color) {
    return line.color;
  }
  if (line.tier === "short") {
    return line.direction === "up" ? "#8b5cf6" : "#d946ef";
  }
  if (line.tier === "medium") {
    return line.direction === "up" ? "#06b6d4" : "#f97316";
  }
  return line.direction === "up" ? "#2563eb" : theme.danger || "#dc2626";
}

function getTrendlineWidth(tier) {
  if (tier === "long") {
    return 2.8;
  }
  if (tier === "short") {
    return 2.5;
  }
  return 2;
}

function getTrendlineDash(line) {
  return Number(line.tier_score || 0) >= 75 ? [] : [8, 5];
}

function updateTrendlineLegendPlacement() {
  const legend = document.getElementById("trendline-legend");
  if (!legend || legend.hidden || !chartState.candles.length) {
    return;
  }

  const plot = getPlotArea();
  const visible = getVisibleCandles();
  if (!visible.length) {
    return;
  }

  const priceRange = getPriceRange(visible);
  const scores = {
    "top-left": 0,
    "top-right": 0,
    "bottom-left": 0,
    "bottom-right": 0,
  };

  visible.forEach((candle, visibleIndex) => {
    const horizontal = visibleIndex < visible.length / 2 ? "left" : "right";
    const highY = priceToY(candle.high, plot, priceRange);
    const lowY = priceToY(candle.low, plot, priceRange);
    const midY = (highY + lowY) / 2;
    const vertical = midY < plot.top + plot.height / 2 ? "top" : "bottom";
    const spanWeight = 1 + Math.min(3, Math.abs(lowY - highY) / 22);
    scores[`${vertical}-${horizontal}`] += spanWeight;
  });

  const placement = Object.entries(scores).sort((left, right) => left[1] - right[1])[0][0];
  legend.classList.remove(
    "trendline-legend-top-left",
    "trendline-legend-top-right",
    "trendline-legend-bottom-left",
    "trendline-legend-bottom-right",
  );
  legend.classList.add(`trendline-legend-${placement}`);
}

function drawEmptyState(ctx, width, height) {
  ctx.fillStyle = getChartTheme().label;
  ctx.font = "14px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("暂无可展示的行情数据", width / 2, height / 2);
}

function updateHover(offsetX, offsetY) {
  const fullPlot = getPlotArea();
  const oscillatorSeries = getVisibleOscillatorSeries();
  const oscillatorHeight = oscillatorSeries.length
    ? Math.max(100, Math.min(150, fullPlot.height * 0.28)) : 0;
  const oscillatorGap = oscillatorSeries.length ? 16 : 0;
  const plot = oscillatorSeries.length ? {
    ...fullPlot,
    bottom: fullPlot.bottom - oscillatorHeight - oscillatorGap,
    height: fullPlot.height - oscillatorHeight - oscillatorGap,
  } : fullPlot;
  const oscillatorPlot = oscillatorSeries.length ? {
    ...fullPlot,
    top: plot.bottom + oscillatorGap,
    height: oscillatorHeight,
  } : null;
  if (
    offsetX < plot.left
    || offsetX > plot.right
    || offsetY < plot.top
    || offsetY > (oscillatorPlot?.bottom || plot.bottom)
  ) {
    chartState.hoverIndex = null;
    chartState.hoverX = null;
    chartState.hoverY = null;
    chartState.hoverTrendlineId = null;
    chartState.hoverKeyZoneId = null;
    hideTooltip();
    drawChart();
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const visibleIndex = clamp(Math.floor((offsetX - plot.left) / slot), 0, chartState.visibleCount - 1);
  const dataIndex = chartState.firstVisible + visibleIndex;
  if (dataIndex < 0 || dataIndex >= chartState.candles.length) {
    chartState.hoverIndex = null;
    chartState.hoverX = null;
    chartState.hoverY = null;
    chartState.hoverTrendlineId = null;
    chartState.hoverKeyZoneId = null;
    hideTooltip();
    drawChart();
    return;
  }

  chartState.hoverIndex = dataIndex;
  chartState.hoverX = offsetX;
  chartState.hoverY = offsetY;
  const visible = getVisibleCandles();
  const priceRange = getPriceRange(visible);
  const inOscillator = Boolean(oscillatorPlot && offsetY >= oscillatorPlot.top);
  const trendlineHit = inOscillator ? null : findTrendlineHit(offsetX, offsetY, plot, priceRange);
  const keyZoneHit = inOscillator || trendlineHit
    ? null
    : findKeyZoneHit(offsetX, offsetY, plot, priceRange);
  chartState.hoverTrendlineId = trendlineHit?.id || null;
  chartState.hoverKeyZoneId = keyZoneHit?.id || null;
  if (trendlineHit) {
    hideOhlcvTooltip();
    showTrendlineTooltip(trendlineHit, offsetX, offsetY);
    renderIndicatorLegend();
    drawChart();
    return;
  }

  if (keyZoneHit) {
    hideOhlcvTooltip();
    showKeyZoneTooltip(keyZoneHit, offsetX, offsetY);
    renderIndicatorLegend();
    drawChart();
    return;
  }

  hideTrendlineTooltip();
  if (inOscillator) {
    showIndicatorTooltip(dataIndex, offsetX, offsetY);
    renderIndicatorLegend();
    drawChart();
    return;
  }
  const candleCenterX = plot.left + slot * (visibleIndex + 0.5);
  const candle = chartState.candles[dataIndex];
  if (isCandleHit(candle, candleCenterX, offsetX, offsetY, slot, plot, priceRange)) {
    showTooltip(chartState.candles[dataIndex], offsetX, offsetY);
  } else {
    hideOhlcvTooltip();
  }
  renderIndicatorLegend();
  drawChart();
}

function showIndicatorTooltip(dataIndex, offsetX, offsetY) {
  const candle = chartState.candles[dataIndex];
  const dateText = crosshairDateText(candle);
  const rows = getVisibleOscillatorSeries().map((series) => {
    if (series.indicator_type === "MACD") {
      const components = series.componentValues || {};
      return `<span>${escapeHtml(series.name)}</span><b>DIF ${formatIndicatorValue(components.line?.[dataIndex])} · DEA ${formatIndicatorValue(components.signal?.[dataIndex])} · 柱 ${formatIndicatorValue(components.histogram?.[dataIndex])}</b>`;
    }
    return `<span>${escapeHtml(series.name)}</span><b>${formatIndicatorValue(series.values[dataIndex])}</b>`;
  }).join("");
  chartState.tooltip.innerHTML = `
    <strong>${escapeHtml(periodLabels[chartState.period])} · ${escapeHtml(dateText)}</strong>
    <div class="indicator-tooltip-grid">${rows}</div>
  `;
  positionChartTooltip(chartState.tooltip, offsetX, offsetY);
}

function formatIndicatorValue(value) {
  return Number.isFinite(value) ? Number(value).toFixed(4).replace(/0+$/, "").replace(/\.$/, "") : "—";
}

function showTooltip(candle, offsetX, offsetY) {
  const periodText = periodLabels[chartState.period];
  const dateText = candle.endDate && candle.endDate !== candle.date
    ? `${candle.date} 至 ${candle.endDate}`
    : candle.date;
  chartState.tooltip.innerHTML = `
    <strong>${periodText} · ${dateText}</strong>
    <div class="ohlcv-grid">
      <span>开盘 ${formatPrice(candle.open)}</span>
      <span>最高 ${formatPrice(candle.high)}</span>
      <span>最低 ${formatPrice(candle.low)}</span>
      <span>收盘 ${formatPrice(candle.close)}</span>
      <span>成交量</span>
      <span>${formatVolume(candle.volume)}</span>
    </div>
  `;

  const containerRect = chartState.container.getBoundingClientRect();
  const tooltipWidth = 230;
  const tooltipHeight = 118;
  const left = Math.min(offsetX + 16, containerRect.width - tooltipWidth - 10);
  const top = Math.min(offsetY + 16, containerRect.height - tooltipHeight - 10);
  chartState.tooltip.style.left = `${Math.max(10, left)}px`;
  chartState.tooltip.style.top = `${Math.max(10, top)}px`;
  chartState.tooltip.hidden = false;
}

function hideTooltip() {
  hideOhlcvTooltip();
  hideTrendlineTooltip();
}

function hideOhlcvTooltip() {
  chartState.tooltip.hidden = true;
}

function hideTrendlineTooltip() {
  if (chartState.trendlineTooltip) {
    chartState.trendlineTooltip.hidden = true;
  }
}

function isCandleHit(candle, centerX, pointerX, pointerY, slot, plot, priceRange) {
  const bodyWidth = Math.max(2, Math.min(12, slot * 0.62));
  const openY = priceToY(candle.open, plot, priceRange);
  const closeY = priceToY(candle.close, plot, priceRange);
  const highY = priceToY(candle.high, plot, priceRange);
  const lowY = priceToY(candle.low, plot, priceRange);
  const bodyTop = Math.min(openY, closeY);
  const bodyBottom = Math.max(openY, closeY, bodyTop + 1);
  const bodyHit = (
    Math.abs(pointerX - centerX) <= bodyWidth / 2 + 2
    && pointerY >= bodyTop - 2
    && pointerY <= bodyBottom + 2
  );
  const wickHit = (
    Math.abs(pointerX - centerX) <= Math.max(2, Math.min(4, slot * 0.16))
    && pointerY >= highY - 2
    && pointerY <= lowY + 2
  );
  return bodyHit || wickHit;
}

function findTrendlineHit(pointerX, pointerY, plot, priceRange) {
  const slot = plot.width / chartState.visibleCount;
  let best = null;
  let bestDistance = Infinity;
  for (const line of chartState.trendlines) {
    if (line.visible === false) {
      continue;
    }
    const fromIndex = Math.max(
      chartState.firstVisible,
      Number(line.start_index),
    );
    const toIndex = Math.min(
      chartState.firstVisible + chartState.visibleCount - 1,
      Number(line.projection_end_index ?? line.end_index),
    );
    if (!Number.isFinite(fromIndex) || !Number.isFinite(toIndex) || toIndex < fromIndex) {
      continue;
    }
    const x1 = indexToX(fromIndex, plot, slot);
    const y1 = priceToY(trendlinePriceAt(line, fromIndex), plot, priceRange);
    const x2 = indexToX(toIndex, plot, slot);
    const y2 = priceToY(trendlinePriceAt(line, toIndex), plot, priceRange);
    const distance = pointToSegmentDistance(
      pointerX,
      pointerY,
      x1,
      y1,
      x2,
      y2,
    );
    const tolerance = Math.max(5, getTrendlineWidth(line.tier) + 3);
    if (distance <= tolerance && distance < bestDistance) {
      best = line;
      bestDistance = distance;
    }
  }
  return best;
}

function findKeyZoneHit(pointerX, pointerY, plot, priceRange) {
  const slot = plot.width / chartState.visibleCount;
  let best = null;
  let bestDistance = Infinity;
  for (const zone of chartState.keyZones) {
    if (zone.visible === false) {
      continue;
    }
    const bounds = getKeyZoneDisplayBounds(zone);
    const start = Math.max(chartState.firstVisible, bounds.start);
    const end = Math.min(
      chartState.firstVisible + chartState.visibleCount - 1,
      bounds.end,
    );
    const low = Number(zone.zone_low);
    const high = Number(zone.zone_high);
    if (![start, end, low, high].every(Number.isFinite) || end < start) {
      continue;
    }
    const left = indexToX(start, plot, slot) - slot / 2;
    const right = indexToX(end, plot, slot) + slot / 2;
    const top = priceToY(high, plot, priceRange);
    const bottom = priceToY(low, plot, priceRange);
    if (pointerX < left || pointerX > right || pointerY < top - 3 || pointerY > bottom + 3) {
      continue;
    }
    const centerDistance = Math.abs(pointerY - priceToY(Number(zone.center), plot, priceRange));
    if (centerDistance < bestDistance) {
      best = zone;
      bestDistance = centerDistance;
    }
  }
  return best;
}

function pointToSegmentDistance(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lengthSquared = dx * dx + dy * dy;
  if (!lengthSquared) {
    return Math.hypot(px - x1, py - y1);
  }
  const ratio = clamp(
    ((px - x1) * dx + (py - y1) * dy) / lengthSquared,
    0,
    1,
  );
  return Math.hypot(
    px - (x1 + ratio * dx),
    py - (y1 + ratio * dy),
  );
}

function showTrendlineTooltip(line, offsetX, offsetY) {
  if (!chartState.trendlineTooltip) {
    return;
  }
  const tier = { long: "长期", medium: "中期", short: "短期" }[line.tier] || line.tier;
  const direction = line.direction === "up" ? "上涨" : "下跌";
  const status = {
    trending: "趋势中",
    challenging: "挑战中",
    broken: "已结束",
  }[line.status] || line.status;
  const role = line.family_role === "stage" ? "阶段变化线" : "主趋势线";
  chartState.trendlineTooltip.innerHTML = `
    <strong>${escapeHtml(`${tier}${direction} · ${status}`)}</strong>
    <div class="trendline-detail-meta">
      <span>评分 / 点位</span><b>${Number(line.tier_score || 0).toFixed(1)} / ${formatPrice(line.projection_end_price)}</b>
      <span>结构 / 触点</span><b>${escapeHtml(role)} / ${Number(line.touches || 0)}</b>
      <span>形成 / 最近触点</span><b>${escapeHtml(line.formation_date || "-")} / ${escapeHtml(line.last_touch_date || "-")}</b>
      <span>距趋势线</span><b>${Number(line.current_close_gap || 0).toFixed(2)} ATR</b>
      <span>方向显著性</span><b>${Number(line.drift_t || 0).toFixed(2)} t</b>
    </div>
    <div class="trendline-score-grid">
      ${trendlineScoreRows(line)}
    </div>
  `;
  positionChartTooltip(chartState.trendlineTooltip, offsetX, offsetY);
  chartState.trendlineTooltip.hidden = false;
}

function showKeyZoneTooltip(zone, offsetX, offsetY) {
  if (!chartState.trendlineTooltip) {
    return;
  }
  const role = zone.current_role === "support" ? "支撑区" : "压力区";
  const status = {
    active: "有效",
    challenging: "测试中",
    retesting: "突破后待回测",
  }[zone.status] || zone.status;
  const components = zone.score_components || {};
  const labels = {
    tests: "测试证据",
    prominence: "拐点显著度",
    rejection: "拒绝质量",
    span: "时间分布",
    recency: "新近性",
    integrity: "区域完整性",
  };
  const weights = { tests: 25, prominence: 15, rejection: 20, span: 15, recency: 10, integrity: 15 };
  const scoreRows = Object.entries(labels).map(([key, label]) => {
    const quality = Number(components[key] || 0);
    return `<span>${escapeHtml(label)} · ${weights[key]}%</span><b>${(quality * 100).toFixed(1)}% / ${(quality * weights[key]).toFixed(1)}</b>`;
  }).join("");
  const integrityMultiplier = Number(components.integrity_multiplier ?? 1);
  const integrityMultiplierRow = `
    <span>完整性总分折减</span><b>×${integrityMultiplier.toFixed(3)}</b>
  `;
  chartState.trendlineTooltip.innerHTML = `
    <strong>${escapeHtml(`${role} · ${status}`)}</strong>
    <div class="trendline-detail-meta">
      <span>评分 / 中心</span><b>${Number(zone.score || 0).toFixed(1)} / ${formatPrice(zone.center)}</b>
      <span>区域范围</span><b>${formatPrice(zone.zone_low)}–${formatPrice(zone.zone_high)}</b>
      <span>结构 / 肩部 / 临时边界</span><b>${Number(zone.independent_tests || 0)} / ${Number(zone.shoulder_tests || 0)} / ${Number(zone.provisional_edge_tests || 0)}</b>
      <span>边界提前确认</span><b>${zone.provisional_edge_confirmation ? "临时，待第 3 根复核" : "无"}</b>
      <span>近期验证 / 互换</span><b>${Number(zone.validation_tests || 0)} / ${zone.role_reversal_confirmed ? "已确认" : "无"}</b>
      <span>形成 / 最近结构测试</span><b>${escapeHtml(zone.formation_date || "-")} / ${escapeHtml(zone.latest_test_date || "-")}</b>
      <span>最近区域验证</span><b>${escapeHtml(zone.latest_validation_date || "-")}</b>
      <span>距现价</span><b>${Number(zone.distance_from_current_atr || 0).toFixed(2)} ATR</b>
    </div>
    <div class="trendline-score-grid">${scoreRows}${integrityMultiplierRow}</div>
  `;
  positionChartTooltip(chartState.trendlineTooltip, offsetX, offsetY);
  chartState.trendlineTooltip.hidden = false;
}

function trendlineScoreRows(line) {
  const scoreFormula = line.score_formula || line.tier;
  const weights = {
    long: [17, 18, 11, 22, 14, 10, 3, 5],
    medium: [17, 18, 12, 20, 12, 9, 6, 4],
    short: [16, 16, 14, 12, 4, 2, 14, 12],
  }[scoreFormula] || [17, 18, 11, 22, 14, 10, 3, 5];
  const metrics = [
    ["边界完整性", line.integrity],
    ["实体完整性", line.body_integrity],
    ["触点接近质量", line.proximity],
    ["触点证据", line.touch_score],
    ["拒绝质量", line.rejection],
    ["触点分布", line.touch_distribution],
    ["方向效率", line.efficiency],
    ["斜率强度", line.slope_strength],
  ];
  const rows = metrics.map(([label, value], index) => {
    const quality = Number(value || 0);
    const weight = weights[index];
    return `
      <span>${escapeHtml(label)} · ${weight}%</span>
      <b>${(quality * 100).toFixed(1)}% / ${(quality * weight).toFixed(1)}</b>
    `;
  });
  if (scoreFormula === "medium" || scoreFormula === "short") {
    const center = scoreFormula === "medium" ? 0.65 : 0.70;
    const steepness = scoreFormula === "medium" ? 1.35 : 1.40;
    const weight = scoreFormula === "medium" ? 2 : 8;
    const significance = 1 / (
      1 + Math.exp(-steepness * (Number(line.drift_t || 0) - center))
    );
    rows.push(`
      <span>方向显著分 · ${weight}%</span>
      <b>${(significance * 100).toFixed(1)}% / ${(significance * weight).toFixed(1)}</b>
    `);
  }
  if (scoreFormula === "short") {
    const eventSpan = Number(line.event_span || 0);
    rows.push(`
      <span>触点跨度 · 2%</span>
      <b>${(eventSpan * 100).toFixed(1)}% / ${(eventSpan * 2).toFixed(1)}</b>
    `);
  }
  rows.push(`
    <span>分布修正</span>
    <b>×${Number(line.distribution_penalty_factor || 0).toFixed(3)}</b>
  `);
  return rows.join("");
}

function positionChartTooltip(element, offsetX, offsetY) {
  element.hidden = false;
  const rect = element.getBoundingClientRect();
  const containerRect = chartState.container.getBoundingClientRect();
  const left = Math.min(offsetX + 16, containerRect.width - rect.width - 10);
  const top = Math.min(offsetY + 16, containerRect.height - rect.height - 10);
  element.style.left = `${Math.max(10, left)}px`;
  element.style.top = `${Math.max(10, top)}px`;
}

function recalculateIndicators() {
  chartState.indicatorSeries = chartState.indicators.map((indicator) => ({
    ...indicator,
    values: alignIndicatorPoints(chartState.candles, indicator.points || []),
    componentValues: alignIndicatorComponents(chartState.candles, indicator.points || []),
  }));
}

function indicatorBarKey(item) {
  return `${item?.date || ""}|${item?.endDate || ""}`;
}

function alignIndicatorPoints(candles, points) {
  const byBar = new Map(points.map((point) => [indicatorBarKey(point), point.value]));
  return candles.map((candle) => byBar.get(indicatorBarKey(candle)) ?? null);
}

function alignIndicatorComponents(candles, points) {
  const names = new Set(points.flatMap((point) => Object.keys(point.components || {})));
  const result = {};
  for (const name of names) {
    const byBar = new Map(points.map((point) => [indicatorBarKey(point), point.components?.[name]]));
    result[name] = candles.map((candle) => byBar.get(indicatorBarKey(candle)) ?? null);
  }
  return result;
}

function indicatorPriceBasisLabel(priceBasis) {
  return {
    all_adjusted: "前复权",
    all: "前复权",
    split: "仅拆股复权",
    raw: "原始价",
  }[priceBasis] || priceBasis || "";
}

function renderIndicatorLegend() {
  if (!chartState.legend) {
    return;
  }

  if (!chartState.indicatorSeries.length) {
    chartState.legend.innerHTML = "";
    return;
  }

  const valueIndex = getLegendValueIndex();
  chartState.legend.innerHTML = chartState.indicatorSeries.map((series) => {
    const value = series.values[valueIndex];
    const displayValue = series.indicator_type === "MACD"
      ? `DIF ${formatIndicatorValue(series.componentValues?.line?.[valueIndex])} · DEA ${formatIndicatorValue(series.componentValues?.signal?.[valueIndex])} · 柱 ${formatIndicatorValue(series.componentValues?.histogram?.[valueIndex])}`
      : series.indicator_type === "VOLAT"
        ? (value == null ? "-" : `${formatIndicatorValue(value)}%`)
      : series.indicator_type === "LINEAR_FIT"
        ? formatIndicatorValue(value)
      : value == null ? "-" : formatPrice(value);
    const visibilityClass = series.visible ? "" : " is-hidden";
    const favoriteClass = series.is_favorite ? " is-favorite" : "";
    const basis = indicatorPriceBasisLabel(series.price_basis);
    const state = series.is_provisional ? "盘中值" : "已收线";
    return `
      <div class="legend-row${visibilityClass}" data-symbol-indicator-id="${series.id}" data-indicator-id="${series.indicator_id}">
        <button class="legend-button${visibilityClass}" type="button" data-action="toggle-visible" title="${series.visible ? "隐藏" : "显示"}">
          ${eyeIcon(series.visible)}
        </button>
        <span class="legend-swatch" style="background:${escapeHtml(series.color)}"></span>
        <button class="legend-button${favoriteClass}" type="button" data-action="toggle-favorite" title="${series.is_favorite ? "取消收藏" : "收藏"}">
          ${starIcon(series.is_favorite)}
        </button>
        <span class="legend-name" title="服务端统一计算 · ${escapeHtml(basis)} · ${state}">${escapeHtml(series.name)}<small>${escapeHtml(basis)}${series.is_provisional ? " · 盘中" : ""}</small></span>
        <span class="legend-value">${displayValue}</span>
        <button class="legend-button legend-remove" type="button" data-action="remove" title="移除">×</button>
      </div>
    `;
  }).join("");
}

function getLegendValueIndex() {
  if (chartState.hoverIndex !== null) {
    return chartState.hoverIndex;
  }
  return Math.min(
    chartState.candles.length - 1,
    chartState.firstVisible + Math.min(chartState.visibleCount, chartState.candles.length) - 1,
  );
}

function getVisibleCandles() {
  const end = Math.min(chartState.candles.length, chartState.firstVisible + chartState.visibleCount);
  return chartState.candles.slice(chartState.firstVisible, end);
}

function getPriceRange(candles) {
  const lows = candles.map((candle) => candle.low);
  const highs = candles.map((candle) => candle.high);
  const visibleIndicatorValues = [];
  for (const series of chartState.indicatorSeries) {
    if (!series.visible || isOscillatorIndicator(series)) {
      continue;
    }
    for (let index = chartState.firstVisible; index < chartState.firstVisible + chartState.visibleCount; index += 1) {
      const value = series.values[index];
      if (Number.isFinite(value)) {
        visibleIndicatorValues.push(value);
      }
    }
  }
  const visibleTrendlineValues = [];
  for (const line of chartState.trendlines) {
    if (line.visible === false) {
      continue;
    }
    const start = Math.max(chartState.firstVisible, Number(line.start_index));
    const end = Math.min(
      chartState.firstVisible + chartState.visibleCount - 1,
      Number(line.projection_end_index ?? line.end_index),
    );
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      visibleTrendlineValues.push(trendlinePriceAt(line, start), trendlinePriceAt(line, end));
    }
  }
  const visibleKeyZoneValues = [];
  for (const zone of chartState.keyZones) {
    if (zone.visible === false) {
      continue;
    }
    const bounds = getKeyZoneDisplayBounds(zone);
    const start = Math.max(chartState.firstVisible, bounds.start);
    const end = Math.min(
      chartState.firstVisible + chartState.visibleCount - 1,
      bounds.end,
    );
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      visibleKeyZoneValues.push(Number(zone.zone_low), Number(zone.zone_high));
    }
    const currentIndex = chartState.candles.length - 1;
    if (
      zone.project_center_to_current === true
      && bounds.end < currentIndex
      && currentIndex >= chartState.firstVisible
      && bounds.end < chartState.firstVisible + chartState.visibleCount
    ) {
      visibleKeyZoneValues.push(Number(zone.center));
    }
  }

  let min = Math.min(...lows, ...visibleIndicatorValues, ...visibleTrendlineValues, ...visibleKeyZoneValues);
  let max = Math.max(...highs, ...visibleIndicatorValues, ...visibleTrendlineValues, ...visibleKeyZoneValues);
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    min = 0;
    max = 1;
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const padding = (max - min) * 0.08;
  return { min: min - padding, max: max + padding };
}

function getPlotArea() {
  const width = chartState.canvas.clientWidth;
  const height = chartState.canvas.clientHeight;
  const { left, top, right, bottom } = chartState.layout;
  return {
    left,
    top,
    right: width - right,
    bottom: height - bottom,
    width: Math.max(1, width - left - right),
    height: Math.max(1, height - top - bottom),
  };
}

function getTimeTicks(visible) {
  const maxTicks = 6;
  const step = Math.max(1, Math.ceil(visible.length / maxTicks));
  const tickMap = new Map();
  for (let index = 0; index < visible.length; index += step) {
    tickMap.set(index, { index, candle: visible[index], forceYear: false });
  }

  for (let index = 0; index < visible.length; index += 1) {
    const currentYear = getCandleYear(visible[index]);
    const previousYear = index > 0 ? getCandleYear(visible[index - 1]) : null;
    if (index === 0 || currentYear !== previousYear) {
      tickMap.set(index, { index, candle: visible[index], forceYear: true });
    }
  }

  const lastIndex = visible.length - 1;
  if (!tickMap.has(lastIndex)) {
    tickMap.set(lastIndex, { index: lastIndex, candle: visible[lastIndex], forceYear: false });
  }

  return Array.from(tickMap.values())
    .sort((a, b) => a.index - b.index)
    .map((tick) => ({
      ...tick,
      label: formatDateLabel(tick.candle, tick.forceYear),
    }));
}

function priceToY(price, plot, range) {
  return plot.bottom - ((price - range.min) / (range.max - range.min)) * plot.height;
}

function yToPrice(y, plot, range) {
  return range.max - ((y - plot.top) / plot.height) * (range.max - range.min);
}

function getPlotRatio(offsetX) {
  const plot = getPlotArea();
  return clamp((offsetX - plot.left) / plot.width, 0, 1);
}

function getCandleSlotWidth() {
  const plot = getPlotArea();
  return Math.max(1, plot.width / Math.max(1, chartState.visibleCount));
}

function getDefaultVisibleCount() {
  if (!chartState.candles.length) {
    return chartState.minVisibleCount;
  }
  return Math.min(chartState.candles.length, chartState.defaultVisibleCount);
}

function getMinimumVisibleCount() {
  return Math.min(chartState.candles.length || chartState.minVisibleCount, chartState.minVisibleCount);
}

function getMaximumVisibleCount() {
  return Math.max(chartState.candles.length, chartState.defaultVisibleCount);
}

function clampViewport() {
  if (!chartState.candles.length) {
    chartState.firstVisible = 0;
    chartState.visibleCount = chartState.minVisibleCount;
    return;
  }

  chartState.visibleCount = clamp(
    chartState.visibleCount || getDefaultVisibleCount(),
    getMinimumVisibleCount(),
    getMaximumVisibleCount(),
  );

  const maxFirst = Math.max(0, chartState.candles.length - chartState.visibleCount);
  chartState.firstVisible = clamp(chartState.firstVisible, 0, maxFirst);
}

function formatPrice(value) {
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: value >= 100 ? 2 : 3,
    maximumFractionDigits: value >= 100 ? 2 : 3,
  });
}

function formatPercent(value) {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatVolume(value) {
  if (value >= 1_000_000_000) {
    return `${(value / 1_000_000_000).toFixed(2)}B`;
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(2)}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(2)}K`;
  }
  return String(Math.round(value));
}

function formatDateLabel(candle, forceYear = false) {
  const source = candle.endDate || candle.date;
  if (["1m", "15m", "1h", "4h"].includes(chartState.period)
      || /^[1-9]\d{0,2}m$/.test(chartState.period)) {
    return forceYear ? source.slice(0, 10) : source.slice(5, 16);
  }
  if (forceYear) {
    return source.slice(0, 7);
  }
  if (chartState.period === "1M") {
    return source.slice(5, 7);
  }
  return source.slice(5);
}

function getCandleYear(candle) {
  const source = candle.endDate || candle.date;
  return source.slice(0, 4);
}

function drawLine(ctx, x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(Math.round(x1) + 0.5, Math.round(y1) + 0.5);
  ctx.lineTo(Math.round(x2) + 0.5, Math.round(y2) + 0.5);
  ctx.stroke();
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function getChartPeriodLabel() {
  return periodLabels[chartState.period] || "日K";
}

function getChartPeriod() {
  return chartState.period;
}

function eyeIcon(visible) {
  if (!visible) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18" /><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" /><path d="M9.9 5.1A9.8 9.8 0 0 1 12 5c5 0 8.7 4.1 10 7a14.5 14.5 0 0 1-2.1 3.2" /><path d="M6.6 6.6A14.2 14.2 0 0 0 2 12c1.3 2.9 5 7 10 7a9.7 9.7 0 0 0 4.1-.9" /></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>';
}

function starIcon(active) {
  const fill = active ? "currentColor" : "none";
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="${fill}" d="M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.2-4.1 5.8-.8L12 3.5z" /></svg>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
