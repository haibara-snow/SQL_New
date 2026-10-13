TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "books": ("book_id", "branch_id", "title", "author", "category", "status"),
    "readers": ("reader_id", "branch_id", "name"),
    "loans": ("loan_id", "branch_id", "book_id", "reader_id", "loan_date", "return_date"),
}

TABLES = tuple(TABLE_COLUMNS)

