const bt = {
  strategies: [],
  codeCatalog: [],
  current: null,
  runs: [],
  currentRunId: null,
  eventSource: null,
  equityPoints: [],
  trades: [],
  logs: [],
  displayInitialCapital: 100000,
  chartHoverIndex: null,
  symbolDefaults: [],
  readOnly: false,
  returnToResults: false,
  resultsPage: 1,
  resultsTotalPages: 1,
};

const btListPage = document.getElementById("backtest-list-page");
const btWorkspace = document.getElementById("backtest-workspace");
const btResultsPage = document.getElementById("backtest-results-page");
const btStatus = document.getElementById("backtest-status");
const btWorkspaceStatus = document.getElementById("backtest-workspace-status");
const btStrategyTable = document.getElementById("backtest-strategy-table");
const btStrategyCount = document.getElementById("backtest-strategy-count");
const btCreateDialog = document.getElementById("backtest-create-dialog");
const btSettingsDialog = document.getElementById("backtest-settings-dialog");
const btName = document.getElementById("backtest-strategy-name");
const btDescription = document.getElementById("backtest-description");
const btSymbols = document.getElementById("backtest-symbols");
const btRules = document.getElementById("backtest-rules");
const btVisualEditor = document.getElementById("backtest-visual-editor");
const btCompetitionEditor = document.getElementById("backtest-competition-editor");
const btCodeEditor = document.getElementById("backtest-code-editor");
const btCodeParams = document.getElementById("backtest-code-params");
const btMetrics = document.getElementById("backtest-metrics");
const btCanvas = document.getElementById("backtest-equity-chart");
const btChartEmpty = document.getElementById("backtest-chart-empty");
const btLogOutput = document.getElementById("backtest-log-output");
const btLogCount = document.getElementById("backtest-log-count");
const btRunHistory = document.getElementById("backtest-run-history");
const btResultsStatus = document.getElementById("backtest-results-status");
const btResultsTable = document.getElementById("backtest-results-table");

function btEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function btJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error?.message || `请求失败（${response.status}）`);
    error.detail = payload.error?.detail;
    throw error;
  }
  return payload;
}

function btSetStatus(message, type = "neutral", workspace = false) {
  const element = workspace ? btWorkspaceStatus : btStatus;
  element.textContent = message;
  element.className = `status ${type}`;
}

function btErrorText(error) {
  if (!error.detail) return error.message;
  if (Array.isArray(error.detail)) {
    const first = error.detail[0];
    return `${error.message} ${first.symbol || ""} ${first.type || ""}`.trim();
  }
  return `${error.message} ${typeof error.detail === "string" ? error.detail : ""}`.trim();
}

async function loadBacktestStrategies() {
  btSetStatus("正在加载策略...", "neutral");
  try {
    const payload = await btJson(await fetch("/api/backtest/strategies"));
    bt.strategies = payload.strategies || [];
    renderBacktestStrategyList();
    btSetStatus("策略列表已更新。", "success");
  } catch (error) {
    btSetStatus(btErrorText(error), "error");
  }
}

async function loadBacktestCodeCatalog() {
  try {
    const payload = await btJson(await fetch("/api/backtest/code-strategies"));
    bt.codeCatalog = payload.strategies || [];
  } catch (error) {
    btSetStatus(btErrorText(error), "error");
  }
}

function renderBacktestStrategyList() {
  btStrategyCount.textContent = `共 ${bt.strategies.length} 个策略`;
  if (!bt.strategies.length) {
    btStrategyTable.innerHTML = '<tbody><tr><td class="empty-cell">暂无策略，请点击“新建策略”。</td></tr></tbody>';
    return;
  }
  const headers = ["名称", "设计模式", "选标模式", "标的", "最近运行", "最后修改", "操作"];
  const rows = bt.strategies.map((strategy) => {
    const symbols = (strategy.definition?.symbols || []).map((item) => item.symbol).join(", ");
    const run = strategy.latest_run;
    const runText = run
      ? `${btRunOutcomeLabel(run)}${run.metrics?.total_return != null ? ` · ${btPercent(run.metrics.total_return)}` : ""}`
      : "尚未运行";
    return `
      <tr class="backtest-strategy-row" data-strategy-id="${strategy.id}">
        <td><strong>${btEscape(strategy.name)}</strong></td>
        <td>${strategy.design_mode === "code" ? "代码" : "非代码"}</td>
        <td>${btEscape(strategy.selection_mode)}</td>
        <td title="${btEscape(symbols)}">${btEscape(symbols)}</td>
        <td>${btEscape(runText)}</td>
        <td>${btEscape(btDateTime(strategy.updated_at))}</td>
        <td>
          <div class="backtest-row-actions">
            ${strategy.design_mode === "visual" ? '<button type="button" data-bt-action="duplicate">复制</button>' : ""}
            ${strategy.design_mode === "code"
              ? '<button type="button" disabled title="内置代码模式策略禁止删除">删除</button>'
              : '<button type="button" data-bt-action="delete">删除</button>'}
          </div>
        </td>
      </tr>`;
  }).join("");
  btStrategyTable.innerHTML = `<thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead><tbody>${rows}</tbody>`;
}

async function openBacktestStrategy(strategyId) {
  btSetStatus("正在读取策略...", "neutral");
  try {
    bt.readOnly = false;
    bt.returnToResults = false;
    if (!bt.codeCatalog.length) await loadBacktestCodeCatalog();
    const payload = await btJson(await fetch(`/api/backtest/strategies/${strategyId}`));
    bt.current = payload.strategy;
    bt.symbolDefaults = structuredClone(payload.strategy.definition?.symbols || []);
    bt.runs = payload.runs || [];
    btListPage.hidden = true;
    btWorkspace.hidden = false;
    renderBacktestEditor();
    setBacktestReadOnly(false);
    if (bt.runs.length) {
      await loadBacktestRunResult(bt.runs[0].id, bt.runs[0].status);
    } else {
      resetBacktestResult();
    }
    btSetStatus("策略已加载。", "success", true);
  } catch (error) {
    btSetStatus(btErrorText(error), "error");
  }
}

function setBacktestReadOnly(readOnly) {
  bt.readOnly = Boolean(readOnly);
  btWorkspace.classList.toggle("backtest-readonly", bt.readOnly);
  ["backtest-save", "backtest-validate", "backtest-run"].forEach((id) => {
    document.getElementById(id).hidden = bt.readOnly;
  });
  const settingsButton = document.getElementById("backtest-settings");
  settingsButton.hidden = false;
  settingsButton.textContent = bt.readOnly ? "查看设置" : "设置";
  btSettingsDialog.querySelectorAll("input, select").forEach((element) => {
    element.disabled = bt.readOnly;
  });
  document.getElementById("backtest-settings-submit").hidden = bt.readOnly;
  document.getElementById("backtest-cancel").hidden = true;
  ["backtest-add-symbol", "backtest-reset-symbols", "backtest-add-rule"].forEach((id) => {
    const element = document.getElementById(id);
    if (element) element.hidden = bt.readOnly;
  });
  btWorkspace.querySelectorAll("input, textarea, select").forEach((element) => {
    element.disabled = bt.readOnly;
  });
  const historySection = btRunHistory.closest("section");
  if (historySection) historySection.hidden = bt.readOnly;
  document.getElementById("backtest-mode-badge").textContent = bt.readOnly
    ? "历史只读"
    : document.getElementById("backtest-mode-badge").textContent;
}

async function loadBacktestResultsOverview(page = 1) {
  btResultsStatus.textContent = "正在加载回测结果...";
  btResultsStatus.className = "status neutral";
  try {
    const payload = await btJson(await fetch(`/api/backtest/runs?page=${page}&page_size=25`));
    bt.resultsPage = payload.page;
    bt.resultsTotalPages = payload.total_pages;
    document.getElementById("backtest-results-count").textContent = `共 ${payload.total_rows} 条`;
    document.getElementById("backtest-results-page-text").textContent = `第 ${payload.page} 页 / 共 ${payload.total_pages} 页`;
    document.getElementById("backtest-results-prev").disabled = payload.page <= 1;
    document.getElementById("backtest-results-next").disabled = payload.page >= payload.total_pages;
    const headers = ["", "编号", "回测时间", "策略名称", "回测区间", "标的", "杠杆", "状态", "收益率", "最大回撤", "成交"];
    const rows = (payload.items || []).map((run) => `
      <tr data-result-run-id="${run.id}">
        <td><input class="bt-result-select" type="checkbox" value="${run.id}" ${["queued", "validating", "running", "cancelling"].includes(run.status) ? "disabled" : ""}></td>
        <td>#${run.id}</td>
        <td>${btEscape(btDateTime(run.created_at))}</td>
        <td>${btEscape(run.strategy_name)}</td>
        <td class="backtest-result-compact">${btCompactDate(run.settings?.start_date)}<br>${btCompactDate(run.settings?.end_date)}</td>
        <td class="backtest-result-compact backtest-result-symbols">${btEscape((run.symbols || []).join(" / ") || "—")}</td>
        <td>${btEscape(run.settings?.leverage_multiplier == null ? "—" : `${run.settings.leverage_multiplier}×`)}</td>
        <td>${btEscape(btRunOutcomeLabel(run))}</td>
        <td>${btPercent(run.metrics?.total_return)}</td>
        <td>${btPercent(run.metrics?.max_drawdown)}</td>
        <td>${btEscape(run.metrics?.trade_count ?? "—")}</td>
      </tr>`).join("");
    btResultsTable.innerHTML = `<thead><tr>${headers.map((item) => `<th>${item}</th>`).join("")}</tr></thead><tbody>${rows || '<tr><td colspan="11">暂无回测结果</td></tr>'}</tbody>`;
    document.getElementById("backtest-delete-runs").disabled = true;
    btResultsStatus.textContent = "回测结果已加载。";
    btResultsStatus.className = "status success";
  } catch (error) {
    btResultsStatus.textContent = btErrorText(error);
    btResultsStatus.className = "status error";
  }
}

async function openBacktestRunDetail(runId) {
  try {
    if (!bt.codeCatalog.length) await loadBacktestCodeCatalog();
    const payload = await btJson(await fetch(`/api/backtest/runs/${runId}/detail`));
    const run = payload.run;
    bt.current = structuredClone(run.strategy_snapshot || {});
    bt.current.default_settings = structuredClone(run.settings || {});
    bt.currentRunId = run.id;
    bt.runs = [];
    bt.equityPoints = payload.equity_points || [];
    bt.trades = payload.trades || [];
    bt.logs = run.logs_deleted_at ? [] : await loadAllBacktestLogs(run.id);
    bt.displayInitialCapital = Number(run.settings?.initial_capital || 100000);
    bt.returnToResults = true;
    btListPage.hidden = true;
    btResultsPage.hidden = true;
    btWorkspace.hidden = false;
    renderBacktestEditor();
    setBacktestReadOnly(true);
    renderBacktestMetrics(run.metrics);
    btChartEmpty.hidden = Boolean(bt.equityPoints.length);
    renderBacktestChart();
    renderBacktestLogs();
    renderBacktestTradeSummary(run);
    btSetStatus(
      run.logs_deleted_at ? `历史回测 #${run.id}；详细日志已清理。` : `历史回测 #${run.id}（只读）。`,
      run.logs_deleted_at ? "warning" : "neutral",
      true,
    );
  } catch (error) {
    btResultsStatus.textContent = btErrorText(error);
    btResultsStatus.className = "status error";
  }
}

function renderBacktestEditor() {
  const strategy = bt.current;
  btName.value = strategy.name;
  btDescription.value = strategy.description || "";
  document.getElementById("backtest-mode-badge").textContent =
    `${strategy.design_mode === "code" ? "代码" : "非代码"} · ${strategy.selection_mode}`;
  document.getElementById("backtest-revision").textContent = `revision ${strategy.revision}`;
  const help = {
    single: "单一标的，规则直接控制该标的仓位。",
    distribution: "多个标的独立执行相同规则；最大仓位之和不得超过 100%。",
    competition: "多个标的在同一数据截面评分，只持有最高分合格标的。",
  };
  document.getElementById("backtest-selection-help").textContent = help[strategy.selection_mode];
  renderBacktestSymbols();
  btVisualEditor.hidden = strategy.design_mode !== "visual";
  btCodeEditor.hidden = strategy.design_mode !== "code";
  btCompetitionEditor.hidden = strategy.design_mode !== "visual" || strategy.selection_mode !== "competition";
  if (strategy.design_mode === "visual") {
    renderBacktestRules();
    renderBacktestCompetition();
  } else {
    renderBacktestCodeParameters();
  }
  renderBacktestRunHistory();
}

function renderBacktestSymbols() {
  const isSevenStar = bt.current.design_mode === "code" && bt.current.code_key === "sevenstar_etf_rotation";
  btSymbols.innerHTML = (bt.current.definition.symbols || []).map((item, index) => `
    <div class="backtest-symbol-row" data-index="${index}">
      <input class="bt-symbol-code" type="text" value="${btEscape(item.symbol)}" placeholder="SPY" aria-label="标的代码">
      <input class="bt-symbol-weight" type="number" min="0.01" max="100" step="0.01" value="${isSevenStar ? 100 : Number(item.max_weight)}" aria-label="最大仓位" ${isSevenStar ? 'disabled title="七星策略按目标数量等权，候选标的上限固定为 100%"' : ""}>
      <input class="bt-symbol-leverage" type="number" min="1" max="10" step="0.1" value="${Number(item.leverage_multiplier ?? 1)}" aria-label="单标的杠杆倍率" title="最终有效杠杆 = 整体杠杆 × 单标的杠杆">
      <div class="backtest-symbol-order">
        <button class="bt-move-symbol" data-direction="up" type="button" aria-label="上移标的" ${index === 0 ? "disabled" : ""}>↑</button>
        <button class="bt-move-symbol" data-direction="down" type="button" aria-label="下移标的" ${index === bt.current.definition.symbols.length - 1 ? "disabled" : ""}>↓</button>
      </div>
      <button class="backtest-remove-button bt-remove-symbol" type="button" aria-label="移除标的">×</button>
    </div>
  `).join("");
}

function syncBacktestSymbolsFromEditor() {
  bt.current.definition.symbols = Array.from(
    btSymbols.querySelectorAll(".backtest-symbol-row")
  ).map((row) => ({
    symbol: row.querySelector(".bt-symbol-code").value.trim().toUpperCase(),
    max_weight: bt.current.code_key === "sevenstar_etf_rotation"
      ? 100
      : Number(row.querySelector(".bt-symbol-weight").value),
    leverage_multiplier: Number(row.querySelector(".bt-symbol-leverage").value),
  }));
}

function renderBacktestRules() {
  const rules = bt.current.definition.rules || [];
  btRules.innerHTML = rules.map((rule, index) => `
    <div class="backtest-rule" data-index="${index}" data-rule-id="${btEscape(rule.id)}">
      <label class="backtest-rule-enabled" title="启用规则"><input class="bt-rule-enabled" type="checkbox" ${rule.enabled ? "checked" : ""}></label>
      <select class="bt-rule-action" aria-label="动作">
        ${["BUY", "SELL", "HOLD"].map((value) => `<option value="${value}" ${rule.action === value ? "selected" : ""}>${value}</option>`).join("")}
      </select>
      <select class="bt-rule-sizing" aria-label="仓位方式">
        ${["TARGET", "DELTA"].map((value) => `<option value="${value}" ${rule.sizing_mode === value ? "selected" : ""}>${value}</option>`).join("")}
      </select>
      <input class="bt-rule-value" type="number" min="0" max="100" step="0.01" value="${Number(rule.value || 0)}" aria-label="仓位百分比">
      <input class="bt-rule-when" type="text" value="${btEscape(rule.when)}" aria-label="执行时间">
      <button class="backtest-remove-button bt-remove-rule" type="button" aria-label="移除规则">×</button>
      <input class="bt-rule-condition backtest-rule-condition" type="text" value="${btEscape(rule.condition)}" placeholder="price > ma(20) AND position < 0.5" aria-label="条件公式">
    </div>
  `).join("");
}

function renderBacktestCompetition() {
  const config = bt.current.definition.competition || {};
  document.getElementById("backtest-competition-eligibility").value = config.eligibility || "true";
  document.getElementById("backtest-competition-score").value = config.score || "(price - close(5)) / atr(5)";
  document.getElementById("backtest-competition-when").value = config.when || "OPEN";
  document.getElementById("backtest-competition-weight").value = Number(config.target_weight ?? 100);
  document.getElementById("backtest-competition-cash").checked = config.cash_when_none !== false;
}

function renderBacktestCodeParameters() {
  const spec = bt.codeCatalog.find((item) => item.key === bt.current.code_key);
  document.getElementById("backtest-code-title").textContent = spec?.name || bt.current.code_key;
  document.getElementById("backtest-code-description").textContent = spec?.description || "";
  if (!spec) {
    btCodeParams.innerHTML = '<div class="backtest-empty">代码策略未注册。</div>';
    return;
  }
  const params = bt.current.definition.params || {};
  btCodeParams.innerHTML = Object.entries(spec.parameter_schema).map(([name, field]) => {
    const value = params[name] ?? field.default;
    const maximum = name === "holdings_num"
      ? Math.min(field.maximum ?? Infinity, bt.current.definition.symbols?.length || 0)
      : field.maximum;
    const range = field.minimum != null || maximum != null
      ? `范围：${field.minimum ?? "不限"} ～ ${Number.isFinite(maximum) ? maximum : "不限"}${field.unit ? ` ${field.unit}` : ""}`
      : "";
    const notes = [
      `默认：${field.type === "choice"
        ? (field.options || []).find((option) => option.value === field.default)?.label || field.default
        : field.default}${field.unit ? ` ${field.unit}` : ""}`,
      range,
      field.help,
      field.suggestion,
    ].filter(Boolean);
    if (field.type === "boolean") {
      return `<label class="backtest-code-param backtest-code-param-boolean" data-param="${btEscape(name)}">
        <span class="backtest-code-param-label">${btEscape(field.label)}</span>
        <input class="bt-code-param" type="checkbox" ${value ? "checked" : ""}>
        <small>${notes.map(btEscape).join(" · ")}</small>
      </label>`;
    }
    if (field.type === "choice") {
      const options = (field.options || []).map((option) => `
        <option value="${btEscape(option.value)}" ${option.value === value ? "selected" : ""}>
          ${btEscape(option.label || option.value)}
        </option>`).join("");
      return `<label class="backtest-code-param" data-param="${btEscape(name)}">
        <span class="backtest-code-param-label">${btEscape(field.label)}</span>
        <select class="bt-code-param" required>${options}</select>
        <small>${notes.map(btEscape).join(" · ")}</small>
      </label>`;
    }
    const type = field.type === "time" ? "time" : field.type === "symbol" ? "text" : "number";
    return `
      <label class="backtest-code-param" data-param="${btEscape(name)}">
        <span class="backtest-code-param-label">${btEscape(field.label)}${field.unit ? `（${btEscape(field.unit)}）` : ""}</span>
        <input class="bt-code-param" type="${type}" value="${btEscape(value)}"
          ${field.minimum != null ? `min="${field.minimum}"` : ""}
          ${Number.isFinite(maximum) ? `max="${maximum}"` : ""}
          ${field.step != null ? `step="${field.step}"` : ""}
          ${field.type === "symbol" ? 'maxlength="24" pattern="[A-Za-z0-9^./=_-]{1,24}"' : ""} required>
        <small>${notes.map(btEscape).join(" · ")}</small>
      </label>`;
  }).join("");
}

function collectBacktestStrategy() {
  const invalidInput = btWorkspace.querySelector("input:invalid, select:invalid, textarea:invalid");
  if (invalidInput) {
    invalidInput.reportValidity();
    throw new Error("请先修正超出范围或格式不正确的参数。 ");
  }
  const strategy = structuredClone(bt.current);
  strategy.name = btName.value.trim();
  strategy.description = btDescription.value.trim();
  strategy.definition.symbols = Array.from(btSymbols.querySelectorAll(".backtest-symbol-row")).map((row) => ({
    symbol: row.querySelector(".bt-symbol-code").value.trim().toUpperCase(),
    max_weight: strategy.code_key === "sevenstar_etf_rotation"
      ? 100
      : Number(row.querySelector(".bt-symbol-weight").value),
    leverage_multiplier: Number(row.querySelector(".bt-symbol-leverage").value),
  }));
  const symbols = strategy.definition.symbols.map((item) => item.symbol);
  if (new Set(symbols).size !== symbols.length) throw new Error("候选池不能包含重复标的。");
  if (strategy.design_mode === "visual") {
    strategy.definition.rules = Array.from(btRules.querySelectorAll(".backtest-rule")).map((row, index) => ({
      id: row.dataset.ruleId || `rule-${Date.now()}-${index}`,
      name: `${row.querySelector(".bt-rule-action").value} if ${row.querySelector(".bt-rule-condition").value.trim() || "true"}`,
      enabled: row.querySelector(".bt-rule-enabled").checked,
      priority: (index + 1) * 10,
      action: row.querySelector(".bt-rule-action").value,
      sizing_mode: row.querySelector(".bt-rule-sizing").value,
      value: Number(row.querySelector(".bt-rule-value").value),
      when: row.querySelector(".bt-rule-when").value.trim().toUpperCase(),
      condition: row.querySelector(".bt-rule-condition").value.trim() || "true",
    }));
    if (strategy.selection_mode === "competition") {
      strategy.definition.competition = {
        eligibility: document.getElementById("backtest-competition-eligibility").value.trim() || "true",
        score: document.getElementById("backtest-competition-score").value.trim(),
        when: document.getElementById("backtest-competition-when").value.trim().toUpperCase(),
        target_weight: Number(document.getElementById("backtest-competition-weight").value),
        cash_when_none: document.getElementById("backtest-competition-cash").checked,
      };
    }
  } else {
    const spec = bt.codeCatalog.find((item) => item.key === strategy.code_key);
    if (!spec) throw new Error("代码策略目录尚未加载或该策略已不再注册。");
    strategy.definition.params = {};
    btCodeParams.querySelectorAll(".backtest-code-param").forEach((row) => {
      const field = spec.parameter_schema[row.dataset.param];
      const input = row.querySelector(".bt-code-param");
      strategy.definition.params[row.dataset.param] = field.type === "boolean"
        ? input.checked
        : field.type === "time"
          ? input.value
          : field.type === "choice"
            ? input.value
            : field.type === "symbol"
              ? input.value.trim().toUpperCase()
              : Number(input.value);
    });
    if (
      Number(strategy.definition.params.holdings_num) >
      strategy.definition.symbols.length
    ) {
      throw new Error("目标持仓数量不能超过候选池标的数量。");
    }
  }
  return strategy;
}

async function saveBacktestStrategy({ announce = true } = {}) {
  const strategy = collectBacktestStrategy();
  const payload = await btJson(await fetch(`/api/backtest/strategies/${strategy.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(strategy),
  }));
  bt.current = payload.strategy;
  renderBacktestEditor();
  if (announce) btSetStatus("策略和默认运行设置已保存。", "success", true);
  return bt.current;
}

function resetBacktestResult() {
  bt.currentRunId = null;
  bt.equityPoints = [];
  bt.trades = [];
  bt.logs = [];
  bt.chartHoverIndex = null;
  bt.displayInitialCapital = Number(bt.current?.default_settings?.initial_capital || 100000);
  btMetrics.innerHTML = '<div class="backtest-empty">运行后显示关键指标</div>';
  btChartEmpty.hidden = false;
  document.getElementById("backtest-trade-summary").textContent = "";
  renderBacktestChart();
  renderBacktestLogs();
}

function settingsToForm() {
  const value = bt.current.default_settings;
  document.getElementById("backtest-start-date").value = value.start_date;
  document.getElementById("backtest-end-date").value = value.end_date;
  document.getElementById("backtest-initial-capital").value = value.initial_capital;
  document.getElementById("backtest-leverage").value = value.leverage_multiplier ?? 1;
  document.getElementById("backtest-commission-share").value = value.commission_per_share;
  document.getElementById("backtest-minimum-commission").value = value.minimum_commission;
  document.getElementById("backtest-slippage").value = value.slippage_bps;
  document.getElementById("backtest-benchmark").value = value.benchmark;
  document.getElementById("backtest-risk-free").value = value.risk_free_rate;
  document.getElementById("backtest-fractional").checked = Boolean(value.allow_fractional_shares);
}

function settingsFromForm() {
  return {
    ...bt.current.default_settings,
    start_date: document.getElementById("backtest-start-date").value,
    end_date: document.getElementById("backtest-end-date").value,
    initial_capital: Number(document.getElementById("backtest-initial-capital").value),
    leverage_multiplier: Number(document.getElementById("backtest-leverage").value),
    commission_per_share: Number(document.getElementById("backtest-commission-share").value),
    minimum_commission: Number(document.getElementById("backtest-minimum-commission").value),
    slippage_bps: Number(document.getElementById("backtest-slippage").value),
    benchmark: document.getElementById("backtest-benchmark").value,
    risk_free_rate: Number(document.getElementById("backtest-risk-free").value),
    allow_fractional_shares: document.getElementById("backtest-fractional").checked,
    strict_data: true,
  };
}

async function runBacktest() {
  try {
    await saveBacktestStrategy({ announce: false });
    resetBacktestResult();
    btSetRunning(true);
    btSetStatus("正在排队并检查历史数据完整性...", "neutral", true);
    const payload = await btJson(await fetch(`/api/backtest/strategies/${bt.current.id}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: bt.current.default_settings }),
    }));
    bt.currentRunId = payload.run.id;
    connectBacktestEvents(payload.run.id);
  } catch (error) {
    btSetRunning(false);
    btSetStatus(btErrorText(error), "error", true);
  }
}

function connectBacktestEvents(runId) {
  if (bt.eventSource) bt.eventSource.close();
  const source = new EventSource(`/api/backtest/runs/${runId}/events`);
  bt.eventSource = source;
  source.addEventListener("update", async (event) => {
    const payload = JSON.parse(event.data);
    consumeBacktestUpdate(payload);
    const status = payload.run.status;
    const progress = Number(payload.run.progress || 0);
    if (status === "running" || status === "validating" || status === "queued") {
      btSetStatus(`回测${btRunStatusLabel(status)}：${(progress * 100).toFixed(1)}% ${payload.run.current_time || ""}`, "neutral", true);
    }
    if (["completed", "failed", "cancelled"].includes(status)) {
      source.close();
      bt.eventSource = null;
      btSetRunning(false);
      await loadBacktestRunResult(runId, status);
      const type = status === "completed"
        ? payload.run.termination_reason === "LIQUIDATED" ? "warning" : "success"
        : status === "cancelled" ? "warning" : "error";
      btSetStatus(
        status === "completed"
          ? payload.run.termination_reason === "LIQUIDATED" ? "回测已爆仓并生成结果。" : "回测完成。"
          : payload.run.error_message || btRunStatusLabel(status),
        type,
        true,
      );
      refreshBacktestRunHistory();
    }
  });
  source.onerror = () => {
    if (bt.eventSource === source && source.readyState === EventSource.CLOSED) {
      btSetStatus("实时连接已断开，可在历史运行中重新打开结果。", "warning", true);
      btSetRunning(false);
    }
  };
}

function consumeBacktestUpdate(payload) {
  if (payload.run?.settings?.initial_capital != null) {
    bt.displayInitialCapital = Number(payload.run.settings.initial_capital);
  }
  const pointKeys = new Set(bt.equityPoints.map((point) => point.sequence || point.trading_date));
  (payload.equity_points || []).forEach((point) => {
    const key = point.sequence || point.trading_date;
    if (!pointKeys.has(key)) {
      pointKeys.add(key);
      bt.equityPoints.push(point);
    }
  });
  const tradeKeys = new Set(bt.trades.map(btTradeKey));
  (payload.trades || []).forEach((trade) => {
    const key = btTradeKey(trade);
    if (!tradeKeys.has(key)) {
      tradeKeys.add(key);
      bt.trades.push(trade);
    }
  });
  const logKeys = new Set(bt.logs.map((log) => log.sequence));
  (payload.logs || []).forEach((log) => {
    if (!logKeys.has(log.sequence)) {
      logKeys.add(log.sequence);
      bt.logs.push(log);
    }
  });
  btChartEmpty.hidden = Boolean(bt.equityPoints.length);
  renderBacktestChart();
  renderBacktestLogs();
  renderBacktestTradeSummary();
}

async function loadBacktestRunResult(runId, knownStatus = "") {
  try {
    const payload = await btJson(await fetch(`/api/backtest/runs/${runId}/results`));
    bt.currentRunId = runId;
    bt.displayInitialCapital = Number(
      payload.run.settings?.initial_capital
      || bt.current?.default_settings?.initial_capital
      || 100000
    );
    bt.chartHoverIndex = null;
    bt.equityPoints = payload.equity_points || [];
    bt.trades = payload.trades || [];
    bt.logs = await loadAllBacktestLogs(runId);
    renderBacktestMetrics(payload.run.metrics);
    btChartEmpty.hidden = Boolean(bt.equityPoints.length);
    renderBacktestChart();
    renderBacktestLogs();
    renderBacktestTradeSummary(payload.run);
    if (!["completed", "failed", "cancelled"].includes(payload.run.status || knownStatus)) {
      btSetRunning(true);
      connectBacktestEvents(runId);
    }
  } catch (error) {
    btSetStatus(btErrorText(error), "error", true);
  }
}

async function loadAllBacktestLogs(runId) {
  const logs = [];
  let after = 0;
  while (true) {
    const payload = await btJson(await fetch(
      `/api/backtest/runs/${runId}/logs?level=DEBUG&limit=5000&after=${after}`
    ));
    const batch = payload.logs || [];
    logs.push(...batch);
    if (batch.length < 5000) break;
    const next = Number(batch[batch.length - 1].sequence);
    if (!Number.isFinite(next) || next <= after) {
      throw new Error("日志分页游标异常。");
    }
    after = next;
  }
  return logs;
}

function renderBacktestMetrics(metrics) {
  if (!metrics) {
    btMetrics.innerHTML = '<div class="backtest-empty">运行完成后显示关键指标</div>';
    return;
  }
  const fields = [
    ["运行结果", metrics.liquidated ? "已爆仓" : "正常完成", metrics.liquidated ? "账户权益不大于零后已强制平仓并提前结束。" : "回测运行至设置的结束日期。"],
    ["整体杠杆倍率", `${btNumber(metrics.leverage_multiplier ?? 1)}×`, "账户级杠杆倍率；各标的最终有效杠杆还会乘以策略中设置的单标的杠杆。"],
    ["期末权益", btMoney(metrics.ending_equity), "回测结束时的现金、应收款与持仓市值之和。"],
    ["总收益率", btPercent(metrics.total_return), "期末权益相对初始资金的累计变化。"],
    ["年化收益率", btPercent(metrics.annualized_return), "将区间总收益按交易日折算为一年。短区间结果可能不稳定。"],
    ["最大回撤", btPercent(metrics.max_drawdown), "权益曲线从历史高点到随后低点的最大跌幅。"],
    ["夏普率", btNumber(metrics.sharpe_ratio), "年化超额收益除以全部收益波动率。"],
    ["Sortino", btNumber(metrics.sortino_ratio), "年化超额收益除以下行波动率，只惩罚负收益波动。"],
    ["交易次数", String(metrics.trade_count ?? 0), "回测产生的实际成交笔数。"],
    ["累计手续费", btMoney(metrics.total_commission), "所有成交收取的每股手续费与最低手续费之和。"],
    ["滑点成本", btMoney(metrics.total_slippage), "成交价相对参考价的不利价差成本。"],
    ["胜率", btPercent(metrics.win_rate), "已平仓卖单中 FIFO 已实现盈亏为正的比例；不反映单笔盈亏大小。"],
    ["换手率", btPercent(metrics.turnover), "成交总额除以平均权益，反映资金周转频率和交易成本敏感度。"],
    ["超额收益", btPercent(metrics.excess_return), "策略总收益率减去比较基准总收益率。"],
  ];
  if (metrics.liquidated && metrics.liquidation?.liquidation_time) {
    fields.splice(2, 0, ["爆仓时间", metrics.liquidation.liquidation_time, "分钟级风险时钟首次检测到账户权益不大于零的时间。"]);
  }
  btMetrics.innerHTML = fields.map(([label, value, title]) => `
    <div class="backtest-metric" title="${btEscape(title)}"><span>${label}</span><strong>${value}</strong></div>
  `).join("");
}

function renderBacktestChart() {
  if (!btCanvas) return;
  const rect = btCanvas.parentElement.getBoundingClientRect();
  const width = Math.max(320, rect.width);
  const height = Math.max(260, rect.height);
  const ratio = window.devicePixelRatio || 1;
  btCanvas.width = Math.round(width * ratio);
  btCanvas.height = Math.round(height * ratio);
  const context = btCanvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  if (!bt.equityPoints.length) return;

  const styles = getComputedStyle(document.body);
  const colors = {
    grid: styles.getPropertyValue("--chart-grid").trim(),
    label: styles.getPropertyValue("--chart-label").trim(),
    strategy: styles.getPropertyValue("--accent-strong").trim(),
    benchmark: styles.getPropertyValue("--warning").trim(),
    surface: styles.getPropertyValue("--surface").trim() || "#151b26",
    text: styles.getPropertyValue("--text").trim() || "#f2f5f8",
  };
  const padding = { left: 70, right: 66, top: 26, bottom: 38 };
  const values = bt.equityPoints.flatMap((point) => [
    Number(point.equity),
    point.benchmark_equity == null ? null : Number(point.benchmark_equity),
  ]).filter((value) => Number.isFinite(value));
  let min = Math.min(...values);
  let max = Math.max(...values);
  const spread = Math.max(1, max - min);
  min -= spread * 0.08;
  max += spread * 0.08;
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const x = (index) => padding.left + (bt.equityPoints.length === 1 ? plotWidth / 2 : index / (bt.equityPoints.length - 1) * plotWidth);
  const y = (value) => padding.top + (max - value) / (max - min) * plotHeight;

  context.font = "11px system-ui";
  context.lineWidth = 1;
  context.strokeStyle = colors.grid;
  context.fillStyle = colors.label;
  for (let index = 0; index <= 4; index += 1) {
    const py = padding.top + index / 4 * plotHeight;
    const value = max - index / 4 * (max - min);
    context.beginPath();
    context.moveTo(padding.left, py);
    context.lineTo(width - padding.right, py);
    context.stroke();
    context.textAlign = "right";
    context.fillText(btCompactMoney(value), padding.left - 8, py + 4);
    const initial = bt.displayInitialCapital || 1;
    context.textAlign = "left";
    context.fillText(`${((value / initial - 1) * 100).toFixed(1)}%`, width - padding.right + 8, py + 4);
  }
  const tickCount = Math.min(5, bt.equityPoints.length);
  for (let index = 0; index < tickCount; index += 1) {
    const pointIndex = Math.round(index / Math.max(1, tickCount - 1) * (bt.equityPoints.length - 1));
    context.textAlign = "center";
    context.fillText(bt.equityPoints[pointIndex].trading_date.slice(5), x(pointIndex), height - 13);
  }

  function drawSeries(key, color) {
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.beginPath();
    let started = false;
    bt.equityPoints.forEach((point, index) => {
      const value = point[key];
      if (value == null) return;
      if (!started) {
        context.moveTo(x(index), y(Number(value)));
        started = true;
      } else {
        context.lineTo(x(index), y(Number(value)));
      }
    });
    context.stroke();
  }
  drawSeries("equity", colors.strategy);
  drawSeries("benchmark_equity", colors.benchmark);
  context.textAlign = "left";
  context.fillStyle = colors.strategy;
  context.fillText("● 策略", padding.left, 16);
  if (bt.equityPoints.some((point) => point.benchmark_equity != null)) {
    context.fillStyle = colors.benchmark;
    context.fillText("● 基准", padding.left + 58, 16);
  }

  const hoverIndex = Number(bt.chartHoverIndex);
  if (bt.chartHoverIndex !== null && Number.isInteger(hoverIndex) && hoverIndex >= 0 && hoverIndex < bt.equityPoints.length) {
    const point = bt.equityPoints[hoverIndex];
    const px = x(hoverIndex);
    const py = y(Number(point.equity));
    context.save();
    context.setLineDash([5, 4]);
    context.strokeStyle = colors.label;
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(px, padding.top);
    context.lineTo(px, height - padding.bottom);
    context.moveTo(padding.left, py);
    context.lineTo(width - padding.right, py);
    context.stroke();
    context.restore();

    const drawLabel = (text, centerX, centerY, align = "center") => {
      context.font = "11px system-ui";
      const labelWidth = context.measureText(text).width + 12;
      const labelHeight = 20;
      let left = align === "right" ? centerX - labelWidth : align === "left" ? centerX : centerX - labelWidth / 2;
      left = Math.max(2, Math.min(width - labelWidth - 2, left));
      const top = Math.max(2, Math.min(height - labelHeight - 2, centerY - labelHeight / 2));
      context.fillStyle = colors.surface;
      context.fillRect(left, top, labelWidth, labelHeight);
      context.strokeStyle = colors.grid;
      context.strokeRect(left, top, labelWidth, labelHeight);
      context.fillStyle = colors.text;
      context.textAlign = "left";
      context.fillText(text, left + 6, top + 14);
    };
    drawLabel(point.trading_date, px, height - padding.bottom / 2);
    drawLabel(btCompactMoney(Number(point.equity)), padding.left - 5, py, "right");
    drawLabel(btPercent(point.return_rate), width - padding.right + 5, py, "left");

    const positionEntries = Object.entries(point.positions || {});
    const holdings = positionEntries.length
      ? positionEntries.map(([symbol, position]) => {
        const marketValue = Number(position.market_value || 0);
        const weight = Number(point.equity) ? marketValue / Number(point.equity) : 0;
        return `${symbol} ${btPercent(weight)}`;
      }).join(" · ")
      : "空仓";
    const lines = [
      point.trading_date,
      `权益 ${btMoney(point.equity)} · 现金 ${btMoney(point.cash)}`,
      `收益率 ${btPercent(point.return_rate)}`,
      `持仓 ${holdings}`,
    ];
    context.font = "12px system-ui";
    const boxWidth = Math.min(
      width - padding.left - padding.right - 12,
      Math.max(...lines.map((line) => context.measureText(line).width)) + 20,
    );
    const boxHeight = lines.length * 20 + 12;
    let boxX = px + 12;
    if (boxX + boxWidth > width - padding.right) boxX = px - boxWidth - 12;
    boxX = Math.max(padding.left, boxX);
    const boxY = padding.top + 8;
    context.fillStyle = colors.surface;
    context.fillRect(boxX, boxY, boxWidth, boxHeight);
    context.strokeStyle = colors.grid;
    context.strokeRect(boxX, boxY, boxWidth, boxHeight);
    context.fillStyle = colors.text;
    context.textAlign = "left";
    lines.forEach((line, index) => context.fillText(line, boxX + 10, boxY + 19 + index * 20));
  }
}

function renderBacktestLogs() {
  const selected = document.getElementById("backtest-log-level").value;
  const accepted = selected === "DEBUG"
    ? ["DEBUG", "INFO", "WARN", "ERROR"]
    : selected === "INFO"
      ? ["INFO", "WARN", "ERROR"]
      : ["ERROR"];
  const logs = bt.logs.filter((log) => accepted.includes(log.level));
  btLogCount.textContent = `${logs.length} / ${bt.logs.length} 条`;
  if (!logs.length) {
    btLogOutput.innerHTML = '<div class="backtest-empty">当前级别暂无日志</div>';
    return;
  }
  btLogOutput.innerHTML = logs.map((log) => `
    <div class="backtest-log-line">
      <span class="level-${btEscape(log.level)}">${btEscape(log.level)}</span>
      <span>${btEscape(log.event_time || "-")}</span>
      <span>${btEscape(log.symbol ? `${log.symbol} · ${log.message}` : log.message)}</span>
    </div>
  `).join("");
  btLogOutput.scrollTop = btLogOutput.scrollHeight;
}

function renderBacktestTradeSummary(run = null) {
  const buys = bt.trades.filter((trade) => trade.side === "BUY").length;
  const sells = bt.trades.filter((trade) => trade.side === "SELL").length;
  const commission = bt.trades.reduce((sum, trade) => sum + Number(trade.commission || 0), 0);
  const slippage = bt.trades.reduce((sum, trade) => sum + Number(trade.slippage_amount || 0), 0);
  document.getElementById("backtest-trade-summary").textContent = bt.trades.length
    ? `成交 ${bt.trades.length} 笔（买入 ${buys} / 卖出 ${sells}），手续费 ${btMoney(commission)}，滑点成本 ${btMoney(slippage)}`
    : run?.status === "completed" ? "本次策略没有产生交易。" : "";
}

function renderBacktestRunHistory() {
  if (!bt.runs.length) {
    btRunHistory.innerHTML = '<div class="backtest-help">尚无历史运行。</div>';
    return;
  }
  btRunHistory.innerHTML = bt.runs.map((run) => `
    <div class="backtest-run-item">
      <span>#${run.id} · ${btEscape(btDateTime(run.created_at))}</span>
      <span>${btEscape(btRunOutcomeLabel(run))}${run.metrics?.total_return != null ? ` · ${btPercent(run.metrics.total_return)}` : ""}</span>
      <button type="button" data-run-id="${run.id}">查看</button>
    </div>
  `).join("");
}

async function refreshBacktestRunHistory() {
  if (!bt.current) return;
  const payload = await btJson(await fetch(`/api/backtest/strategies/${bt.current.id}`));
  bt.runs = payload.runs || [];
  renderBacktestRunHistory();
}

function btSetRunning(running) {
  document.getElementById("backtest-run").disabled = running;
  document.getElementById("backtest-save").disabled = running;
  document.getElementById("backtest-cancel").hidden = !running;
}

function btRunStatusLabel(status) {
  return ({
    queued: "排队中",
    validating: "校验中",
    running: "运行中",
    cancelling: "正在取消",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  })[status] || status || "未知";
}

function btRunOutcomeLabel(run) {
  return run?.termination_reason === "LIQUIDATED" || run?.metrics?.liquidated
    ? "已爆仓"
    : btRunStatusLabel(run?.status);
}

function btTradeKey(trade) {
  return [trade.event_time, trade.symbol, trade.side, trade.quantity, trade.fill_price].join("|");
}

function btDateTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function btCompactDate(value) {
  return String(value || "—").replaceAll("-", "");
}

function btMoney(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { style: "currency", currency: "USD", minimumFractionDigits: 2 });
}

function btCompactMoney(value) {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function btPercent(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function btNumber(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toFixed(3);
}

function showBtDialog(dialog) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeBtDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function initBacktest() {
  document.querySelector('[data-view="backtest-view"]').addEventListener("click", () => {
    if (btWorkspace.hidden) loadBacktestStrategies();
  });
  document.getElementById("backtest-new-strategy").addEventListener("click", () => showBtDialog(btCreateDialog));
  document.getElementById("backtest-results-button").addEventListener("click", () => {
    btListPage.hidden = true;
    btWorkspace.hidden = true;
    btResultsPage.hidden = false;
    loadBacktestResultsOverview(1);
  });
  document.getElementById("backtest-results-back").addEventListener("click", () => {
    btResultsPage.hidden = true;
    btListPage.hidden = false;
    loadBacktestStrategies();
  });
  document.getElementById("backtest-results-prev").addEventListener("click", () => {
    if (bt.resultsPage > 1) loadBacktestResultsOverview(bt.resultsPage - 1);
  });
  document.getElementById("backtest-results-next").addEventListener("click", () => {
    if (bt.resultsPage < bt.resultsTotalPages) loadBacktestResultsOverview(bt.resultsPage + 1);
  });
  btResultsTable.addEventListener("change", () => {
    document.getElementById("backtest-delete-runs").disabled = !btResultsTable.querySelector(".bt-result-select:checked");
  });
  btResultsTable.addEventListener("click", (event) => {
    if (event.target.closest("input")) return;
    const row = event.target.closest("[data-result-run-id]");
    if (row) openBacktestRunDetail(Number(row.dataset.resultRunId));
  });
  document.getElementById("backtest-delete-runs").addEventListener("click", async () => {
    const runIds = Array.from(btResultsTable.querySelectorAll(".bt-result-select:checked"))
      .map((item) => Number(item.value));
    if (!runIds.length) return;
    if (!window.confirm(`将不可撤销地从回测结果中删除 ${runIds.length} 条记录，并清除详细日志和每日权益/持仓节点。确认继续？`)) return;
    try {
      const result = await btJson(await fetch("/api/backtest/runs/deletions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_ids: runIds, confirm: true }),
      }));
      btResultsStatus.textContent = `已删除 ${result.run_ids.length} 条回测记录的重数据（${result.deleted_log_rows.toLocaleString()} 条日志、${result.deleted_equity_rows.toLocaleString()} 个每日节点）。`;
      btResultsStatus.className = "status success";
      await loadBacktestResultsOverview(bt.resultsPage);
    } catch (error) {
      btResultsStatus.textContent = btErrorText(error);
      btResultsStatus.className = "status error";
    }
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => closeBtDialog(document.getElementById(button.dataset.closeDialog)));
  });
  document.getElementById("backtest-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      name: document.getElementById("backtest-create-name").value.trim(),
      design_mode: "visual",
      selection_mode: document.getElementById("backtest-create-selection").value,
    };
    try {
      const payload = await btJson(await fetch("/api/backtest/strategies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }));
      closeBtDialog(btCreateDialog);
      await loadBacktestStrategies();
      await openBacktestStrategy(payload.strategy.id);
    } catch (error) {
      btSetStatus(btErrorText(error), "error");
    }
  });
  btStrategyTable.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-strategy-id]");
    if (!row) return;
    const strategyId = Number(row.dataset.strategyId);
    const action = event.target.closest("[data-bt-action]")?.dataset.btAction;
    if (action === "delete") {
      event.stopPropagation();
      if (!window.confirm("将永久删除该策略；已完成的历史运行及其策略快照仍会保留。确认删除？")) return;
      try {
        await btJson(await fetch(`/api/backtest/strategies/${strategyId}`, { method: "DELETE" }));
        await loadBacktestStrategies();
      } catch (error) {
        btSetStatus(btErrorText(error), "error");
      }
      return;
    }
    if (action === "duplicate") {
      event.stopPropagation();
      try {
        await btJson(await fetch(`/api/backtest/strategies/${strategyId}/duplicate`, { method: "POST" }));
        await loadBacktestStrategies();
      } catch (error) {
        btSetStatus(btErrorText(error), "error");
      }
      return;
    }
    openBacktestStrategy(strategyId);
  });
  document.getElementById("backtest-back").addEventListener("click", () => {
    if (bt.eventSource) {
      bt.eventSource.close();
      bt.eventSource = null;
    }
    btSetRunning(false);
    btWorkspace.hidden = true;
    setBacktestReadOnly(false);
    if (bt.returnToResults) {
      bt.returnToResults = false;
      btResultsPage.hidden = false;
      loadBacktestResultsOverview(bt.resultsPage);
    } else {
      btListPage.hidden = false;
      loadBacktestStrategies();
    }
  });
  document.getElementById("backtest-save").addEventListener("click", async () => {
    try {
      await saveBacktestStrategy();
    } catch (error) {
      btSetStatus(btErrorText(error), "error", true);
    }
  });
  document.getElementById("backtest-validate").addEventListener("click", async () => {
    try {
      await saveBacktestStrategy({ announce: false });
      const payload = await btJson(await fetch(`/api/backtest/strategies/${bt.current.id}/validate`, { method: "POST" }));
      btSetStatus(payload.message, "success", true);
    } catch (error) {
      btSetStatus(btErrorText(error), "error", true);
    }
  });
  document.getElementById("backtest-settings").addEventListener("click", () => {
    settingsToForm();
    showBtDialog(btSettingsDialog);
  });
  document.getElementById("backtest-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (bt.readOnly) return;
    bt.current.default_settings = settingsFromForm();
    try {
      await saveBacktestStrategy({ announce: false });
      closeBtDialog(btSettingsDialog);
      btSetStatus("默认运行设置已保存。", "success", true);
    } catch (error) {
      btSetStatus(btErrorText(error), "error", true);
    }
  });
  document.getElementById("backtest-run").addEventListener("click", runBacktest);
  document.getElementById("backtest-cancel").addEventListener("click", async () => {
    if (!bt.currentRunId) return;
    try {
      await btJson(await fetch(`/api/backtest/runs/${bt.currentRunId}/cancel`, { method: "POST" }));
      btSetStatus("正在取消回测...", "warning", true);
    } catch (error) {
      btSetStatus(btErrorText(error), "error", true);
    }
  });
  document.getElementById("backtest-add-symbol").addEventListener("click", () => {
    syncBacktestSymbolsFromEditor();
    bt.current.definition.symbols.push({
      symbol: "",
      max_weight: bt.current.selection_mode === "distribution" ? 10 : 100,
      leverage_multiplier: 1,
    });
    renderBacktestSymbols();
  });
  btSymbols.addEventListener("click", (event) => {
    const move = event.target.closest(".bt-move-symbol");
    if (move) {
      syncBacktestSymbolsFromEditor();
      const index = Number(move.closest(".backtest-symbol-row").dataset.index);
      const target = move.dataset.direction === "up" ? index - 1 : index + 1;
      if (target >= 0 && target < bt.current.definition.symbols.length) {
        [bt.current.definition.symbols[index], bt.current.definition.symbols[target]] =
          [bt.current.definition.symbols[target], bt.current.definition.symbols[index]];
        renderBacktestSymbols();
      }
      return;
    }
    const button = event.target.closest(".bt-remove-symbol");
    if (!button) return;
    syncBacktestSymbolsFromEditor();
    const index = Number(button.closest(".backtest-symbol-row").dataset.index);
    bt.current.definition.symbols.splice(index, 1);
    renderBacktestSymbols();
  });
  document.getElementById("backtest-reset-symbols").addEventListener("click", () => {
    bt.current.definition.symbols = structuredClone(bt.symbolDefaults);
    renderBacktestSymbols();
  });
  document.getElementById("backtest-add-rule").addEventListener("click", () => {
    bt.current.definition.rules.push({
      id: `rule-${Date.now()}`,
      name: "新规则",
      enabled: true,
      priority: (bt.current.definition.rules.length + 1) * 10,
      action: bt.current.selection_mode === "competition" ? "SELL" : "BUY",
      sizing_mode: "TARGET",
      value: 0,
      condition: "true",
      when: "OPEN",
    });
    renderBacktestRules();
  });
  btRules.addEventListener("click", (event) => {
    const button = event.target.closest(".bt-remove-rule");
    if (!button) return;
    const index = Number(button.closest(".backtest-rule").dataset.index);
    bt.current.definition.rules.splice(index, 1);
    renderBacktestRules();
  });
  btRunHistory.addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (button) loadBacktestRunResult(Number(button.dataset.runId));
  });
  document.getElementById("backtest-log-level").addEventListener("change", renderBacktestLogs);
  btCanvas.addEventListener("mousemove", (event) => {
    if (!bt.equityPoints.length) return;
    const rect = btCanvas.getBoundingClientRect();
    const padding = { left: 70, right: 66 };
    const plotWidth = Math.max(1, rect.width - padding.left - padding.right);
    const relativeX = Math.max(0, Math.min(plotWidth, event.clientX - rect.left - padding.left));
    bt.chartHoverIndex = bt.equityPoints.length === 1
      ? 0
      : Math.round(relativeX / plotWidth * (bt.equityPoints.length - 1));
    renderBacktestChart();
  });
  btCanvas.addEventListener("mouseleave", () => {
    bt.chartHoverIndex = null;
    renderBacktestChart();
  });
  document.getElementById("backtest-download-log").addEventListener("click", () => {
    const lines = bt.logs.map((log) => `[${log.level}] ${log.event_time || "-"} ${log.symbol || ""} ${log.message}`);
    const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `backtest-${bt.currentRunId || "log"}.log`;
    link.click();
    URL.revokeObjectURL(url);
  });
  document.getElementById("backtest-download-xls").addEventListener("click", () => {
    if (!bt.currentRunId) {
      btSetStatus("请先运行或选择一条历史回测。", "error");
      return;
    }
    const link = document.createElement("a");
    link.href = `/api/backtest/runs/${bt.currentRunId}/logs.xls`;
    link.download = `backtest-${bt.currentRunId}.xls`;
    link.click();
  });
  window.addEventListener("resize", renderBacktestChart);
  loadBacktestCodeCatalog();
}
