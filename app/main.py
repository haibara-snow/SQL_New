from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings, load_settings
from app.schema import TABLE_COLUMNS, TABLES
from app.sql_engine import QueryValidationError, build_table_query, plan_sites, validate_query


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=12000)


class SiteRequestError(RuntimeError):
    def __init__(self, site_id: str, message: str):
        super().__init__(message)
        self.site_id = site_id


class FinalQueryTimeout(TimeoutError):
    pass


class FinalResultLimitError(ValueError):
    pass


def _memory_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE books (
            book_id INTEGER, branch_id INTEGER, title TEXT, author TEXT, category TEXT, status TEXT,
            UNIQUE (book_id, branch_id)
        );
        CREATE TABLE readers (
            reader_id INTEGER, branch_id INTEGER, name TEXT,
            UNIQUE (reader_id, branch_id)
        );
        CREATE TABLE loans (
            loan_id INTEGER, branch_id INTEGER, book_id INTEGER, reader_id INTEGER, loan_date TEXT, return_date TEXT
        );
        """
    )
    return connection


def _run_final_query(
    query_sql: str,
    fetched: dict[str, list[list[Any]]],
    max_rows: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    connection = _memory_connection()
    try:
        for table in TABLES:
            rows = fetched.get(table, [])
            if not rows:
                continue
            columns = TABLE_COLUMNS[table]
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                rows,
            )
        deadline = time.monotonic() + timeout_seconds
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            cursor = connection.execute(query_sql)
            columns = [item[0] for item in (cursor.description or [])]
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).casefold() or time.monotonic() > deadline:
                raise FinalQueryTimeout from exc
            raise
        if time.monotonic() > deadline:
            raise FinalQueryTimeout
        if len(rows) > max_rows:
            raise FinalResultLimitError(f"Final result exceeds the configured {max_rows} row limit")
        return {"columns": columns, "rows": [list(row) for row in rows], "row_count": len(rows)}
    finally:
        connection.close()


async def execute_query(
    raw_sql: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], int]:
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    plan: dict[str, Any] = {
        "request_id": request_id,
        "raw_sql": raw_sql,
        "tables": [],
        "candidate_sites": [site.site_id for site in settings.sites],
        "actual_sites": [],
        "optimization_rules": [],
        "site_subqueries": [],
        "final_steps": [],
    }
    try:
        query = validate_query(raw_sql)
    except QueryValidationError as exc:
        plan["error"] = str(exc)
        plan["final_steps"].append("SQL 在解析或白名单校验阶段被拒绝，没有访问分站。")
        plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"request_id": request_id, "error": str(exc), "plan": plan}, 400

    candidate_sites, _, direct_site_id = plan_sites(query, settings)
    plan["tables"] = query.tables
    plan["parse"] = {"dialect": "sqlite", "status": "accepted", "parsed_sql": query.sql}
    plan["candidate_sites"] = candidate_sites
    branch_pushdown = any(domain is not None for domain in query.table_domains.values())
    plan["optimization_rules"] = [
        {"rule": "sqlglot 语法树解析与只读白名单", "applied": True, "details": "仅允许预定义逻辑表、列和受支持的 SELECT 子句。"},
        {"rule": "branch_id 水平分片目录", "applied": True, "details": "branch_id 与站点映射来自配置文件。"},
        {
            "rule": "branch_id 分片裁剪",
            "applied": branch_pushdown,
            "details": (
                f"分片键约束裁剪到站点 {direct_site_id}。"
                if direct_site_id
                else "对能够证明安全的表子查询下推 branch_id 条件；其他表和站点仍保留。"
                if branch_pushdown
                else "没有可安全推导的 branch_id 条件，保留全部候选站点。"
            ),
        },
        {
            "rule": "同站点连接下推",
            "applied": query.colocated and direct_site_id is not None,
            "details": "只有连接条件保证各表 branch_id 相等且查询已裁剪到单站点时才下推完整连接。",
        },
        {
            "rule": "全局操作语义",
            "applied": direct_site_id is None,
            "details": "多站点查询先取基础表数据，再在主站内存 SQLite 执行完整 SQL。",
        },
        {
            "rule": "投影下推",
            "applied": False,
            "details": "保守获取基础表全列，以保留原查询中的筛选、连接和排序语义。",
        },
    ]

    requests: list[tuple[Any, list[dict[str, str]]]] = []
    if direct_site_id:
        site = settings.site(direct_site_id)
        statements = [{"label": "final_query", "sql": query.sql}]
        requests.append((site, statements))
        mode = "site_sql"
    else:
        mode = "gather_tables"
        for site in settings.sites:
            statements = []
            for alias, table in query.aliases.items():
                domain = query.table_domains[alias]
                if domain is not None and site.branch_id not in domain:
                    continue
                statements.append({"label": table, "sql": build_table_query(table, domain)})
            if statements:
                requests.append((site, statements))

    plan["actual_sites"] = [site.site_id for site, _ in requests]
    for site, statements in requests:
        plan["site_subqueries"].append({
            "site_id": site.site_id,
            "branch_id": site.branch_id,
            "mode": mode,
            "url": site.url,
            "status": "pending",
            "statements": [
                {"label": item["label"], "sql": item["sql"], "status": "pending", "row_count": None, "duration_ms": None}
                for item in statements
            ],
        })

    async def send(site, statements):
        payload = {"request_id": request_id, "queries": statements}
        try:
            response = await client.post(f"{site.url}/internal/query", json=payload)
            if response.status_code != 200:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                raise SiteRequestError(site.site_id, f"HTTP {response.status_code}: {detail}")
            return site.site_id, response.json()
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise SiteRequestError(site.site_id, f"站点通信失败: {exc}") from exc

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        outcomes = await asyncio.gather(
            *(send(site, statements) for site, statements in requests),
            return_exceptions=True,
        )

    remote_results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    by_site_plan = {item["site_id"]: item for item in plan["site_subqueries"]}
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            site_id = getattr(outcome, "site_id", "unknown")
            failures.append(f"站点 {site_id}: {outcome}")
            if site_id in by_site_plan:
                by_site_plan[site_id]["status"] = "error"
                by_site_plan[site_id]["error"] = str(outcome)
                for statement in by_site_plan[site_id]["statements"]:
                    statement["status"] = "error"
            continue
        site_id, response_data = outcome
        remote_results[site_id] = response_data.get("results", {})
        site_plan = by_site_plan[site_id]
        site_plan["status"] = "complete"
        for statement in site_plan["statements"]:
            result = remote_results[site_id].get(statement["label"], {})
            statement["status"] = "complete"
            statement["row_count"] = result.get("row_count")
            statement["duration_ms"] = result.get("duration_ms")
            statement["columns"] = result.get("columns", [])

    if failures:
        error = "分布式查询未完成；没有返回部分结果。" + " ".join(failures)
        plan["error"] = error
        plan["final_steps"].append("至少一个目标站点失败，主站拒绝将部分数据作为完整结果返回。")
        plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"request_id": request_id, "error": error, "plan": plan}, 502

    try:
        if direct_site_id:
            site_result = remote_results[direct_site_id]["final_query"]
            final_result = {
                "columns": site_result["columns"],
                "rows": site_result["rows"],
                "row_count": site_result["row_count"],
            }
            plan["final_steps"].append(f"完整 SQL 已在站点 {direct_site_id} 的 SQLite 执行。")
        else:
            fetched = {table: [] for table in TABLES}
            for results in remote_results.values():
                for table, result in results.items():
                    fetched.setdefault(table, []).extend(result["rows"])
            plan["final_steps"].append("从所有命中分片收集受控基础表子查询结果。")
            plan["final_steps"].append("将分片行装入主站临时内存 SQLite 数据库。")
            final_result = _run_final_query(
                query.sql,
                fetched,
                settings.max_rows_per_query,
                settings.query_timeout_seconds,
            )
            plan["final_steps"].append("在内存 SQLite 执行原始逻辑 SQL，统一处理 JOIN、DISTINCT、聚合、ORDER BY 和 LIMIT。")
    except FinalQueryTimeout:
        error = f"主站内存 SQLite 查询超过 {settings.query_timeout_seconds:g} 秒，未返回结果。"
        plan["error"] = error
        plan["final_steps"].append(error)
        plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"request_id": request_id, "error": error, "plan": plan}, 504
    except FinalResultLimitError as exc:
        error = str(exc)
        plan["error"] = error
        plan["final_steps"].append(error)
        plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"request_id": request_id, "error": error, "plan": plan}, 413
    except Exception as exc:
        error = f"最终结果处理失败: {exc}"
        plan["error"] = error
        plan["final_steps"].append(error)
        plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"request_id": request_id, "error": error, "plan": plan}, 400

    plan["returned_rows"] = final_result["row_count"]
    plan["total_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    plan["final_steps"].append(f"合并完成，返回 {final_result['row_count']} 行。")
    return {
        "request_id": request_id,
        **final_result,
        "plan": plan,
    }, 200


def create_main_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Distributed Library SQL Gateway")
    app.state.settings = settings
    app.state.transport = transport
    app.mount("/static", StaticFiles(directory=settings.root / "app" / "web"), name="static")

    @app.get("/")
    async def main_home():
        return FileResponse(settings.root / "app" / "web" / "index.html")

    app.state.history = deque(maxlen=20)

    @app.get("/api/distribution")
    async def distribution():
        return {
            "logical_tables": TABLE_COLUMNS,
            "partition_key": "branch_id",
            "main_url": settings.main_url,
            "sites": [
                {"site_id": site.site_id, "branch_id": site.branch_id, "url": site.url, "database": str(site.database)}
                for site in settings.sites
            ],
        }

    async def _get_site_json(site, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, transport=transport) as client:
                response = await client.get(f"{site.url}{path}", params=params)
            if response.status_code != 200:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                return {"site_id": site.site_id, "status": "error", "error": f"HTTP {response.status_code}: {detail}"}
            return {"site_id": site.site_id, "status": "ok", "data": response.json()}
        except Exception as exc:
            return {"site_id": site.site_id, "status": "error", "error": str(exc)}

    @app.get("/api/catalog")
    async def catalog():
        # 全局目录：分片条件来自配置，行数来自各分站实时统计。单个分站失败时仍返回其余站点。
        fetched = await asyncio.gather(*(_get_site_json(site, "/api/profile") for site in settings.sites))
        by_id = {item["site_id"]: item for item in fetched}
        sites_out = []
        totals = {"books": 0, "readers": 0, "loans": 0, "available": 0, "loaned": 0, "open_loans": 0}
        for site in settings.sites:
            item = by_id[site.site_id]
            entry = {
                "site_id": site.site_id,
                "branch_id": site.branch_id,
                "url": site.url,
                "predicate": f"branch_id = {site.branch_id}",
                "status": item["status"],
            }
            if item["status"] != "ok":
                entry["error"] = item.get("error", "站点不可用")
                sites_out.append(entry)
                continue
            data = item["data"]
            entry.update({
                "books": data.get("books", 0),
                "readers": data.get("readers", 0),
                "loans": data.get("loans", 0),
                "available": data.get("available", 0),
                "loaned": data.get("loaned", 0),
                "open_loans": data.get("open_loans", 0),
                "categories": data.get("categories", []),
            })
            for key in totals:
                totals[key] += int(entry[key])
            sites_out.append(entry)
        return {
            "partition_key": "branch_id",
            "partition_kind": "horizontal",
            "logical_tables": TABLE_COLUMNS,
            "complete": all(item["status"] == "ok" for item in sites_out),
            "totals": totals,
            "sites": sites_out,
        }

    @app.get("/api/search")
    async def search(q: str, limit: int = 30):
        keyword = q.strip()
        if not keyword:
            return JSONResponse({"error": "请输入书名、作者或分类关键字。"}, status_code=400)
        if len(keyword) > 40:
            return JSONResponse({"error": "检索关键字不能超过 40 个字符。"}, status_code=400)
        page_size = min(max(limit, 1), 40)
        fetched = await asyncio.gather(*(
            _get_site_json(site, "/api/browse", {"table": "books", "limit": page_size, "q": keyword})
            for site in settings.sites
        ))
        failures = [f"站点 {item['site_id']}: {item.get('error', '不可用')}" for item in fetched if item["status"] != "ok"]
        if failures:
            return JSONResponse(
                {"error": "跨站检索未完成，没有返回部分结果。" + " ".join(failures)},
                status_code=502,
            )
        columns = ["site_id", *TABLE_COLUMNS["books"]]
        merged: list[list[Any]] = []
        matched = 0
        per_site = []
        for site, item in zip(settings.sites, fetched):
            data = item["data"]
            matched += int(data.get("total", 0))
            per_site.append({"site_id": site.site_id, "branch_id": site.branch_id, "matched": data.get("total", 0)})
            for row in data.get("rows", []):
                merged.append([site.site_id, *row])
        title_at = columns.index("title")
        branch_at = columns.index("branch_id")
        book_at = columns.index("book_id")
        merged.sort(key=lambda row: (row[branch_at], str(row[title_at]), row[book_at]))
        visible = merged[:page_size]
        return {
            "q": keyword,
            "columns": columns,
            "rows": visible,
            "row_count": len(visible),
            "matched": matched,
            "truncated": matched > len(visible),
            "sites": per_site,
        }

    @app.get("/api/history")
    async def history():
        return {"items": list(app.state.history)}

    @app.get("/api/health")
    async def health():
        async def check(site):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.request_timeout_seconds,
                    transport=transport,
                ) as client:
                    response = await client.get(f"{site.url}/api/health")
                if response.status_code != 200:
                    return {"site_id": site.site_id, "status": "error", "http_status": response.status_code}
                data = response.json()
                return {"site_id": site.site_id, "status": "ok", "branch_id": data.get("branch_id")}
            except Exception as exc:
                return {"site_id": site.site_id, "status": "error", "error": str(exc)}

        return {"status": "ok", "sites": await asyncio.gather(*(check(site) for site in settings.sites))}

    @app.post("/api/query")
    async def query(request: QueryRequest):
        body, status = await execute_query(request.sql, settings, transport)
        plan = body.get("plan") or {}
        app.state.history.appendleft({
            "sql": request.sql,
            "status": status,
            "row_count": body.get("row_count"),
            "sites": plan.get("actual_sites", []),
            "duration_ms": plan.get("total_duration_ms"),
            "error": body.get("error"),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        return JSONResponse(body, status_code=status)

    return app


app = create_main_app()
