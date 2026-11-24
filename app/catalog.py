"""分站本地统计与只读浏览。

主站的全局目录（global catalog）会并发拉取各分站的 profile，
再汇总成分片条件、行数和分类分布。浏览接口只允许预定义逻辑表。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schema import TABLE_COLUMNS
from app.storage import connect_readonly

# 各表允许用 LIKE 检索的文本列。表名本身来自白名单，不接受用户拼接。
SEARCH_COLUMNS: dict[str, tuple[str, ...]] = {
    "books": ("title", "author", "category", "status"),
    "readers": ("name",),
    "loans": ("loan_date", "return_date"),
}
PRIMARY_KEYS = {"books": "book_id", "readers": "reader_id", "loans": "loan_id"}
MAX_BROWSE_LIMIT = 40


class BrowseError(ValueError):
    pass


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def local_profile(database: Path) -> dict[str, Any]:
    """统计本分站三张表的行数、在架/借出状态和分类分布。"""
    connection = connect_readonly(database)
    try:
        def scalar(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0])

        status_counts = {
            row[0]: int(row[1])
            for row in connection.execute("SELECT status, COUNT(*) FROM books GROUP BY status")
        }
        categories = [
            {"category": row[0], "books": int(row[1])}
            for row in connection.execute(
                "SELECT category, COUNT(*) AS book_count FROM books GROUP BY category ORDER BY book_count DESC, category"
            )
        ]
        return {
            "books": scalar("SELECT COUNT(*) FROM books"),
            "readers": scalar("SELECT COUNT(*) FROM readers"),
            "loans": scalar("SELECT COUNT(*) FROM loans"),
            "available": status_counts.get("available", 0),
            "loaned": status_counts.get("loaned", 0),
            "open_loans": scalar("SELECT COUNT(*) FROM loans WHERE return_date IS NULL"),
            "categories": categories,
        }
    finally:
        connection.close()


def browse_table(
    database: Path,
    table: str,
    limit: int,
    offset: int,
    q: str,
    *,
    sort_by_title: bool = False,
) -> dict[str, Any]:
    """按白名单读取本站一张表，可选关键字过滤，供分站页面分页浏览。"""
    if table not in TABLE_COLUMNS:
        raise BrowseError(f"Unknown table: {table}")
    if sort_by_title and table != "books":
        raise BrowseError("Only books can be sorted by title")
    if limit < 1 or limit > MAX_BROWSE_LIMIT:
        raise BrowseError(f"limit must be between 1 and {MAX_BROWSE_LIMIT}")
    if offset < 0 or offset > 100000:
        raise BrowseError("offset is out of range")
    keyword = q.strip()
    if len(keyword) > 40:
        raise BrowseError("Search text exceeds 40 characters")

    columns = TABLE_COLUMNS[table]
    where = ""
    params: list[Any] = []
    if keyword:
        clauses = [f"{column} LIKE ? ESCAPE '\\'" for column in SEARCH_COLUMNS[table]]
        params.extend([_like_pattern(keyword)] * len(SEARCH_COLUMNS[table]))
        if keyword.isdigit():
            clauses.append(f"{PRIMARY_KEYS[table]} = ?")
            params.append(int(keyword))
        where = " WHERE " + " OR ".join(clauses)

    order_by = "title, book_id" if sort_by_title else PRIMARY_KEYS[table]
    connection = connect_readonly(database)
    try:
        total = int(connection.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0])
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table}{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "table": table,
            "columns": list(columns),
            "rows": [list(row) for row in rows],
            "row_count": len(rows),
            "total": total,
            "limit": limit,
            "offset": offset,
            "q": keyword,
        }
    finally:
        connection.close()
