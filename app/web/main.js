const examples = [
  {
    label: "单站裁剪 · B",
    sql: "SELECT book_id, title, author, status\nFROM books\nWHERE branch_id = 2\nORDER BY title\nLIMIT 8;"
  },
  {
    label: "三站汇总",
    sql: "SELECT branch_id, category, COUNT(*) AS books\nFROM books\nGROUP BY branch_id, category\nORDER BY branch_id, books DESC;"
  },
  {
    label: "同站点连接",
    sql: "SELECT b.title, r.name, l.loan_date\nFROM books b\nJOIN loans l ON b.book_id = l.book_id AND b.branch_id = l.branch_id\nJOIN readers r ON l.reader_id = r.reader_id AND l.branch_id = r.branch_id\nWHERE b.branch_id = 1\nORDER BY l.loan_date DESC\nLIMIT 8;"
  },
  {
    label: "跨站点连接",
    sql: "SELECT b.branch_id AS book_branch, r.branch_id AS reader_branch, b.title, r.name\nFROM books b\nJOIN readers r ON b.branch_id < r.branch_id\nWHERE b.branch_id = 1\nORDER BY reader_branch, b.title\nLIMIT 10;"
  },
  {
    label: "OR 不裁剪",
    sql: "SELECT branch_id, title, category\nFROM books\nWHERE branch_id = 2 OR category = '文学'\nORDER BY branch_id, title\nLIMIT 12;"
  },
  {
    label: "全局排序 LIMIT",
    sql: "SELECT branch_id, title\nFROM books\nORDER BY title DESC, branch_id\nLIMIT 7;"
  }
];

const sqlInput = document.querySelector("#sql-input");
const runButton = document.querySelector("#run-query");
const resultWrap = document.querySelector("#result-wrap");
const planContent = document.querySelector("#plan-content");
const queryError = document.querySelector("#query-error");
const resultMeta = document.querySelector("#result-meta");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function renderExamples() {
  const host = document.querySelector("#examples");
  host.innerHTML = examples.map((example, index) =>
    `<button class="example-button" type="button" data-example="${index}">${escapeHtml(example.label)}</button>`
  ).join("");
  host.addEventListener("click", (event) => {
    const button = event.target.closest("[data-example]");
    if (!button) return;
    const example = examples[Number(button.dataset.example)];
    sqlInput.value = example.sql;
    host.querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    queryError.textContent = "";
  });
}

document.querySelector("#main-address").textContent = `主站 · ${window.location.host}`;

function renderTable(data) {
  if (!data.columns?.length) {
    resultWrap.innerHTML = `<div class="empty-state">查询已执行，没有可显示的列。</div>`;
    return;
  }
  const head = data.columns.map((name) => `<th>${escapeHtml(name)}</th>`).join("");
  const rows = (data.rows || []).map((row) => `<tr>${row.map((value) =>
    `<td>${value === null ? '<span class="null-value">NULL</span>' : escapeHtml(value)}</td>`
  ).join("")}</tr>`).join("");
  resultWrap.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  if (!rows) resultWrap.innerHTML += `<div class="empty-state">查询成功，结果为空。</div>`;
}

function renderPlan(plan) {
  if (!plan) return;
  document.querySelector("#request-label").textContent = `request_id ${plan.request_id || "—"}`;
  const siteBlocks = (plan.site_subqueries || []).map((site) => {
    const statements = (site.statements || []).map((statement) => `
      <div class="subquery">
        <div class="subquery-label">${escapeHtml(statement.label)}</div>
        <pre>${escapeHtml(statement.sql)}</pre>
        <div class="subquery-meta">${escapeHtml(statement.status)} · ${statement.row_count ?? "—"} 行 · ${statement.duration_ms ?? "—"} ms</div>
      </div>`).join("");
    return `<article class="site-plan">
      <div class="site-plan-head"><strong>站点 ${escapeHtml(site.site_id)} · branch ${escapeHtml(site.branch_id)}</strong><span class="status-tag ${escapeHtml(site.status)}">${escapeHtml(site.status)}</span></div>
      ${statements || `<div class="subquery-meta">无子查询</div>`}
      ${site.error ? `<div class="plan-error">${escapeHtml(site.error)}</div>` : ""}
    </article>`;
  }).join("");
  const rules = (plan.optimization_rules || []).map((rule) => `
    <li><span class="rule-state ${rule.applied ? "" : "skipped"}">${rule.applied ? "✓" : "–"}</span>
    <span><strong>${escapeHtml(rule.rule)}</strong>：${escapeHtml(rule.details)}</span></li>`).join("");
  const steps = (plan.final_steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  const error = plan.error ? `<div class="plan-error">${escapeHtml(plan.error)}</div>` : "";
  const parse = plan.parse ? `<div class="parsed-sql"><span>SQL AST · ${escapeHtml(plan.parse.status)}</span><pre>${escapeHtml(plan.parse.parsed_sql)}</pre></div>` : "";
  planContent.innerHTML = `
    <aside class="plan-summary">
      <h3>查询分解</h3>
      <dl>
        <dt>解析状态</dt><dd>${escapeHtml(plan.parse?.status || "拒绝")}</dd>
        <dt>涉及表</dt><dd>${escapeHtml((plan.tables || []).join(", ") || "无")}</dd>
        <dt>候选站点</dt><dd>${escapeHtml((plan.candidate_sites || []).join(", ") || "无")}</dd>
        <dt>实际访问</dt><dd>${escapeHtml((plan.actual_sites || []).join(", ") || "无")}</dd>
        <dt>返回行数</dt><dd>${escapeHtml(plan.returned_rows ?? "—")}</dd>
        <dt>总耗时</dt><dd>${escapeHtml(plan.total_duration_ms ?? "—")} ms</dd>
      </dl>
    </aside>
    <div class="plan-detail">
      ${error}
      ${parse}
      <h3>优化规则</h3>
      <ul class="rule-list">${rules || "<li>尚无规则。</li>"}</ul>
      <h3>分站子查询与通信</h3>
      <div class="site-plans">${siteBlocks || `<div class="empty-state">本次查询不需要访问分站。</div>`}</div>
      <h3 style="margin-top:14px">最终处理</h3>
      <ol class="step-list">${steps || "<li>等待执行。</li>"}</ol>
    </div>`;
}

async function executeQuery() {
  const sql = sqlInput.value.trim();
  if (!sql) {
    queryError.textContent = "请输入 SELECT 查询。";
    return;
  }
  runButton.disabled = true;
  runButton.innerHTML = '<span aria-hidden="true">…</span> 正在执行';
  queryError.textContent = "";
  resultMeta.textContent = "请求分站中";
  try {
    const response = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql })
    });
    const data = await response.json();
    if (data.plan) renderPlan(data.plan);
    if (!response.ok) {
      queryError.textContent = data.error || "查询执行失败。";
      resultMeta.textContent = `失败 · HTTP ${response.status}`;
      return;
    }
    renderTable(data);
    resultMeta.textContent = `${data.row_count} 行 · request_id ${data.request_id}`;
  } catch (error) {
    queryError.textContent = `主站请求失败：${error.message}`;
    resultMeta.textContent = "通信失败";
  } finally {
    runButton.disabled = false;
    runButton.innerHTML = '<span aria-hidden="true">▶</span> 执行查询';
  }
}

async function checkCluster() {
  const target = document.querySelector("#cluster-health");
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    const down = (data.sites || []).filter((site) => site.status !== "ok");
    const details = (data.sites || []).map((site) => `${site.site_id}: ${site.status}`).join(" · ");
    target.innerHTML = `<span class="health-dot ${down.length ? "error" : ""}"></span><span>${escapeHtml(details || "站点状态未知")}</span>`;
  } catch {
    target.innerHTML = '<span class="health-dot error"></span><span>站点状态不可用</span>';
  }
}

function renderSearchTable(data) {
  const wrap = document.querySelector("#search-wrap");
  if (!data.columns?.length) {
    wrap.innerHTML = "";
    return;
  }
  const head = data.columns.map((name) => `<th>${escapeHtml(name)}</th>`).join("");
  const rows = (data.rows || []).map((row) => `<tr>${row.map((value) =>
    `<td>${value === null ? '<span class="null-value">NULL</span>' : escapeHtml(value)}</td>`
  ).join("")}</tr>`).join("");
  wrap.innerHTML = `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows || ""}</tbody></table></div>`;
  if (!rows) wrap.innerHTML += `<div class="empty-state">没有匹配的图书。</div>`;
}

function renderCatalog(data) {
  const totals = data.totals || {};
  const metrics = [
    ["图书", totals.books],
    ["读者", totals.readers],
    ["借阅", totals.loans],
    ["在架", totals.available],
    ["借出", totals.loaned],
    ["未归还", totals.open_loans],
  ];
  document.querySelector("#catalog-totals").innerHTML = metrics.map(([label, value]) =>
    `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")}</strong></article>`
  ).join("");
  const body = (data.sites || []).map((site) => {
    if (site.status !== "ok") {
      return `<tr><td>${escapeHtml(site.site_id)}</td><td>${escapeHtml(site.predicate)}</td><td colspan="6">${escapeHtml(site.error || "不可用")}</td></tr>`;
    }
    return `<tr>
      <td><a href="${escapeHtml(site.url)}/">${escapeHtml(site.site_id)}</a></td>
      <td>${escapeHtml(site.predicate)}</td>
      <td>${escapeHtml(site.books)}</td>
      <td>${escapeHtml(site.readers)}</td>
      <td>${escapeHtml(site.loans)}</td>
      <td>${escapeHtml(site.available)}</td>
      <td>${escapeHtml(site.loaned)}</td>
      <td>${escapeHtml(site.open_loans)}</td>
    </tr>`;
  }).join("");
  document.querySelector("#catalog-body").innerHTML = body || `<tr><td colspan="8">没有分站。</td></tr>`;
  document.querySelector("#catalog-sites").innerHTML = (data.sites || []).map((site) => {
    if (site.status !== "ok") {
      return `<article class="site-card"><strong>站点 ${escapeHtml(site.site_id)}</strong><p>目录不可用</p></article>`;
    }
    const categories = (site.categories || []).slice(0, 4).map((item) =>
      `<li><span>${escapeHtml(item.category)}</span><b>${escapeHtml(item.books)}</b></li>`
    ).join("");
    return `<article class="site-card">
      <strong>站点 ${escapeHtml(site.site_id)} · ${escapeHtml(site.predicate)}</strong>
      <ul class="category-list">${categories || "<li><span>无分类</span></li>"}</ul>
    </article>`;
  }).join("");
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/catalog");
    const data = await response.json();
    renderCatalog(data);
  } catch (error) {
    document.querySelector("#catalog-body").innerHTML = `<tr><td colspan="8">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function loadHistory() {
  const host = document.querySelector("#history");
  try {
    const response = await fetch("/api/history");
    const data = await response.json();
    const items = data.items || [];
    if (!items.length) {
      host.innerHTML = `<span class="subtle-label">执行后会保留在主站内存中</span>`;
      return;
    }
    host.innerHTML = items.slice(0, 8).map((item, index) => {
      const firstLine = String(item.sql || "").trim().split("\n")[0];
      const mark = item.status === 200 ? "" : " history-failed";
      return `<button class="example-button history-button${mark}" type="button" data-history="${index}" title="${escapeHtml(item.sql)}">${escapeHtml(firstLine)}</button>`;
    }).join("");
    host.querySelectorAll("[data-history]").forEach((button) => {
      button.addEventListener("click", () => {
        const item = items[Number(button.dataset.history)];
        if (!item) return;
        sqlInput.value = item.sql;
        queryError.textContent = "";
      });
    });
  } catch {
    host.innerHTML = `<span class="subtle-label">历史暂时不可用</span>`;
  }
}

async function searchBooks(event) {
  event.preventDefault();
  const keyword = document.querySelector("#book-query").value.trim();
  const meta = document.querySelector("#search-meta");
  const error = document.querySelector("#search-error");
  error.textContent = "";
  if (!keyword) {
    error.textContent = "请输入检索关键字。";
    return;
  }
  meta.textContent = "正在访问全部分站";
  try {
    const response = await fetch(`/api/search?q=${encodeURIComponent(keyword)}`);
    const data = await response.json();
    if (!response.ok) {
      error.textContent = data.error || "检索失败。";
      meta.textContent = `失败 · HTTP ${response.status}`;
      document.querySelector("#search-wrap").innerHTML = "";
      return;
    }
    const siteNote = (data.sites || []).map((site) => `${site.site_id} ${site.matched}`).join(" · ");
    meta.textContent = `匹配 ${data.matched} 本 · 显示 ${data.row_count} 行${data.truncated ? " · 已截断" : ""} · ${siteNote}`;
    renderSearchTable(data);
  } catch (failure) {
    error.textContent = `检索请求失败：${failure.message}`;
    meta.textContent = "通信失败";
  }
}

renderExamples();
loadCatalog();
loadHistory();
document.querySelector("#refresh-catalog").addEventListener("click", loadCatalog);
document.querySelector("#book-search").addEventListener("submit", searchBooks);
runButton.addEventListener("click", async () => {
  await executeQuery();
  loadHistory();
});
sqlInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    executeQuery().then(loadHistory);
  }
});
checkCluster();
setInterval(checkCluster, 15000);
