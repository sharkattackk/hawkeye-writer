"""Tests run the SQL on SQLite, which accepts some words MySQL 8 reserves. Catch those
early: an alias like `AS empty` passes the tests but breaks in production."""
import re

from pipeline.lambda_stack import LAMBDA_DIR

# MySQL 8 reserved words that make plausible column aliases
RESERVED = {
    "empty", "rank", "groups", "window", "function", "lead", "lag", "row", "rows", "system",
    "cube", "grouping", "recursive", "member", "over", "of", "key", "keys", "order", "group",
    "range", "read", "release", "condition", "interval", "match", "option", "usage", "virtual",
    "dense_rank", "percent_rank", "row_number", "first_value", "last_value", "nth_value", "ntile",
    "cume_dist", "json_table", "lateral", "int", "integer", "float", "double", "decimal", "change",
}


def test_no_reserved_words_as_sql_aliases():
    source = (LAMBDA_DIR / "handler.py").read_text()
    aliases = re.findall(r"\bAS\s+([a-z_]+)\b", source, flags=re.IGNORECASE)
    bad = sorted({alias for alias in aliases if alias.lower() in RESERVED})
    assert not bad, f"MySQL reserved words used as aliases: {bad}"
