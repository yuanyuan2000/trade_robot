const bta = {
  open: false,
  runId: null,
  meta: null,
  analysis: null,
  candles: null,
  months: 3,
  hiddenSeries: new Set(),
  leveragedBenchmarks: false,
  selectedDate: null,
  selectedSymbol: null,
  pinned: false,
  followLatest: true,
  decisionCache: new Map(),
  decisionTimer: null,
  liveRefreshTimer: null,
};

const btaPage = document.getElementById("backtest-analysis-page");
const btaStatus = document.getElementById("backtest-analysis-status");
const btaStart = document.getElementById("backtest-analysis-start");
const btaEnd = document.getElementById("backtest-analysis-end");
const btaChart = document.getElementById("backtest-analysis-chart");
const btaCandleChart = document.getElementById("backtest-analysis-candles");

function btaDate(value) {
  return new Date(`${value}T00:00:00Z`);
}

function btaIso(value) {
  return value.toISOString().slice(0, 10);
}

function btaShiftMonths(value, months) {
  const source = btaDate(value);
  const day = source.getUTCDate();
  source.setUTCDate(1);
  source.setUTCMonth(source.getUTCMonth() + months);
  const nextMonth = new Date(source);
  nextMonth.setUTCMonth(nextMonth.getUTCMonth() + 1);
  nextMonth.setUTCDate(0);
  source.setUTCDate(Math.min(day, nextMonth.getUTCDate()));
  return btaIso(source);
}

function btaAddDays(value, days) {
  const parsed = btaDate(value);
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return btaIso(parsed);
}

function btaClamp(value, minimum, maximum) {
  return value < minimum ? minimum : value > maximum ? maximum : value;
}

function btaStartForEnd(end, months) {
  return btaAddDays(btaShiftMonths(end, -months), 1);
}

function btaEndForStart(start, months) {
  return btaAddDays(btaShiftMonths(start, months), -1);
}

function btaSetStatus(message, type = "neutral") {
  btaStatus.textContent = message;
  btaStatus.className = `status ${type}`;
}

function btaSyncLeverageButton() {
  const button = document.getElementById("backtest-analysis-leverage");
  button.setAttribute("aria-pressed", String(bta.leveragedBenchmarks));
  button.textContent = bta.leveragedBenchmarks ? "✓ 杠杆" : "杠杆";
}

function btaSeriesColor(series, index) {
  if (series.type === "strategy") return "#2563eb";
  if (series.type === "equal_weight") return "#f59e0b";
  if (series.type === "benchmark") return "#a855f7";
  return `hsl(${Math.round((index * 137.508 + 18) % 360)} 68% 48%)`;
}

function btaResizeCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.round(rect.width));
  const height = Math.max(220, Math.round(rect.height));
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { context, width, height };
}

function btaRenderMetrics(metrics = {}) {
  const values = [
    ["区间收益", btPercent(metrics.return_rate)],
    ["区间最大回撤", btPercent(metrics.max_drawdown)],
    ["区间成交", String(metrics.trade_count ?? 0)],
    ["卖出实现盈亏", btMoney(metrics.realized_pnl)],
  ];
  document.getElementById("backtest-analysis-metrics").innerHTML = values.map(([label, value]) => `
    <div class="backtest-analysis-metric"><span>${btEscape(label)}</span><strong>${btEscape(value)}</strong></div>
  `).join("");
}

function btaRenderLegend() {
  const target = document.getElementById("backtest-analysis-legend");
  const series = bta.analysis?.series || [];
  target.innerHTML = series.map((item, index) => {
    const color = btaSeriesColor(item, index);
    const active = !bta.hiddenSeries.has(item.key);
    const benchmark = item.configured_benchmark && !item.label.includes("配置基准") ? "（配置基准）" : "";
    const leverage = bta.leveragedBenchmarks && item.type !== "strategy"
      ? item.leverage_mode === "per_asset"
        ? "（按标的杠杆）"
        : `（${Number(item.leverage_multiplier || 1).toFixed(2).replace(/\.?0+$/, "")}x）`
      : "";
    return `<button type="button" data-series-key="${btEscape(item.key)}" data-series-type="${btEscape(item.type)}" aria-pressed="${active}">
      <span class="backtest-analysis-legend-dot" style="background:${color}"></span>
      <span>${btEscape(item.label + benchmark + leverage)}</span>
    </button>`;
  }).join("");
}

function btaSeriesPoints(item) {
  if (bta.leveragedBenchmarks && item.type !== "strategy") {
    return item.leveraged_points || item.points || [];
  }
  return item.points || [];
}

function btaVisibleSeries() {
  return (bta.analysis?.series || []).filter((item) => !bta.hiddenSeries.has(item.key));
}

function btaDrawChart() {
  const { context, width, height } = btaResizeCanvas(btaChart);
  context.clearRect(0, 0, width, height);
  const empty = document.getElementById("backtest-analysis-chart-empty");
  const dates = bta.analysis?.range?.trading_dates || [];
  const series = btaVisibleSeries();
  const values = series.flatMap((item) => btaSeriesPoints(item).map((point) => Number(point.return_rate))).filter(Number.isFinite);
  empty.hidden = Boolean(dates.length && values.length);
  if (!dates.length || !values.length) return;

  const styles = getComputedStyle(document.documentElement);
  const colors = {
    grid: styles.getPropertyValue("--border").trim(),
    label: styles.getPropertyValue("--muted").trim(),
    background: styles.getPropertyValue("--chart-bg").trim(),
  };
  const padding = { left: 58, right: 18, top: 14, bottom: 30 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  let minimum = Math.min(...values, 0);
  let maximum = Math.max(...values, 0);
  const span = Math.max(0.01, maximum - minimum);
  minimum -= span * 0.08;
  maximum += span * 0.08;
  const x = (index) => padding.left + (dates.length === 1 ? plotWidth / 2 : index / (dates.length - 1) * plotWidth);
  const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;

  context.font = "11px system-ui";
  context.lineWidth = 1;
  for (let index = 0; index <= 4; index += 1) {
    const py = padding.top + index / 4 * plotHeight;
    const value = maximum - index / 4 * (maximum - minimum);
    context.strokeStyle = colors.grid;
    context.beginPath();
    context.moveTo(padding.left, py);
    context.lineTo(width - padding.right, py);
    context.stroke();
    context.fillStyle = colors.label;
    context.textAlign = "right";
    context.fillText(`${(value * 100).toFixed(1)}%`, padding.left - 7, py + 4);
  }
  const dateIndex = new Map(dates.map((value, index) => [value, index]));
  const orderedSeries = (bta.analysis.series || [])
    .map((item, index) => ({ item, index }))
    .sort((left, right) => Number(left.item.type === "strategy") - Number(right.item.type === "strategy"));
  orderedSeries.forEach(({ item, index: seriesIndex }) => {
    if (bta.hiddenSeries.has(item.key)) return;
    context.strokeStyle = btaSeriesColor(item, seriesIndex);
    context.lineWidth = item.type === "strategy" ? 3.0 : item.type === "equal_weight" ? 2.1 : 1.35;
    context.shadowColor = item.type === "strategy" ? btaSeriesColor(item, seriesIndex) : "transparent";
    context.shadowBlur = item.type === "strategy" ? 5 : 0;
    context.setLineDash(item.type === "equal_weight" || item.type === "benchmark" ? [6, 4] : []);
    context.beginPath();
    let started = false;
    btaSeriesPoints(item).forEach((point) => {
      const index = dateIndex.get(point.trading_date);
      if (index == null || !Number.isFinite(Number(point.return_rate))) return;
      if (!started) context.moveTo(x(index), y(Number(point.return_rate)));
      else context.lineTo(x(index), y(Number(point.return_rate)));
      started = true;
    });
    if (started) context.stroke();
    context.shadowColor = "transparent";
    context.shadowBlur = 0;
  });
  context.setLineDash([]);

  const tickCount = Math.min(5, dates.length);
  for (let index = 0; index < tickCount; index += 1) {
    const pointIndex = Math.round(index / Math.max(1, tickCount - 1) * (dates.length - 1));
    context.fillStyle = colors.label;
    context.textAlign = "center";
    context.fillText(dates[pointIndex].slice(5), x(pointIndex), height - 10);
  }

  if (bta.selectedDate && dateIndex.has(bta.selectedDate)) {
    const px = x(dateIndex.get(bta.selectedDate));
    context.strokeStyle = colors.label;
    context.setLineDash([4, 4]);
    context.beginPath();
    context.moveTo(px, padding.top);
    context.lineTo(px, height - padding.bottom);
    context.stroke();
    context.setLineDash([]);
  }
}

function btaRenderDecision(payload) {
  const target = document.getElementById("backtest-analysis-decision");
  document.getElementById("backtest-analysis-decision-date").textContent = payload.date || "—";
  if (!(payload.rows || []).length) {
    target.innerHTML = '<div class="backtest-empty">该交易日没有可展示的策略决策。</div>';
    return;
  }
  if (payload.mode === "competition") {
    const help = payload.formula_help
      ? `<span class="backtest-analysis-help" title="${btEscape(payload.formula_help)}">?</span>` : "";
    const rows = payload.rows.map((row) => {
      const status = row.filtered ? `过滤${row.filter_reasons?.length ? `：${row.filter_reasons.join("、")}` : ""}` : "通过";
      return `<div class="backtest-analysis-table-row">
        <span class="backtest-analysis-cell" title="${btEscape(row.symbol || "—")}">${btEscape(row.symbol || "—")}</span>
        <span class="backtest-analysis-cell ${row.filtered ? "backtest-analysis-filtered" : "backtest-analysis-passed"}" title="${btEscape(status)}">${btEscape(status)}</span>
        <span class="backtest-analysis-cell">${Number(row.holding_percent || 0).toFixed(2).replace(/\.00$/, "")}%</span>
        <span class="backtest-analysis-cell">${row.score == null ? "—" : Number(row.score).toFixed(2)}</span>
        <span class="backtest-analysis-cell backtest-analysis-formula" title="${btEscape(row.formula || "—")}">${btEscape(row.formula || "—")}</span>
      </div>`;
    }).join("");
    target.innerHTML = `<div class="backtest-analysis-table">
      <div class="backtest-analysis-table-row header"><span>标的</span><span>过滤状态</span><span>持仓</span><span>评分</span><span>评分计算公式${help}</span></div>
      ${rows}
    </div>`;
    return;
  }
  const rows = payload.rows.map((row) => `
    <div class="backtest-analysis-table-row rules" title="${btEscape(row.resolved_condition || "")}">
      <span class="backtest-analysis-cell">${btEscape(row.symbol || "—")}</span>
      <span class="backtest-analysis-cell">${btEscape(row.content || row.rule_name || "—")}</span>
      <span class="backtest-analysis-cell ${row.matched ? "backtest-analysis-passed" : ""}">${row.matched ? "成立" : "不成立"}</span>
    </div>`).join("");
  target.innerHTML = `<div class="backtest-analysis-table">
    <div class="backtest-analysis-table-row header rules"><span>标的</span><span>规则内容</span><span>结果</span></div>
    ${rows}
  </div>`;
}

async function btaLoadDecision(value) {
  if (!bta.runId || !value) return;
  const key = `${bta.runId}:${value}`;
  if (bta.decisionCache.has(key)) {
    btaRenderDecision(bta.decisionCache.get(key));
    return;
  }
  try {
    const payload = await btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis/decision?date=${encodeURIComponent(value)}`));
    bta.decisionCache.set(key, payload);
    if (bta.selectedDate === value) btaRenderDecision(payload);
  } catch (error) {
    document.getElementById("backtest-analysis-decision").innerHTML = `<div class="backtest-empty">${btEscape(btErrorText(error))}</div>`;
  }
}

function btaScheduleDecision(value) {
  window.clearTimeout(bta.decisionTimer);
  bta.decisionTimer = window.setTimeout(() => btaLoadDecision(value), 90);
}

function btaRenderTrades(payload) {
  const summary = payload.summary || {};
  document.getElementById("backtest-analysis-trade-symbol").textContent = payload.symbol || "—";
  const fields = [
    ["买入 / 卖出 / 盈利卖单", `${summary.buy_count || 0} / ${summary.sell_count || 0} / ${summary.profitable_sell_count || 0}`],
    ["卖出实现盈亏", btMoney(summary.realized_pnl)],
    ["手续费", btMoney(summary.commission)],
    ["收益率", btPercent(summary.return_rate)],
  ];
  document.getElementById("backtest-analysis-trade-summary").innerHTML = fields.map(([label, value]) => `
    <div><span>${btEscape(label)}</span><strong>${btEscape(value)}</strong></div>
  `).join("");
  document.getElementById("backtest-analysis-trades").innerHTML = (payload.trades || []).length
    ? payload.trades.map((trade) => {
      const pnl = trade.realized_pnl == null ? null : Number(trade.realized_pnl);
      const pnlClass = pnl > 0
        ? "backtest-analysis-pnl-positive"
        : pnl < 0 ? "backtest-analysis-pnl-negative" : "";
      const pnlHtml = pnl == null
        ? ""
        : ` · <strong class="${pnlClass}">${btEscape(btMoney(pnl))}</strong>`;
      return `<div class="backtest-analysis-trade-row" title="${btEscape(trade.reason || "")}">
        <span>${btEscape(String(trade.event_time || "").slice(5, 16))}</span>
        <strong class="${trade.side === "BUY" ? "backtest-analysis-buy" : "backtest-analysis-sell"}">${trade.side === "BUY" ? "买" : "卖"}</strong>
        <span>${btEscape(`${Number(trade.quantity).toFixed(2)} @ ${Number(trade.fill_price).toFixed(2)}`)}${pnlHtml}</span>
      </div>`;
    }).join("")
    : '<div class="backtest-empty">所选标的在区间内没有成交。</div>';
}

function btaDrawCandles() {
  const { context, width, height } = btaResizeCanvas(btaCandleChart);
  context.clearRect(0, 0, width, height);
  const candles = bta.candles?.candles || [];
  const empty = document.getElementById("backtest-analysis-candle-empty");
  empty.hidden = Boolean(candles.length);
  if (!candles.length) return;
  const styles = getComputedStyle(document.documentElement);
  const grid = styles.getPropertyValue("--border").trim();
  const label = styles.getPropertyValue("--muted").trim();
  // Leave a dedicated marker gutter above and below the price range so B/S
  // badges remain visually separate from candle bodies and date labels.
  const padding = { left: 58, right: 16, top: 34, bottom: 42 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const lows = candles.map((item) => Number(item.low));
  const highs = candles.map((item) => Number(item.high));
  const minimum = Math.min(...lows);
  const maximum = Math.max(...highs);
  const span = Math.max(0.0001, maximum - minimum);
  const y = (value) => padding.top + (maximum + span * .08 - value) / (span * 1.16) * plotHeight;
  const x = (index) => padding.left + (index + .5) / candles.length * plotWidth;
  const bodyWidth = Math.max(2, Math.min(10, plotWidth / candles.length * .62));
  context.font = "11px system-ui";
  for (let index = 0; index <= 4; index += 1) {
    const py = padding.top + index / 4 * plotHeight;
    const value = maximum + span * .08 - index / 4 * span * 1.16;
    context.strokeStyle = grid;
    context.beginPath();
    context.moveTo(padding.left, py);
    context.lineTo(width - padding.right, py);
    context.stroke();
    context.fillStyle = label;
    context.textAlign = "right";
    context.fillText(value.toFixed(2), padding.left - 7, py + 4);
  }
  const tradeByDate = new Map();
  (bta.candles?.trades || []).forEach((trade) => {
    const day = String(trade.event_time || "").slice(0, 10);
    if (!tradeByDate.has(day)) tradeByDate.set(day, []);
    tradeByDate.get(day).push(trade);
  });

  function drawTradeMarker(px, anchorY, side, stackIndex) {
    const buy = side === "BUY";
    const color = buy ? "#0284c7" : "#ea580c";
    const direction = buy ? 1 : -1;
    const tipY = anchorY + direction * 7;
    const boxWidth = 22;
    const boxHeight = 16;
    const gap = 5 + stackIndex * 21;
    const boxTop = buy ? tipY + gap : tipY - gap - boxHeight;
    const boxLeft = px - boxWidth / 2;

    context.save();
    context.strokeStyle = color;
    context.fillStyle = color;
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(px, anchorY + direction * 2);
    context.lineTo(px, tipY);
    context.stroke();

    context.beginPath();
    context.moveTo(px, tipY);
    context.lineTo(px - 4.5, tipY + direction * 6);
    context.lineTo(px + 4.5, tipY + direction * 6);
    context.closePath();
    context.fill();

    context.beginPath();
    context.roundRect(boxLeft, boxTop, boxWidth, boxHeight, 4);
    context.fill();
    context.strokeStyle = "rgba(255,255,255,.9)";
    context.lineWidth = 1;
    context.stroke();
    context.fillStyle = "#ffffff";
    context.font = "700 10px system-ui";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(buy ? "B" : "S", px, boxTop + boxHeight / 2 + .5);
    context.restore();
  }

  candles.forEach((item, index) => {
    const open = Number(item.open);
    const close = Number(item.close);
    const px = x(index);
    const color = close >= open ? "#16a34a" : "#dc2626";
    context.strokeStyle = color;
    context.fillStyle = color;
    context.beginPath();
    context.moveTo(px, y(Number(item.high)));
    context.lineTo(px, y(Number(item.low)));
    context.stroke();
    const top = Math.min(y(open), y(close));
    const bodyHeight = Math.max(1, Math.abs(y(open) - y(close)));
    context.fillRect(px - bodyWidth / 2, top, bodyWidth, bodyHeight);
    const stacks = { BUY: 0, SELL: 0 };
    (tradeByDate.get(item.date) || []).forEach((trade) => {
      const side = trade.side === "BUY" ? "BUY" : "SELL";
      drawTradeMarker(
        px,
        side === "BUY" ? y(Number(item.low)) : y(Number(item.high)),
        side,
        stacks[side],
      );
      stacks[side] += 1;
    });
    if (item.date === bta.selectedDate) {
      context.strokeStyle = label;
      context.setLineDash([3, 3]);
      context.beginPath();
      context.moveTo(px, padding.top);
      context.lineTo(px, height - padding.bottom);
      context.stroke();
      context.setLineDash([]);
    }
  });
  const tickCount = Math.min(5, candles.length);
  for (let index = 0; index < tickCount; index += 1) {
    const pointIndex = Math.round(index / Math.max(1, tickCount - 1) * (candles.length - 1));
    context.fillStyle = label;
    context.textAlign = "center";
    context.fillText(candles[pointIndex].date.slice(5), x(pointIndex), height - 9);
  }
}

async function btaLoadRange() {
  if (!bta.runId || !btaStart.value || !btaEnd.value) return;
  btaSetStatus("正在计算所选区间...", "neutral");
  const query = `start_date=${encodeURIComponent(btaStart.value)}&end_date=${encodeURIComponent(btaEnd.value)}`;
  try {
    const [analysis, candles] = await Promise.all([
      btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis?${query}`)),
      btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis/candles?symbol=${encodeURIComponent(bta.selectedSymbol)}&${query}`)),
    ]);
    bta.analysis = analysis;
    bta.candles = candles;
    const actual = analysis.range;
    document.getElementById("backtest-analysis-actual").textContent = `计划周期：${bta.months}个月｜实际：${actual.actual_start_date} 至 ${actual.actual_end_date}`;
    bta.selectedDate = bta.selectedDate && actual.trading_dates.includes(bta.selectedDate)
      ? bta.selectedDate : actual.actual_end_date;
    btaRenderMetrics(analysis.metrics);
    const leverageButton = document.getElementById("backtest-analysis-leverage");
    const assumed = [];
    if (analysis.benchmark_leverage?.dynamic_symbol_assumed_one) assumed.push("VOLAT 动态单标的杠杆");
    if (analysis.benchmark_leverage?.dynamic_special_assumed_one) assumed.push("策略动态特殊杠杆");
    leverageButton.title = assumed.length
      ? `各基准按整体杠杆 × 单标的杠杆 × 特殊杠杆计算；${assumed.join("、")}在固定倍数比较曲线中按 1 倍假设。期间不调仓，不计融资利息和手续费。`
      : "各基准按整体杠杆 × 单标的杠杆 × 特殊杠杆计算。期间不调仓，不计融资利息和手续费。";
    btaRenderLegend();
    btaDrawChart();
    btaDrawCandles();
    btaRenderTrades(candles);
    btaScheduleDecision(bta.selectedDate);
    const warning = [...(analysis.warnings || []), candles.warning].filter(Boolean).join("；");
    btaSetStatus(warning || "精细化分析已更新。", warning ? "warning" : "success");
  } catch (error) {
    btaSetStatus(btErrorText(error), "error");
  }
}

async function btaLoadCandles() {
  if (!bta.runId || !bta.selectedSymbol || !btaStart.value || !btaEnd.value) return;
  const query = `symbol=${encodeURIComponent(bta.selectedSymbol)}&start_date=${encodeURIComponent(btaStart.value)}&end_date=${encodeURIComponent(btaEnd.value)}`;
  try {
    bta.candles = await btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis/candles?${query}`));
    btaDrawCandles();
    btaRenderTrades(bta.candles);
  } catch (error) {
    btaSetStatus(btErrorText(error), "error");
  }
}

async function openBacktestAnalysis() {
  if (!bt.currentRunId) return;
  bta.runId = bt.currentRunId;
  bta.open = true;
  bta.hiddenSeries.clear();
  bta.leveragedBenchmarks = false;
  btaSyncLeverageButton();
  bta.decisionCache.clear();
  bta.pinned = false;
  bta.followLatest = true;
  btListPage.hidden = true;
  btResultsPage.hidden = true;
  btWorkspace.hidden = true;
  btaPage.hidden = false;
  btaSetStatus("正在读取已完成回测区间...", "neutral");
  try {
    bta.meta = await btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis/meta`));
    if (!bta.meta.available) throw new Error("回测尚未完成三个月，暂不能打开精细化分析。");
    document.getElementById("backtest-analysis-title").textContent = `${bta.meta.strategy_name} · 精细化分析`;
    document.getElementById("backtest-analysis-subtitle").textContent = `回测 #${bta.runId} · 可用区间 ${bta.meta.available_start_date} 至 ${bta.meta.available_end_date}`;
    document.getElementById("backtest-analysis-live").hidden = ["completed", "failed", "cancelled"].includes(bta.meta.status);
    btaStart.min = bta.meta.available_start_date;
    btaStart.max = bta.meta.available_end_date;
    btaEnd.min = bta.meta.available_start_date;
    btaEnd.max = bta.meta.available_end_date;
    btaEnd.value = bta.meta.available_end_date;
    btaStart.value = btaClamp(btaStartForEnd(btaEnd.value, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
    const symbolSelect = document.getElementById("backtest-analysis-symbol");
    symbolSelect.innerHTML = bta.meta.symbols.map((symbol) => `<option value="${btEscape(symbol)}">${btEscape(symbol)}</option>`).join("");
    bta.selectedSymbol = bta.meta.symbols[0] || null;
    symbolSelect.value = bta.selectedSymbol;
    await btaLoadRange();
  } catch (error) {
    btaSetStatus(btErrorText(error), "error");
  }
}

function btaClose() {
  bta.open = false;
  btaPage.hidden = true;
  btWorkspace.hidden = false;
}

document.getElementById("backtest-analysis-back").addEventListener("click", btaClose);
document.getElementById("backtest-analysis-unpin").addEventListener("click", () => {
  bta.pinned = false;
  document.getElementById("backtest-analysis-unpin").hidden = true;
});
document.getElementById("backtest-analysis-periods").addEventListener("click", (event) => {
  const button = event.target.closest("[data-months]");
  if (!button || !bta.meta) return;
  bta.months = Number(button.dataset.months);
  document.querySelectorAll("#backtest-analysis-periods [data-months]").forEach((item) => item.classList.toggle("active", item === button));
  btaStart.value = btaClamp(btaStartForEnd(btaEnd.value, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  bta.followLatest = btaEnd.value === bta.meta.available_end_date;
  btaLoadRange();
});
btaStart.addEventListener("change", () => {
  if (!bta.meta) return;
  btaEnd.value = btaClamp(btaEndForStart(btaStart.value, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  bta.followLatest = btaEnd.value === bta.meta.available_end_date;
  btaLoadRange();
});
btaEnd.addEventListener("change", () => {
  if (!bta.meta) return;
  btaStart.value = btaClamp(btaStartForEnd(btaEnd.value, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  bta.followLatest = btaEnd.value === bta.meta.available_end_date;
  btaLoadRange();
});
document.getElementById("backtest-analysis-prev").addEventListener("click", () => {
  if (!bta.meta) return;
  const start = btaClamp(btaShiftMonths(btaStart.value, -bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  btaStart.value = start;
  btaEnd.value = btaClamp(btaEndForStart(start, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  bta.followLatest = false;
  btaLoadRange();
});
document.getElementById("backtest-analysis-next").addEventListener("click", () => {
  if (!bta.meta) return;
  const end = btaClamp(btaShiftMonths(btaEnd.value, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  btaEnd.value = end;
  btaStart.value = btaClamp(btaStartForEnd(end, bta.months), bta.meta.available_start_date, bta.meta.available_end_date);
  bta.followLatest = end === bta.meta.available_end_date;
  btaLoadRange();
});
document.getElementById("backtest-analysis-symbol").addEventListener("change", (event) => {
  bta.selectedSymbol = event.target.value;
  btaLoadCandles();
});
document.getElementById("backtest-analysis-legend").addEventListener("click", (event) => {
  const button = event.target.closest("[data-series-key]");
  if (!button) return;
  const key = button.dataset.seriesKey;
  if (bta.hiddenSeries.has(key)) bta.hiddenSeries.delete(key);
  else bta.hiddenSeries.add(key);
  btaRenderLegend();
  btaDrawChart();
});
document.querySelector(".backtest-analysis-legend-actions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-legend-mode]");
  if (!button || !bta.analysis) return;
  const mode = button.dataset.legendMode;
  bta.hiddenSeries.clear();
  if (mode !== "all") {
    bta.analysis.series.forEach((item) => {
      const keep = item.type === "strategy" || (mode === "core" && item.type === "equal_weight");
      if (!keep) bta.hiddenSeries.add(item.key);
    });
  }
  btaRenderLegend();
  btaDrawChart();
});
document.getElementById("backtest-analysis-leverage").addEventListener("click", () => {
  bta.leveragedBenchmarks = !bta.leveragedBenchmarks;
  btaSyncLeverageButton();
  btaRenderLegend();
  btaDrawChart();
});
btaChart.addEventListener("mousemove", (event) => {
  if (bta.pinned || !bta.analysis?.range?.trading_dates?.length) return;
  const rect = btaChart.getBoundingClientRect();
  const dates = bta.analysis.range.trading_dates;
  const padding = { left: 58, right: 18 };
  const plotWidth = Math.max(1, rect.width - padding.left - padding.right);
  const relative = Math.max(0, Math.min(plotWidth, event.clientX - rect.left - padding.left));
  const index = Math.round(relative / plotWidth * Math.max(0, dates.length - 1));
  const value = dates[index];
  if (value === bta.selectedDate) return;
  bta.selectedDate = value;
  btaDrawChart();
  btaDrawCandles();
  btaScheduleDecision(value);
});
btaChart.addEventListener("click", () => {
  if (!bta.selectedDate) return;
  bta.pinned = true;
  document.getElementById("backtest-analysis-unpin").hidden = false;
  btaLoadDecision(bta.selectedDate);
});
document.addEventListener("keydown", (event) => {
  if (!bta.open || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const dates = bta.analysis?.range?.trading_dates || [];
  const index = dates.indexOf(bta.selectedDate);
  if (index < 0) return;
  const target = Math.max(0, Math.min(dates.length - 1, index + (event.key === "ArrowLeft" ? -1 : 1)));
  bta.selectedDate = dates[target];
  bta.pinned = true;
  document.getElementById("backtest-analysis-unpin").hidden = false;
  btaDrawChart();
  btaDrawCandles();
  btaLoadDecision(bta.selectedDate);
});
document.addEventListener("backtest-analysis-progress", (event) => {
  if (!bta.open || Number(event.detail?.runId) !== Number(bta.runId)) return;
  window.clearTimeout(bta.liveRefreshTimer);
  bta.liveRefreshTimer = window.setTimeout(async () => {
    try {
      const meta = await btJson(await fetch(`/api/backtest/runs/${bta.runId}/analysis/meta`));
      const changed = meta.available_end_date && meta.available_end_date !== bta.meta?.available_end_date;
      bta.meta = meta;
      btaStart.max = meta.available_end_date;
      btaEnd.max = meta.available_end_date;
      document.getElementById("backtest-analysis-subtitle").textContent = `回测 #${bta.runId} · 可用区间 ${meta.available_start_date} 至 ${meta.available_end_date}`;
      document.getElementById("backtest-analysis-live").hidden = ["completed", "failed", "cancelled"].includes(meta.status);
      if (changed && bta.followLatest) {
        btaEnd.value = meta.available_end_date;
        btaStart.value = btaClamp(btaStartForEnd(btaEnd.value, bta.months), meta.available_start_date, meta.available_end_date);
        bta.decisionCache.clear();
        await btaLoadRange();
      }
    } catch (error) {
      btaSetStatus(btErrorText(error), "error");
    }
  }, 500);
});
window.addEventListener("resize", () => {
  if (!bta.open) return;
  btaDrawChart();
  btaDrawCandles();
});
