from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import Site


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Database is not initialized: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def create_schema(connection: sqlite3.Connection, branch_id: int) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS books (
            book_id INTEGER PRIMARY KEY,
            branch_id INTEGER NOT NULL CHECK (branch_id = {branch_id}),
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('available', 'loaned')),
            UNIQUE (book_id, branch_id)
        );
        CREATE TABLE IF NOT EXISTS readers (
            reader_id INTEGER PRIMARY KEY,
            branch_id INTEGER NOT NULL CHECK (branch_id = {branch_id}),
            name TEXT NOT NULL,
            UNIQUE (reader_id, branch_id)
        );
        CREATE TABLE IF NOT EXISTS loans (
            loan_id INTEGER PRIMARY KEY,
            branch_id INTEGER NOT NULL CHECK (branch_id = {branch_id}),
            book_id INTEGER NOT NULL,
            reader_id INTEGER NOT NULL,
            loan_date TEXT NOT NULL,
            return_date TEXT,
            FOREIGN KEY (book_id, branch_id) REFERENCES books (book_id, branch_id),
            FOREIGN KEY (reader_id, branch_id) REFERENCES readers (reader_id, branch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_books_branch_status ON books (branch_id, status);
        CREATE INDEX IF NOT EXISTS idx_readers_branch ON readers (branch_id);
        CREATE INDEX IF NOT EXISTS idx_loans_branch_book ON loans (branch_id, book_id);
        """
    )


def initialize_database(site: Site) -> None:
    site.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(site.database)
    try:
        create_schema(connection, site.branch_id)
        connection.commit()
    finally:
        connection.close()

