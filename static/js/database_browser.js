const dbState = {
  table: "",
  page: 1,
  totalPages: 1,
  pageSize: 50,
};

function setDbStatus(message, type = "neutral") {
  const status = document.getElementById("db-status");
  status.textContent = message;
  status.className = `status ${type}`;
}

async function loadTables() {
  const response = await fetch("/api/db/tables");
  const payload = await response.json();
  const select = document.getElementById("table-select");
  select.innerHTML = "";

  if (!payload.ok || payload.tables.length === 0) {
    select.innerHTML = '<option value="">暂无数据表</option>';
    renderEmptyTable("暂无可浏览的数据表");
    return;
  }

  for (const table of payload.tables) {
    const option = document.createElement("option");
    option.value = table;
    option.textContent = table;
    select.appendChild(option);
  }

  dbState.table = payload.tables[0];
  dbState.page = 1;
  await loadTablePage();
}

async function loadTablePage() {
  if (!dbState.table) {
    return;
  }
  const params = new URLSearchParams({
    page: String(dbState.page),
    page_size: String(dbState.pageSize),
  });
  const response = await fetch(`/api/db/table/${encodeURIComponent(dbState.table)}?${params}`);
  const payload = await response.json();

  if (!payload.ok) {
    renderEmptyTable(payload.error?.message || "数据库读取失败");
    return;
  }

  dbState.page = payload.page;
  dbState.totalPages = payload.total_pages;
  document.getElementById("page-input").value = String(payload.page);
  document.getElementById("page-total").textContent = `/ ${payload.total_pages}`;
  document.getElementById("table-summary").textContent =
    `${payload.table} · 共 ${payload.total_rows} 行 · 每页最多 ${payload.page_size} 行`;
  renderTable(payload.columns, payload.rows);
  updatePagerButtons();
}

async function backupDatabase() {
  const button = document.getElementById("backup-db");
  button.disabled = true;
  setDbStatus("正在备份数据库...", "neutral");

  try {
    const response = await fetch("/api/db/backup", { method: "POST" });
    const payload = await response.json();
    if (!payload.ok) {
      setDbStatus(payload.error?.message || "数据库备份失败。", "error");
      return;
    }
    setDbStatus(`备份完成：${payload.path}`, "success");
  } catch (error) {
    setDbStatus("无法连接后端，数据库备份失败。", "error");
  } finally {
    button.disabled = false;
  }
}

function renderTable(columns, rows) {
  const table = document.getElementById("data-table");
  if (!columns.length) {
    renderEmptyTable("该表没有列");
    return;
  }

  const thead = `<thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>`;
  const tbodyRows = rows.map((row) => {
    const cells = columns.map((column) => {
      const value = row[column] === null || row[column] === undefined ? "" : String(row[column]);
      return `<td title="${escapeHtml(value)}">${escapeHtml(value)}</td>`;
    });
    return `<tr>${cells.join("")}</tr>`;
  });

  const tbody = tbodyRows.length
    ? `<tbody>${tbodyRows.join("")}</tbody>`
    : `<tbody><tr><td class="empty-cell" colspan="${columns.length}">该页暂无数据</td></tr></tbody>`;

  table.innerHTML = thead + tbody;
}

function renderEmptyTable(message) {
  document.getElementById("data-table").innerHTML =
    `<tbody><tr><td class="empty-cell">${escapeHtml(message)}</td></tr></tbody>`;
  document.getElementById("table-summary").textContent = message;
}

function updatePagerButtons() {
  document.getElementById("prev-page").disabled = dbState.page <= 1;
  document.getElementById("next-page").disabled = dbState.page >= dbState.totalPages;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bindDatabaseBrowser() {
  document.getElementById("table-select").addEventListener("change", async (event) => {
    dbState.table = event.target.value;
    dbState.page = 1;
    await loadTablePage();
  });

  document.getElementById("reload-table").addEventListener("click", loadTables);
  document.getElementById("backup-db").addEventListener("click", backupDatabase);

  document.getElementById("prev-page").addEventListener("click", async () => {
    dbState.page = Math.max(1, dbState.page - 1);
    await loadTablePage();
  });

  document.getElementById("next-page").addEventListener("click", async () => {
    dbState.page = Math.min(dbState.totalPages, dbState.page + 1);
    await loadTablePage();
  });

  document.getElementById("jump-page").addEventListener("click", async () => {
    const nextPage = Number(document.getElementById("page-input").value || 1);
    dbState.page = Math.min(Math.max(1, nextPage), dbState.totalPages);
    await loadTablePage();
  });
}
