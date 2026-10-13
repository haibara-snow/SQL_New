from __future__ import annotations

import sqlite3

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


if __name__ == "__main__":
    seed_all()
    print("Initialized three branch databases with repeatable sample data.")

