from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings, Site
from app.main import create_main_app, execute_query
from app.seed import READER_NAMES, TITLES, seed_all, seed_extended_all
from app.shard import create_shard_app


def make_settings(root: Path, unavailable: str | None = None) -> Settings:
    sites = []
    for index, site_id in enumerate(("A", "B", "C"), start=1):
        url = f"http://site-{site_id.lower()}:{8000 + index}"
        if site_id == unavailable:
            url = "http://missing-site:8999"
        sites.append(Site(
            site_id=site_id,
            branch_id=index,
            url=url,
            host="127.0.0.1",
            port=8000 + index,
            database=root / "data" / f"site_{site_id.lower()}.sqlite3",
        ))
    return Settings(
        main_host="127.0.0.1",
        main_port=8000,
        request_timeout_seconds=1,
        query_timeout_seconds=2,
        max_rows_per_query=10000,
        sites=tuple(sites),
        root=Path(__file__).resolve().parents[1],
    )


class AppRouterTransport(httpx.AsyncBaseTransport):
    def __init__(self, settings: Settings):
        self.apps = {
            site.url: create_shard_app(settings, site.site_id, site.database)
            for site in settings.sites
            if "missing-site" not in site.url
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        app = self.apps.get(base)
        if app is None:
            return httpx.Response(503, json={"detail": "simulated site outage"}, request=request)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as client:
            content = await request.aread()
            response = await client.request(
                request.method,
                f"{request.url.path}?{request.url.query.decode()}" if request.url.query else request.url.path,
                content=content,
                headers={key: value for key, value in request.headers.items() if key.lower() != "host"},
            )
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=await response.aread(),
                request=request,
            )


def setup(tmp_path: Path, unavailable: str | None = None):
    settings = make_settings(tmp_path, unavailable)
    seed_all(settings)
    return settings, AppRouterTransport(settings)


def test_extended_seed_rebuilds_consistent_data_for_each_site(tmp_path):
    settings = make_settings(tmp_path)
    seed_extended_all(settings)
    seed_extended_all(settings)

    for site in settings.sites:
        with TestClient(create_shard_app(settings, site.site_id, site.database)) as client:
            health = client.get("/api/health").json()
            assert health["database_rows"] == 473
        connection = sqlite3.connect(site.database)
        try:
            assert connection.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 215
            assert connection.execute("SELECT COUNT(*) FROM readers").fetchone()[0] == 88
            assert connection.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 170
            assert connection.execute("SELECT COUNT(*) FROM books WHERE status = 'loaned'").fetchone()[0] == 45
            assert connection.execute("SELECT COUNT(*) FROM loans WHERE return_date IS NULL").fetchone()[0] == 45
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            connection.close()


def run_query(settings: Settings, transport: AppRouterTransport, sql: str):
    return asyncio.run(execute_query(sql, settings, transport))


def test_branch_id_two_only_visits_site_b(tmp_path):
    settings, transport = setup(tmp_path)
    body, status = run_query(settings, transport, "SELECT book_id, branch_id FROM books WHERE branch_id = 2")
    assert status == 200
    assert body["plan"]["actual_sites"] == ["B"]
    assert body["row_count"] == 15
    assert {row[1] for row in body["rows"]} == {2}


def test_unsharded_scan_visits_all_sites_and_returns_all_books(tmp_path):
    settings, transport = setup(tmp_path)
    body, status = run_query(settings, transport, "SELECT branch_id, book_id FROM books ORDER BY book_id")
    assert status == 200
    assert body["plan"]["actual_sites"] == ["A", "B", "C"]
    assert body["row_count"] == 45
    assert {row[0] for row in body["rows"]} == {1, 2, 3}


def test_or_branch_predicate_does_not_prune_sites(tmp_path):
    settings, transport = setup(tmp_path)
    body, status = run_query(
        settings,
        transport,
        "SELECT branch_id, category FROM books WHERE branch_id = 2 OR category = '文学'",
    )
    assert status == 200
    assert body["plan"]["actual_sites"] == ["A", "B", "C"]
    assert {row[0] for row in body["rows"]} == {1, 2, 3}


def test_colocated_join_executes_at_one_site_and_returns_correct_rows(tmp_path):
    settings, transport = setup(tmp_path)
    sql = """SELECT b.title, r.name, l.loan_date
        FROM books b
        JOIN loans l ON b.book_id = l.book_id AND b.branch_id = l.branch_id
        JOIN readers r ON l.reader_id = r.reader_id AND l.branch_id = r.branch_id
        WHERE b.branch_id = 2 ORDER BY l.loan_date"""
    body, status = run_query(settings, transport, sql)
    assert status == 200
    assert body["plan"]["actual_sites"] == ["B"]
    assert body["row_count"] == 10
    expected = [
        (TITLES[number - 1], READER_NAMES[number % len(READER_NAMES)], f"2026-09-{number:02d}")
        for number in range(1, 11)
    ]
    assert [tuple(row) for row in body["rows"]] == expected


def test_cross_site_join_gathers_tables_and_merges_correctly(tmp_path):
    settings, transport = setup(tmp_path)
    sql = """SELECT b.branch_id, r.branch_id, b.title, r.name
        FROM books b JOIN readers r ON b.branch_id < r.branch_id
        WHERE b.branch_id = 1 ORDER BY r.branch_id, b.book_id, r.reader_id"""
    body, status = run_query(settings, transport, sql)
    assert status == 200
    assert body["plan"]["actual_sites"] == ["A", "B", "C"]
    assert body["plan"]["site_subqueries"][0]["mode"] == "gather_tables"
    assert body["row_count"] == 240
    assert {row[0] for row in body["rows"]} == {1}
    assert {row[1] for row in body["rows"]} == {2, 3}


def test_uncolocated_join_does_not_prune_on_intersecting_branch_domains(tmp_path):
    settings, transport = setup(tmp_path)
    sql = """SELECT b.branch_id, r.branch_id
        FROM books b JOIN readers r ON b.branch_id <> r.branch_id
        WHERE b.branch_id IN (1, 2) AND r.branch_id = 1
        ORDER BY b.branch_id, r.reader_id"""
    body, status = run_query(settings, transport, sql)
    assert status == 200
    assert body["plan"]["actual_sites"] == ["A", "B"]
    assert body["plan"]["site_subqueries"][0]["mode"] == "gather_tables"
    assert body["row_count"] == 120
    assert {row[0] for row in body["rows"]} == {2}
    assert {row[1] for row in body["rows"]} == {1}


def test_global_order_by_and_limit_are_applied_after_gather(tmp_path):
    settings, transport = setup(tmp_path)
    body, status = run_query(
        settings,
        transport,
        "SELECT branch_id, title FROM books ORDER BY title DESC, branch_id ASC LIMIT 7",
    )
    assert status == 200
    assert body["plan"]["actual_sites"] == ["A", "B", "C"]
    all_rows = [(branch, title) for branch in (1, 2, 3) for title in (
        "The Distributed Library", "Database Systems", "A Study in Algorithms", "Modern Data Platforms",
        "The SQL Handbook", "Networks in Practice", "Designing Reliable Systems", "Information Theory",
        "A Quiet Reader", "Practical Python", "The Book of Indexes", "Data and Society",
        "Query Planning", "Library Architecture", "The Search Problem",
    )]
    expected = sorted(all_rows, key=lambda row: row[1], reverse=True)[:7]
    assert [tuple(row) for row in body["rows"]] == expected


def test_distinct_aggregates_having_and_projection_aliases(tmp_path):
    settings, transport = setup(tmp_path)
    body, status = run_query(
        settings,
        transport,
        "SELECT branch_id, COUNT(*) AS total FROM books GROUP BY branch_id HAVING total >= 15 ORDER BY total DESC, branch_id",
    )
    assert status == 200
    assert body["rows"] == [[1, 15], [2, 15], [3, 15]]
    distinct, distinct_status = run_query(
        settings,
        transport,
        "SELECT DISTINCT category FROM books WHERE title LIKE '%Library%' OR book_id IN (1002, 2002) ORDER BY category",
    )
    assert distinct_status == 200
    assert len(distinct["rows"]) == 4


def test_unsupported_and_mutating_sql_are_rejected(tmp_path):
    settings, transport = setup(tmp_path)
    for sql in (
        "DELETE FROM books",
        "SELECT * FROM books; SELECT * FROM readers",
        "SELECT * FROM (SELECT * FROM books) AS nested",
        "SELECT * FROM books LIMIT 2 OFFSET 1",
        "SELECT * FROM books, readers",
        "SELECT mystery(title) FROM books",
    ):
        body, status = run_query(settings, transport, sql)
        assert status == 400
        assert body["error"]
        assert body["plan"]["actual_sites"] == []


def test_site_failure_never_returns_partial_results(tmp_path):
    settings, transport = setup(tmp_path, unavailable="C")
    body, status = run_query(settings, transport, "SELECT book_id FROM books")
    assert status == 502
    assert "站点 C" in body["error"]
    assert "rows" not in body
    assert body["plan"]["actual_sites"] == ["A", "B", "C"]
    assert body["plan"]["site_subqueries"][-1]["status"] == "error"


def test_catalog_search_browse_and_history(tmp_path):
    settings, transport = setup(tmp_path)
    site = settings.sites[0]
    with TestClient(create_shard_app(settings, site.site_id, site.database)) as shard_client:
        profile = shard_client.get("/api/profile").json()
        assert profile["books"] == 15
        assert profile["readers"] == 8
        assert profile["loans"] == 10
        assert profile["loaned"] == 5
        assert profile["predicate"] == "branch_id = 1"
        browse = shard_client.get("/api/browse", params={"table": "books", "q": "Library"}).json()
        assert browse["total"] == 2
        assert {row[1] for row in browse["rows"]} == {site.branch_id}
        assert [row[browse["columns"].index("title")] for row in browse["rows"]] == [
            "The Distributed Library", "Library Architecture",
        ]
        assert shard_client.get("/api/browse", params={"table": "secrets"}).status_code == 400
    with TestClient(create_main_app(settings, transport)) as main_client:
        catalog = main_client.get("/api/catalog").json()
        assert catalog["complete"] is True
        assert catalog["totals"]["books"] == 45
        assert [item["site_id"] for item in catalog["sites"]] == ["A", "B", "C"]
        found = main_client.get("/api/search", params={"q": "Library"})
        assert found.status_code == 200
        body = found.json()
        assert body["matched"] == 6
        assert {row[0] for row in body["rows"]} == {"A", "B", "C"}
        assert main_client.get("/api/search", params={"q": "  "}).status_code == 400
        queried = main_client.post("/api/query", json={"sql": "SELECT COUNT(*) AS total FROM readers"})
        assert queried.status_code == 200
        history = main_client.get("/api/history").json()["items"]
        assert history[0]["sql"].startswith("SELECT COUNT")
        assert history[0]["status"] == 200
        assert history[0]["sites"] == ["A", "B", "C"]


def test_search_sorts_matches_before_applying_the_limit(tmp_path):
    settings, transport = setup(tmp_path)
    with TestClient(create_main_app(settings, transport)) as client:
        full_response = client.get("/api/search", params={"q": "Library"})
        limited_response = client.get("/api/search", params={"q": "Library", "limit": 1})
        assert full_response.status_code == limited_response.status_code == 200
        full = full_response.json()
        limited = limited_response.json()
        assert limited["rows"] == full["rows"][:1]
        assert limited["rows"][0][limited["columns"].index("title")] == "Library Architecture"
        assert limited["matched"] == full["matched"] == 6
        assert limited["truncated"] is True


def test_search_fails_closed_when_a_site_is_down(tmp_path):
    settings, transport = setup(tmp_path, unavailable="C")
    with TestClient(create_main_app(settings, transport)) as client:
        response = client.get("/api/search", params={"q": "Library"})
        assert response.status_code == 502
        assert "站点 C" in response.json()["error"]
        catalog = client.get("/api/catalog").json()
        assert catalog["complete"] is False
        assert catalog["sites"][-1]["status"] == "error"


def test_main_and_all_shard_http_endpoints_work(tmp_path):
    settings, transport = setup(tmp_path)
    with TestClient(create_main_app(settings, transport)) as main_client:
        assert main_client.get("/").status_code == 200
        assert main_client.get("/static/app.css").status_code == 200
        assert main_client.get("/api/health").json()["sites"] == [
            {"site_id": "A", "status": "ok", "branch_id": 1},
            {"site_id": "B", "status": "ok", "branch_id": 2},
            {"site_id": "C", "status": "ok", "branch_id": 3},
        ]
        result = main_client.post("/api/query", json={"sql": "SELECT COUNT(*) AS total FROM books"})
        assert result.status_code == 200
        assert result.json()["rows"] == [[45]]
    for site in settings.sites:
        shard = create_shard_app(settings, site.site_id, site.database)
        with TestClient(shard) as shard_client:
            health = shard_client.get("/api/health").json()
            assert health["site_id"] == site.site_id
            assert health["database_rows"] == 33
            assert shard_client.get("/").status_code == 200
            query = shard_client.post("/internal/query", json={
                "request_id": "http-smoke",
                "queries": [{"label": "smoke", "sql": "SELECT branch_id FROM books LIMIT 1"}],
            })
            assert query.status_code == 200
            assert query.json()["results"]["smoke"]["rows"] == [[site.branch_id]]
