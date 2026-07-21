const chartState = {
  container: null,
  canvas: null,
  ctx: null,
  tooltip: null,
  legend: null,
  rawRows: [],
  candles: [],
  indicators: [],
  indicatorSeries: [],
  trendlines: [],
  period: "1D",
  firstVisible: 0,
  visibleCount: 0,
  minVisibleCount: 8,
  dragStartX: 0,
  dragStartFirstVisible: 0,
  isDragging: false,
  hoverIndex: null,
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
    open: Number(row.open),
    high: Number(row.high),
    low: Number(row.low),
    close: Number(row.close),
    volume: Number(row.volume || 0),
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
  chartState.trendlines = Array.isArray(trendlines) ? trendlines : [];
  drawChart();
}

function clearChartTrendlines() {
  setChartTrendlines([]);
}

function bindPeriodButtons() {
  document.querySelectorAll(".period-button").forEach((button) => {
    button.addEventListener("click", () => {
      chartState.period = button.dataset.period;
      document.querySelectorAll(".period-button").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      rebuildPeriodCandles();
      resetViewportToFullRange();
      hideTooltip();
      drawChart();
      document.dispatchEvent(new CustomEvent("chart-period-change", {
        detail: { period: chartState.period, label: periodLabels[chartState.period] },
      }));
    });
  });
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
      hideTooltip();
      drawChart();
      return;
    }

    updateHover(event.clientX - rect.left, event.clientY - rect.top);
  });

  canvas.addEventListener("mouseleave", () => {
    if (!chartState.isDragging) {
      chartState.hoverIndex = null;
      hideTooltip();
      drawChart();
    }
  });
}

function rebuildPeriodCandles() {
  if (chartState.period === "1D") {
    chartState.candles = chartState.rawRows.map((row) => ({ ...row }));
    recalculateIndicators();
    return;
  }

  if (chartState.period === "3D") {
    chartState.candles = aggregateByCount(chartState.rawRows, 3);
    recalculateIndicators();
    return;
  }

  chartState.candles = aggregateByCalendar(chartState.rawRows, chartState.period);
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

  const plot = getPlotArea();
  drawGrid(ctx, plot, theme);

  if (!chartState.candles.length) {
    drawEmptyState(ctx, width, height);
    return;
  }

  const visible = getVisibleCandles();
  const priceRange = getPriceRange(visible);
  drawValueAxes(ctx, plot, visible, priceRange, theme);
  drawTimeAxis(ctx, plot, visible, theme);
  drawCandleSeries(ctx, plot, visible, priceRange, theme);
  drawIndicators(ctx, plot, priceRange);
  drawTrendlines(ctx, plot, priceRange, theme);
  drawCrosshair(ctx, plot, visible, priceRange, theme);
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

function drawCrosshair(ctx, plot, visible, priceRange, theme) {
  if (chartState.hoverIndex === null) {
    return;
  }

  const visibleIndex = chartState.hoverIndex - chartState.firstVisible;
  if (visibleIndex < 0 || visibleIndex >= visible.length) {
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const candle = visible[visibleIndex];
  const x = plot.left + slot * (visibleIndex + 0.5);
  const y = priceToY(candle.close, plot, priceRange);

  ctx.strokeStyle = theme.crosshair;
  ctx.setLineDash([4, 4]);
  drawLine(ctx, x, plot.top, x, plot.bottom);
  drawLine(ctx, plot.left, y, plot.right, y);
  ctx.setLineDash([]);
}

function drawIndicators(ctx, plot, priceRange) {
  const slot = plot.width / chartState.visibleCount;
  for (const series of chartState.indicatorSeries) {
    if (!series.visible) {
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

function drawTrendlines(ctx, plot, priceRange, theme) {
  if (!chartState.trendlines.length) {
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const visibleStart = chartState.firstVisible;
  const visibleEnd = chartState.firstVisible + chartState.visibleCount - 1;

  for (const line of chartState.trendlines) {
    const startIndex = Number(line.start_index);
    const endIndex = Number(line.end_index);
    const projectionEndIndex = Number(line.projection_end_index ?? line.end_index);
    if (!Number.isFinite(startIndex) || !Number.isFinite(endIndex) || !Number.isFinite(projectionEndIndex)) {
      continue;
    }
    if (projectionEndIndex < visibleStart || startIndex > visibleEnd) {
      continue;
    }

    const color = getTrendlineColor(line, theme);
    const width = getTrendlineWidth(line.tier);
    const dash = getTrendlineDash(line.tier);

    drawTrendlineSegment(
      ctx,
      plot,
      priceRange,
      slot,
      line,
      startIndex,
      Math.min(endIndex, projectionEndIndex),
      color,
      width,
      dash,
      0.94,
    );

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
        [2, 5],
        0.42,
      );
    }
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
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

function getTrendlineDash(tier) {
  if (tier === "long") {
    return [2, 6];
  }
  if (tier === "medium") {
    return [8, 5];
  }
  return [];
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
  const plot = getPlotArea();
  if (
    offsetX < plot.left
    || offsetX > plot.right
    || offsetY < plot.top
    || offsetY > plot.bottom
  ) {
    chartState.hoverIndex = null;
    hideTooltip();
    drawChart();
    return;
  }

  const slot = plot.width / chartState.visibleCount;
  const visibleIndex = clamp(Math.floor((offsetX - plot.left) / slot), 0, chartState.visibleCount - 1);
  const dataIndex = chartState.firstVisible + visibleIndex;
  if (dataIndex < 0 || dataIndex >= chartState.candles.length) {
    chartState.hoverIndex = null;
    hideTooltip();
    drawChart();
    return;
  }

  chartState.hoverIndex = dataIndex;
  showTooltip(chartState.candles[dataIndex], offsetX, offsetY);
  renderIndicatorLegend();
  drawChart();
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
  chartState.tooltip.hidden = true;
}

function recalculateIndicators() {
  chartState.indicatorSeries = chartState.indicators.map((indicator) => ({
    ...indicator,
    values: calculateIndicatorValues(chartState.candles, indicator),
  }));
}

function calculateIndicatorValues(candles, indicator) {
  const period = Number(indicator.params?.period);
  if (!Number.isInteger(period) || period < 2) {
    return candles.map(() => null);
  }
  if (indicator.indicator_type === "MA") {
    return calculateMA(candles, period);
  }
  if (indicator.indicator_type === "EMA") {
    return calculateEMA(candles, period);
  }
  return candles.map(() => null);
}

function calculateMA(candles, period) {
  const values = [];
  let sum = 0;
  for (let index = 0; index < candles.length; index += 1) {
    sum += candles[index].close;
    if (index >= period) {
      sum -= candles[index - period].close;
    }
    values.push(index >= period - 1 ? sum / period : null);
  }
  return values;
}

function calculateEMA(candles, period) {
  const values = candles.map(() => null);
  if (candles.length < period) {
    return values;
  }

  let sum = 0;
  for (let index = 0; index < period; index += 1) {
    sum += candles[index].close;
  }

  const multiplier = 2 / (period + 1);
  let previous = sum / period;
  values[period - 1] = previous;

  for (let index = period; index < candles.length; index += 1) {
    previous = candles[index].close * multiplier + previous * (1 - multiplier);
    values[index] = previous;
  }
  return values;
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
    const visibilityClass = series.visible ? "" : " is-hidden";
    const favoriteClass = series.is_favorite ? " is-favorite" : "";
    return `
      <div class="legend-row${visibilityClass}" data-symbol-indicator-id="${series.id}" data-indicator-id="${series.indicator_id}">
        <button class="legend-button${visibilityClass}" type="button" data-action="toggle-visible" title="${series.visible ? "隐藏" : "显示"}">
          ${eyeIcon(series.visible)}
        </button>
        <span class="legend-swatch" style="background:${escapeHtml(series.color)}"></span>
        <button class="legend-button${favoriteClass}" type="button" data-action="toggle-favorite" title="${series.is_favorite ? "取消收藏" : "收藏"}">
          ${starIcon(series.is_favorite)}
        </button>
        <span class="legend-name">${escapeHtml(series.name)}</span>
        <span class="legend-value">${value == null ? "-" : formatPrice(value)}</span>
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
    if (!series.visible) {
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
    const start = Math.max(chartState.firstVisible, Number(line.start_index));
    const end = Math.min(
      chartState.firstVisible + chartState.visibleCount - 1,
      Number(line.projection_end_index ?? line.end_index),
    );
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      visibleTrendlineValues.push(trendlinePriceAt(line, start), trendlinePriceAt(line, end));
    }
  }

  let min = Math.min(...lows, ...visibleIndicatorValues, ...visibleTrendlineValues);
  let max = Math.max(...highs, ...visibleIndicatorValues, ...visibleTrendlineValues);
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
