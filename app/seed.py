from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from app.config import Settings, load_settings
from app.storage import create_schema


TITLES = (
    "The Distributed Library", "Database Systems", "A Study in Algorithms", "Modern Data Platforms",
    "The SQL Handbook", "Networks in Practice", "Designing Reliable Systems", "Information Theory",
    "A Quiet Reader", "Practical Python", "The Book of Indexes", "Data and Society",
    "Query Planning", "Library Architecture", "The Search Problem",
)
AUTHORS = ("Lin Mei", "Chen Rui", "Alex Morgan", "Sana Ito", "Priya Shah", "Noah Kim")
CATEGORIES = ("数据库", "计算机", "文学", "数学", "网络", "工程")
READER_NAMES = ("林晓", "陈安", "周宁", "王晨", "赵一", "许清", "李闻", "孙遥")
EXTENDED_CATEGORIES = (*CATEGORIES, "人工智能", "数据科学")
EXTENDED_TITLE_STEMS = (
    "分布式数据库优化", "云端数据管理", "人工智能案例", "网络安全实践",
    "数据科学方法", "软件工程设计", "信息检索技术", "计算机系统结构",
    "现代文学选读", "统计建模实战",
)
EXTENDED_AUTHORS = (
    "Lin Mei", "Chen Rui", "Alex Morgan", "Sana Ito", "Priya Shah", "Noah Kim",
    "Wei Zhang", "Mia Chen", "Jun Park", "Avery Liu", "Iris Wang", "Ethan Zhou",
)


def seed_all(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    for site in settings.sites:
        site.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(site.database)
        try:
            create_schema(connection, site.branch_id)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM loans")
            connection.execute("DELETE FROM books")
            connection.execute("DELETE FROM readers")
            branch = site.branch_id
            books = []
            for number, title in enumerate(TITLES, start=1):
                active = number <= 10 and number % 2 == 1
                books.append((
                    branch * 1000 + number,
                    branch,
                    title,
                    AUTHORS[(number + branch - 2) % len(AUTHORS)],
                    CATEGORIES[(number + branch - 2) % len(CATEGORIES)],
                    "loaned" if active else "available",
                ))
            readers = [
                (branch * 10000 + number, branch, READER_NAMES[(number + branch - 2) % len(READER_NAMES)])
                for number in range(1, len(READER_NAMES) + 1)
            ]
            loans = []
            for number in range(1, 11):
                return_date = None if number % 2 == 1 else f"2026-09-{number + 1:02d}"
                loans.append((
                    branch * 100000 + number,
                    branch,
                    branch * 1000 + number,
                    branch * 10000 + ((number - 1) % len(READER_NAMES)) + 1,
                    f"2026-09-{number:02d}",
                    return_date,
                ))
            connection.executemany(
                "INSERT INTO books (book_id, branch_id, title, author, category, status) VALUES (?, ?, ?, ?, ?, ?)",
                books,
            )
            connection.executemany(
                "INSERT INTO readers (reader_id, branch_id, name) VALUES (?, ?, ?)", readers
            )
            connection.executemany(
                "INSERT INTO loans (loan_id, branch_id, book_id, reader_id, loan_date, return_date) VALUES (?, ?, ?, ?, ?, ?)",
                loans,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def seed_extended_all(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    seed_all(settings)
    start_date = date(2025, 1, 1)

    for site in settings.sites:
        connection = sqlite3.connect(site.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            branch = site.branch_id
            readers = [
                (branch * 10000 + number, branch, f"{site.site_id}分馆读者{number:03d}")
                for number in range(9, 89)
            ]
            books = []
            for number in range(16, 216):
                category = EXTENDED_CATEGORIES[(number + branch - 2) % len(EXTENDED_CATEGORIES)]
                title = (
                    f"{EXTENDED_TITLE_STEMS[(number + branch - 2) % len(EXTENDED_TITLE_STEMS)]}"
                    f" · {site.site_id}馆藏 {number:03d}"
                )
                active = number <= 175 and number % 4 == 0
                books.append((
                    branch * 1000 + number,
                    branch,
                    title,
                    EXTENDED_AUTHORS[(number + branch - 2) % len(EXTENDED_AUTHORS)],
                    category,
                    "loaned" if active else "available",
                ))
            loans = []
            for book_number in range(16, 176):
                loan_date = start_date + timedelta(days=(book_number * 17 + branch * 31) % 560)
                active = book_number % 4 == 0
                return_date = None if active else (loan_date + timedelta(days=7 + book_number % 24)).isoformat()
                reader_number = 9 + ((book_number * 7 + branch) % 80)
                loans.append((
                    branch * 100000 + book_number,
                    branch,
                    branch * 1000 + book_number,
                    branch * 10000 + reader_number,
                    loan_date.isoformat(),
                    return_date,
                ))

            connection.executemany(
                "INSERT INTO readers (reader_id, branch_id, name) VALUES (?, ?, ?)", readers
            )
            connection.executemany(
                "INSERT INTO books (book_id, branch_id, title, author, category, status) VALUES (?, ?, ?, ?, ?, ?)",
                books,
            )
            connection.executemany(
                "INSERT INTO loans (loan_id, branch_id, book_id, reader_id, loan_date, return_date) VALUES (?, ?, ?, ?, ?, ?)",
                loans,
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Foreign key check failed for site {site.site_id}: {violations[:3]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


if __name__ == "__main__":
    seed_all()
    print("Initialized three branch databases with repeatable sample data.")
