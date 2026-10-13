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
loadHealth();
setInterval(loadHealth, 15000);
