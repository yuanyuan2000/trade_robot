(function () {
  function esc(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function codeField(name, field, value, disabled) {
    const attrs = disabled ? " disabled" : "";
    const rowAttrs = `class="backtest-code-param${field.type === "boolean" ? " backtest-code-param-boolean" : ""}" data-param="${esc(name)}" data-strategy-param="${esc(name)}"`;
    const inputAttrs = `class="bt-code-param" data-primary-param="${esc(name)}" data-strategy-value`;
    const defaultValue = field.type === "choice"
      ? (field.options || []).find((option) => String(option.value) === String(field.default))?.label || field.default
      : field.default;
    const range = field.minimum != null || Number.isFinite(field.maximum)
      ? `范围：${field.minimum ?? "不限"} ～ ${Number.isFinite(field.maximum) ? field.maximum : "不限"}${field.unit ? ` ${field.unit}` : ""}`
      : "";
    const notes = [
      `默认：${defaultValue}${field.unit ? ` ${field.unit}` : ""}`,
      range,
      field.help,
      field.suggestion,
    ].filter(Boolean).map(esc).join(" · ");
    if (field.type === "boolean") {
      return `<label ${rowAttrs}><span>${esc(field.label || name)}</span><input ${inputAttrs} type="checkbox" ${value ? "checked" : ""}${attrs}><small>${notes}</small></label>`;
    }
    if (field.type === "choice") {
      const options = (field.options || []).map((option) => {
        const item = typeof option === "object" ? option : { value: option, label: option };
        return `<option value="${esc(item.value)}" ${String(item.value) === String(value) ? "selected" : ""}>${esc(item.label || item.value)}</option>`;
      }).join("");
      return `<label ${rowAttrs}><span>${esc(field.label || name)}</span><select ${inputAttrs}${attrs} required>${options}</select><small>${notes}</small></label>`;
    }
    const type = field.type === "time" ? "time" : field.type === "symbol" ? "text" : "number";
    return `<label ${rowAttrs}><span>${esc(field.label || name)}${field.unit ? `（${esc(field.unit)}）` : ""}</span><input ${inputAttrs} type="${type}" value="${esc(value)}" ${field.minimum != null ? `min="${field.minimum}"` : ""} ${Number.isFinite(field.maximum) ? `max="${field.maximum}"` : ""} ${field.step != null ? `step="${field.step}"` : ""} ${field.type === "symbol" ? 'maxlength="24" pattern="[A-Za-z0-9^./=_-]{1,24}"' : ""}${attrs} required><small>${notes}</small></label>`;
  }

  function codeFieldsHtml(strategy, catalog, disabled) {
    const spec = (catalog || []).find((item) => item.key === strategy.code_key);
    if (!spec) return '<div class="backtest-empty">代码策略未注册。</div>';
    const params = strategy.definition?.params || {};
    const order = spec.parameter_order || Object.keys(spec.parameter_schema || {});
    return order.filter((name) => spec.parameter_schema[name]).map((name) => {
      const original = spec.parameter_schema[name];
      const maximum = ["holdings_num", "buy_top_n"].includes(name)
        ? Math.min(original.maximum ?? Infinity, strategy.definition?.symbols?.length || 0)
        : original.maximum;
      const field = { ...original, maximum };
      const rawValue = params[name] ?? field.default;
      const value = field.value_aliases?.[rawValue] ?? rawValue;
      return codeField(name, field, value, disabled);
    }).join("");
  }

  function renderCodeParameters(container, strategy, catalog, options = {}) {
    container.innerHTML = codeFieldsHtml(strategy, catalog, Boolean(options.readOnly));
  }

  function render(container, strategy, catalog, options = {}) {
    const definition = strategy.definition || {};
    const disabled = Boolean(options.readOnly);
    const lock = disabled ? " disabled" : "";
    const isSevenStar = strategy.design_mode === "code" && strategy.code_key === "sevenstar_etf_rotation";
    const dynamicLeverage = Boolean(definition.dynamic_leverage_enabled);
    const symbols = (definition.symbols || []).map((item, index) => `<div class="backtest-symbol-row" data-strategy-symbol="${index}">
      <input class="bt-symbol-code" data-symbol-code value="${esc(item.symbol)}" aria-label="标的代码"${lock}>
      <input class="bt-symbol-weight" data-symbol-weight type="number" min="0.01" max="100" step="0.01" value="${isSevenStar ? 100 : Number(item.max_weight)}" aria-label="最大仓位"${isSevenStar ? ' disabled title="七星策略按目标数量等权，候选标的上限固定为 100%"' : lock}>
      <input class="bt-symbol-leverage" data-symbol-leverage type="number" min="1" max="10" step="0.1" value="${Number(item.leverage_multiplier ?? 1)}" aria-label="单标的杠杆倍率" title="${dynamicLeverage ? "已由 VOLAT 动态杠杆替代，关闭动态杠杆后恢复此值" : "最终有效杠杆 = 整体杠杆 × 单标的杠杆"}"${dynamicLeverage || disabled ? " disabled" : ""}>
      <div class="backtest-symbol-order">
        <button data-move-symbol="up" type="button" aria-label="上移标的"${index === 0 || disabled ? " disabled" : ""}>↑</button>
        <button data-move-symbol="down" type="button" aria-label="下移标的"${index === definition.symbols.length - 1 || disabled ? " disabled" : ""}>↓</button>
      </div>
      <button class="backtest-remove-button" type="button" data-remove-symbol aria-label="移除标的"${lock}>×</button>
    </div>`).join("");
    let editor = "";
    if (strategy.design_mode === "code") {
      const spec = (catalog || []).find((item) => item.key === strategy.code_key);
      editor = `<section class="backtest-editor-section"><h4>${esc(spec?.name || strategy.code_key)}参数</h4><p class="backtest-help">${esc(spec?.description || "")}</p><div class="backtest-stack">${codeFieldsHtml(strategy, catalog, disabled)}</div></section>`;
    } else {
      const rules = (definition.rules || []).map((rule, index) => `<div class="backtest-rule" data-strategy-rule="${index}" data-rule-id="${esc(rule.id)}">
        <input data-rule-enabled type="checkbox" ${rule.enabled ? "checked" : ""}${lock}>
        <select data-rule-action${lock}>${["BUY", "SELL", "HOLD"].map((value) => `<option ${rule.action === value ? "selected" : ""}>${value}</option>`).join("")}</select>
        <select data-rule-sizing${lock}>${["TARGET", "DELTA"].map((value) => `<option ${rule.sizing_mode === value ? "selected" : ""}>${value}</option>`).join("")}</select>
        <input data-rule-value type="number" min="0" max="100" step="0.01" value="${Number(rule.value || 0)}"${lock}>
        <input data-rule-when value="${esc(rule.when || "OPEN")}"${lock}>
        <button type="button" data-remove-rule${lock}>×</button>
        <input class="backtest-rule-condition" data-rule-condition value="${esc(rule.condition || "true")}"${lock}>
      </div>`).join("");
      const competition = definition.competition || {};
      editor = `<section class="backtest-editor-section"><h4>${strategy.selection_mode === "competition" ? "风险/退出规则（可选）" : "交易规则"}</h4><button type="button" data-add-rule${lock}>添加规则</button><div class="backtest-stack">${rules}</div></section>`;
      if (strategy.selection_mode === "competition") editor += `<section class="backtest-editor-section"><h4>竞争选标</h4><div class="backtest-settings-grid">
        <label class="backtest-field"><span>候选条件</span><input data-competition="eligibility" value="${esc(competition.eligibility || "true")}"${lock}></label>
        <label class="backtest-field"><span>评分公式</span><input data-competition="score" value="${esc(competition.score || "")}"${lock}></label>
        <label class="backtest-field"><span>候选检查时间</span><input data-competition="eligibility_when" value="${esc(competition.eligibility_when || competition.when || "OPEN")}"${lock}></label>
        <label class="backtest-field"><span>评分与选标时间</span><input data-competition="when" value="${esc(competition.when || "OPEN")}"${lock}></label>
        <label class="backtest-field"><span>最低可入选评分</span><input data-competition="minimum_score" type="number" step="any" value="${competition.minimum_score ?? ""}"${lock}></label>
        <label class="backtest-field"><span>胜出目标仓位 %</span><input data-competition="target_weight" type="number" min="0.01" max="100" value="${Number(competition.target_weight ?? 100)}"${lock}></label>
        <label class="backtest-check-field"><input data-competition="cash_when_none" type="checkbox" ${competition.cash_when_none !== false ? "checked" : ""}${lock}><span>无合格标的时空仓</span></label>
        <label class="backtest-check-field"><input data-competition="rebalance_existing" type="checkbox" ${competition.rebalance_existing !== false ? "checked" : ""}${lock}><span>目标不变时重新确认目标</span></label>
      </div></section>`;
    }
    container.innerHTML = `<section class="backtest-editor-section"><div class="backtest-section-title"><h4>标的与仓位上限</h4><button type="button" data-add-symbol${lock}>添加标的</button></div><label class="backtest-check-field"><input data-dynamic-enabled type="checkbox" ${definition.dynamic_leverage_enabled ? "checked" : ""}${lock}><span>启用动态杠杆率</span></label><p class="backtest-help">${esc({single:"single 模式必须且只能有一个标的。",distribution:"distribution 模式的最大仓位合计不能超过 100%。",competition:"competition 模式至少需要两个候选标的。"}[strategy.selection_mode] || "")}</p><div class="backtest-stack" data-symbol-list>${symbols}</div></section>${editor}`;
  }

  function collect(container, strategy, catalog) {
    const definition = structuredClone(strategy.definition || {});
    definition.dynamic_leverage_enabled = container.querySelector("[data-dynamic-enabled]").checked;
    definition.symbols = Array.from(container.querySelectorAll("[data-strategy-symbol]")).map((row) => ({
      symbol: row.querySelector("[data-symbol-code]").value.trim().toUpperCase(),
      max_weight: Number(row.querySelector("[data-symbol-weight]").value),
      leverage_multiplier: Number(row.querySelector("[data-symbol-leverage]").value),
    }));
    if (strategy.design_mode === "code") {
      const spec = (catalog || []).find((item) => item.key === strategy.code_key);
      definition.params = {};
      container.querySelectorAll("[data-strategy-param]").forEach((row) => {
        const name = row.dataset.strategyParam;
        const field = spec.parameter_schema[name];
        const input = row.querySelector("[data-strategy-value]");
        definition.params[name] = field.type === "boolean" ? input.checked : ["choice", "time"].includes(field.type) ? input.value : field.type === "symbol" ? input.value.trim().toUpperCase() : Number(input.value);
      });
    } else {
      definition.rules = Array.from(container.querySelectorAll("[data-strategy-rule]")).map((row, index) => ({
        id: row.dataset.ruleId || `rule-${Date.now()}-${index}`,
        name: `${row.querySelector("[data-rule-action]").value} if ${row.querySelector("[data-rule-condition]").value.trim() || "true"}`,
        enabled: row.querySelector("[data-rule-enabled]").checked,
        priority: (index + 1) * 10,
        action: row.querySelector("[data-rule-action]").value,
        sizing_mode: row.querySelector("[data-rule-sizing]").value,
        value: Number(row.querySelector("[data-rule-value]").value),
        when: row.querySelector("[data-rule-when]").value.trim().toUpperCase(),
        condition: row.querySelector("[data-rule-condition]").value.trim() || "true",
      }));
      if (strategy.selection_mode === "competition") {
        const get = (key) => container.querySelector(`[data-competition="${key}"]`);
        definition.competition = {
          eligibility: get("eligibility").value.trim() || "true", score: get("score").value.trim(),
          eligibility_when: get("eligibility_when").value.trim().toUpperCase(), when: get("when").value.trim().toUpperCase(),
          minimum_score: get("minimum_score").value === "" ? null : Number(get("minimum_score").value),
          target_weight: Number(get("target_weight").value), cash_when_none: get("cash_when_none").checked,
          rebalance_existing: get("rebalance_existing").checked,
        };
      }
    }
    return definition;
  }

  function bind(container, rerender, changed) {
    container.addEventListener("click", (event) => {
      const definition = changed();
      if (event.target.closest("[data-add-symbol]")) definition.symbols.push({ symbol: "SPY", max_weight: definition.symbols.length ? 50 : 100, leverage_multiplier: 1 });
      else if (event.target.closest("[data-remove-symbol]")) definition.symbols.splice(Number(event.target.closest("[data-strategy-symbol]").dataset.strategySymbol), 1);
      else if (event.target.closest("[data-move-symbol]")) {
        const button = event.target.closest("[data-move-symbol]");
        const index = Number(button.closest("[data-strategy-symbol]").dataset.strategySymbol);
        const target = button.dataset.moveSymbol === "up" ? index - 1 : index + 1;
        [definition.symbols[index], definition.symbols[target]] = [definition.symbols[target], definition.symbols[index]];
      }
      else if (event.target.closest("[data-add-rule]")) definition.rules.push({ id: `rule-${Date.now()}`, name: "新规则", enabled: true, priority: (definition.rules.length + 1) * 10, action: "BUY", sizing_mode: "TARGET", value: 100, when: "OPEN", condition: "true" });
      else if (event.target.closest("[data-remove-rule]")) definition.rules.splice(Number(event.target.closest("[data-strategy-rule]").dataset.strategyRule), 1);
      else return;
      rerender(definition);
    });
  }

  window.StrategyForm = { render, renderCodeParameters, collect, bind };
})();
