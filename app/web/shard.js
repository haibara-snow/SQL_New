const eventList = document.querySelector("#event-list");
let eventCount = 0;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function renderEvent(event) {
  if (event.event_type === "heartbeat") return;
  if (eventCount === 0) eventList.innerHTML = "";
  eventCount += 1;
  const time = event.timestamp ? new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false }) : "";
  const detailParts = [];
  if (event.query_count) detailParts.push(`${event.query_count} 条 SQL`);
  if (event.row_count !== undefined) detailParts.push(`${event.row_count} 行`);
  if (event.duration_ms !== undefined) detailParts.push(`${event.duration_ms} ms`);
  if (event.error) detailParts.push(event.error);
  if (event.statements) detailParts.push(`${event.statements.length} 条 SQL 已校验`);
  const sqls = event.statements || (event.sql ? [{ label: event.label || "SQL", sql: event.sql }] : []);
  const sqlMarkup = sqls.map((item) => `<pre class="event-sql">${escapeHtml(item.label)}
${escapeHtml(item.sql)}</pre>`).join("");
  const item = document.createElement("article");
  item.className = "event-item";
  item.innerHTML = `
    <div class="event-time">${escapeHtml(time)}<br>${escapeHtml(event.site_id || "")}</div>
    <div class="event-body">
      <div class="event-title-row">
        <span class="event-kind">${escapeHtml(event.event_type)}</span>
        <span class="event-title">${escapeHtml(event.message)}</span>
        <span class="event-request">${escapeHtml(event.request_id)}</span>
      </div>
      ${sqlMarkup}
      ${detailParts.length ? `<div class="event-detail">${escapeHtml(detailParts.join(" · "))}</div>` : ""}
    </div>`;
  eventList.prepend(item);
  while (eventList.children.length > 140) eventList.lastElementChild.remove();
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error("database unavailable");
    const data = await response.json();
    document.querySelector("#site-id").textContent = data.site_id;
    document.querySelector("#branch-id").textContent = data.branch_id;
    document.querySelector("#database-path").textContent = data.database;
    document.querySelector("#row-count").textContent = data.database_rows;
    document.querySelector("#site-title").textContent = `分站 ${data.site_id} · branch_id ${data.branch_id}`;
    const metadataResponse = await fetch("/api/metadata");
    const metadata = await metadataResponse.json();
    document.querySelector("#main-link").href = `${metadata.main_url}/`;
  } catch (error) {
    document.querySelector("#row-count").textContent = "数据库不可用";
  }
}

const stream = new EventSource("/events");
stream.onopen = () => {
  document.querySelector("#stream-status").innerHTML = '<span class="health-dot"></span>实时连接已建立';
};
stream.onmessage = (message) => {
  try { renderEvent(JSON.parse(message.data)); } catch { /* Ignore malformed stream events. */ }
};
stream.onerror = () => {
  document.querySelector("#stream-status").innerHTML = '<span class="health-dot error"></span>正在重连';
};
document.querySelector("#clear-events").addEventListener("click", () => {
  eventList.innerHTML = '<div class="empty-state">事件视图已清空，后续事件仍会实时显示。</div>';
  eventCount = 0;
});
const tables = [
  { id: "books", label: "books 图书" },
  { id: "readers", label: "readers 读者" },
  { id: "loans", label: "loans 借阅" },
];
let currentTable = "books";
let browseOffset = 0;
const browseLimit = 8;

function renderBrowse(data) {
  const wrap = document.querySelector("#browse-wrap");
  const columns = data.columns || [];
  const head = columns.map((name) => `<th>${escapeHtml(name)}</th>`).join("");
  const rows = (data.rows || []).map((row) => `<tr>${row.map((value) =>
    `<td>${value === null ? '<span class="null-value">NULL</span>' : escapeHtml(value)}</td>`
  ).join("")}</tr>`).join("");
  wrap.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  if (!rows) wrap.innerHTML += `<div class="empty-state">当前条件下没有记录。</div>`;
  const from = data.total ? data.offset + 1 : 0;
  const to = data.offset + data.row_count;
  document.querySelector("#browse-meta").textContent = `${data.table} · ${from}–${to} / ${data.total}`;
  document.querySelector("#prev-page").disabled = data.offset <= 0;
  document.querySelector("#next-page").disabled = data.offset + data.row_count >= data.total;
}

async function loadBrowse() {
  const error = document.querySelector("#browse-error");
  error.textContent = "";
  const keyword = document.querySelector("#local-query").value.trim();
  const params = new URLSearchParams({
    table: currentTable,
    limit: String(browseLimit),
    offset: String(browseOffset),
  });
  if (keyword) params.set("q", keyword);
  try {
    const response = await fetch(`/api/browse?${params.toString()}`);
    const data = await response.json();
    if (!response.ok) {
      error.textContent = data.detail || "读取失败。";
      return;
    }
    renderBrowse(data);
  } catch (failure) {
    error.textContent = `读取本站数据失败：${failure.message}`;
  }
}

function renderTableTabs() {
  document.querySelector("#table-tabs").innerHTML = tables.map((table) =>
    `<button class="example-button" type="button" data-table="${table.id}" aria-pressed="${table.id === currentTable}">${escapeHtml(table.label)}</button>`
  ).join("");
}

document.querySelector("#table-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-table]");
  if (!button) return;
  currentTable = button.dataset.table;
  browseOffset = 0;
  renderTableTabs();
  loadBrowse();
});
document.querySelector("#local-search").addEventListener("submit", (event) => {
  event.preventDefault();
  browseOffset = 0;
  loadBrowse();
});
document.querySelector("#prev-page").addEventListener("click", () => {
  browseOffset = Math.max(0, browseOffset - browseLimit);
  loadBrowse();
});
document.querySelector("#next-page").addEventListener("click", () => {
  browseOffset += browseLimit;
  loadBrowse();
});

async function loadProfile() {
  try {
    const response = await fetch("/api/profile");
    if (!response.ok) return;
    const data = await response.json();
    const metrics = [
      ["图书", data.books],
      ["读者", data.readers],
      ["借阅", data.loans],
      ["未归还", data.open_loans],
    ];
    document.querySelector("#local-metrics").innerHTML = metrics.map(([label, value]) =>
      `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`
    ).join("");
  } catch { /* 统计失败时仍保留下方表格。 */ }
}

loadHealth();
renderTableTabs();
loadProfile();
loadBrowse();
setInterval(loadHealth, 15000);
setInterval(loadProfile, 15000);
