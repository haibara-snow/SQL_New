from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.catalog import BrowseError, browse_table, local_profile
from app.config import Settings, load_settings
from app.events import EventBroker
from app.schema import TABLES
from app.sql_engine import QueryValidationError, validate_query
from app.storage import connect_readonly


class InternalQuery(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    sql: str = Field(min_length=1, max_length=12000)


class InternalQueryRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    queries: list[InternalQuery] = Field(min_length=1, max_length=3)


class QueryDeadlineExceeded(RuntimeError):
    pass


def _run_sqlite_query(database: Path, sql: str, timeout_seconds: float, max_rows: int) -> dict[str, Any]:
    started = time.perf_counter()
    connection = connect_readonly(database)
    deadline = time.monotonic() + timeout_seconds
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        cursor = connection.execute(sql)
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise HTTPException(
                status_code=413,
                detail=f"Query exceeds the configured {max_rows} row limit; no rows were returned.",
            )
        if time.monotonic() > deadline:
            raise QueryDeadlineExceeded
        return {
            "columns": columns,
            "rows": [list(row) for row in rows],
            "row_count": len(rows),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).casefold() or time.monotonic() > deadline:
            raise QueryDeadlineExceeded from exc
        raise
    finally:
        connection.close()


def create_shard_app(
    settings: Settings | None = None,
    site_id: str | None = None,
    database_path: Path | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    selected_id = site_id or os.environ.get("SITE_ID", "A")
    site = settings.site(selected_id)
    database = (database_path or site.database).resolve()
    broker = EventBroker(site.site_id)
    app = FastAPI(title=f"Library database site {site.site_id}")
    app.mount("/static", StaticFiles(directory=settings.root / "app" / "web"), name="static")
    app.state.settings = settings
    app.state.site = site
    app.state.database = database
    app.state.events = broker

    @app.get("/")
    async def shard_home():
        return FileResponse(settings.root / "app" / "web" / "shard.html")

    @app.get("/api/health")
    async def health():
        try:
            connection = connect_readonly(database)
            count = sum(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES)
            connection.close()
            return {
                "status": "ok",
                "site_id": site.site_id,
                "branch_id": site.branch_id,
                "database": str(database),
                "database_rows": count,
            }
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    @app.get("/api/metadata")
    async def metadata():
        return {
            "site_id": site.site_id,
            "branch_id": site.branch_id,
            "database": str(database),
            "main_url": settings.main_url,
        }

    @app.get("/api/profile")
    async def profile():
        try:
            stats = await asyncio.to_thread(local_profile, database)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc
        return {
            "site_id": site.site_id,
            "branch_id": site.branch_id,
            "predicate": f"branch_id = {site.branch_id}",
            **stats,
        }

    @app.get("/api/browse")
    async def browse(table: str, limit: int = 12, offset: int = 0, q: str = ""):
        try:
            return await asyncio.to_thread(browse_table, database, table, limit, offset, q)
        except BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    @app.get("/events")
    async def events():
        async def generate():
            async for event in broker.stream():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/internal/query")
    async def internal_query(request: InternalQueryRequest):
        request_id = request.request_id
        await broker.publish(
            request_id,
            "request_received",
            "收到主站请求",
            query_count=len(request.queries),
        )
        accepted: list[tuple[InternalQuery, str]] = []
        try:
            for item in request.queries:
                validated = validate_query(item.sql)
                accepted.append((item, validated.sql))
        except QueryValidationError as exc:
            await broker.publish(request_id, "request_rejected", "拒绝不支持的 SQL", error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await broker.publish(
            request_id,
            "sql_accepted",
            "接受 SQL 查询",
            statements=[{"label": item.label, "sql": sql} for item, sql in accepted],
        )
        results: dict[str, Any] = {}
        try:
            for item, sql in accepted:
                await broker.publish(
                    request_id,
                    "query_started",
                    "开始 SQLite 查询",
                    label=item.label,
                    sql=sql,
                )
                started = time.perf_counter()
                try:
                    result = await asyncio.to_thread(
                        _run_sqlite_query,
                        database,
                        sql,
                        settings.query_timeout_seconds,
                        settings.max_rows_per_query,
                    )
                except QueryDeadlineExceeded as exc:
                    await broker.publish(
                        request_id,
                        "query_failed",
                        "SQLite 查询超时",
                        label=item.label,
                        sql=sql,
                    )
                    raise HTTPException(status_code=504, detail="SQLite query timed out") from exc
                except HTTPException as exc:
                    await broker.publish(
                        request_id,
                        "query_failed",
                        "SQLite 查询失败",
                        label=item.label,
                        error=str(exc.detail),
                    )
                    raise
                except Exception as exc:
                    await broker.publish(
                        request_id,
                        "query_failed",
                        "SQLite 查询失败",
                        label=item.label,
                        error=str(exc),
                    )
                    raise HTTPException(status_code=400, detail=f"SQLite query failed: {exc}") from exc
                result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                results[item.label] = result
                await broker.publish(
                    request_id,
                    "query_completed",
                    f"SQLite 执行完成，返回 {result['row_count']} 行",
                    label=item.label,
                    row_count=result["row_count"],
                    duration_ms=result["duration_ms"],
                )
            await broker.publish(request_id, "response_sending", "发送查询响应", labels=list(results))
            await broker.publish(request_id, "response_sent", "响应已发送给主站", labels=list(results))
            return {"request_id": request_id, "site_id": site.site_id, "results": results}
        except HTTPException:
            raise

    return app


app = create_shard_app()
