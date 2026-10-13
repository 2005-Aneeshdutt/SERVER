"""WikiSQL (Salesforce, BSD-3-Clause): turn a question about one table into a SQLite query.

Source: the official release (github.com/salesforce/WikiSQL, data.tar.bz2), dev split, because
the Hugging Face mirrors drop the tables and execution needs them. Checkable by **execution**:
each table is loaded into in-memory SQLite and the predicted query's result is compared with
the gold query's result. Text columns use COLLATE NOCASE, the same for gold and prediction.
"""

import bz2
import io
import json
import random
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import httpx

from distill.datasets import DATA, TaskExample, example_id

TASK = "wikisql-sql"
URL = "https://github.com/salesforce/WikiSQL/raw/master/data.tar.bz2"
RAW = DATA / "raw" / "wikisql"
AGG = ["", "MAX", "MIN", "COUNT", "SUM", "AVG"]
OPS = ["=", ">", "<"]
TABLE = "t"

SYSTEM = (
    "You write SQLite queries for a reporting tool. The user gives one table (its schema and a "
    "few sample rows) and a question. Reply with a single SQLite SELECT statement that answers "
    "the question, and nothing else: no explanation, no code fences."
)


def _download() -> None:
    if (RAW / "dev.jsonl").exists() and (RAW / "dev.tables.jsonl").exists():
        return
    RAW.mkdir(parents=True, exist_ok=True)
    archive = httpx.get(URL, follow_redirects=True, timeout=300).content
    with tarfile.open(fileobj=io.BytesIO(bz2.decompress(archive))) as tar:
        for name in ("dev.jsonl", "dev.tables.jsonl"):
            member = tar.extractfile(f"data/{name}")
            assert member is not None, name
            (RAW / name).write_bytes(member.read())


def _columns(header: list[str]) -> list[str]:
    """Unique, quotable column names (WikiSQL headers can repeat)."""
    out: list[str] = []
    for h in header:
        name, i = h.strip() or "col", 2
        while name in out:
            name, i = f"{h.strip()}_{i}", i + 1
        out.append(name)
    return out


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: Any, numeric: bool) -> str:
    if numeric:
        try:
            return repr(float(value)) if "." in str(value) else str(int(float(value)))
        except (TypeError, ValueError):
            pass
    return "'" + str(value).replace("'", "''") + "'"


def schema_sql(table: dict[str, Any]) -> str:
    cols = _columns(table["header"])
    defs = [
        f"{quote_ident(c)} {'REAL' if t == 'real' else 'TEXT COLLATE NOCASE'}"
        for c, t in zip(cols, table["types"], strict=True)
    ]
    return f"CREATE TABLE {TABLE} ({', '.join(defs)})"


def connect(table: dict[str, Any]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(schema_sql(table))
    placeholders = ", ".join("?" for _ in table["header"])
    rows = []
    for row in table["rows"]:
        values = []
        for value, kind in zip(row, table["types"], strict=True):
            if kind == "real":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = None
            values.append(value)
        rows.append(values)
    conn.executemany(f"INSERT INTO {TABLE} VALUES ({placeholders})", rows)
    return conn


def gold_sql(sql: dict[str, Any], table: dict[str, Any]) -> str:
    cols = _columns(table["header"])
    col = quote_ident(cols[sql["sel"]])
    agg = AGG[sql["agg"]]
    select = f"{agg}({col})" if agg else col
    conds = [
        f"{quote_ident(cols[c])} {OPS[op]} {_literal(v, table['types'][c] == 'real')}"
        for c, op, v in sql["conds"]
    ]
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    return f"SELECT {select} FROM {TABLE}{where}"


def execute(table: dict[str, Any], query: str) -> list[tuple[Any, ...]] | None:
    """Rows as a sorted list (order-insensitive comparison), or None if the query fails."""
    conn = connect(table)
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    norm = [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in rows]
    return sorted(norm, key=repr)


def user_message(table: dict[str, Any], question: str) -> str:
    cols = _columns(table["header"])
    sample = "\n".join(" | ".join(str(v) for v in row) for row in table["rows"][:3])
    return (
        f"Table: {schema_sql(table)}\n"
        f"Sample rows ({' | '.join(cols)}):\n{sample}\n"
        f"Question: {question.strip()}"
    )


def load(n: int, seed: int = 7) -> tuple[list[TaskExample], dict[str, dict[str, Any]]]:
    """Examples plus the tables they use (kept locally for execution-based scoring)."""
    _download()
    tables = {
        t["id"]: t
        for t in map(json.loads, (RAW / "dev.tables.jsonl").read_text("utf-8").splitlines())
    }
    questions = [json.loads(line) for line in (RAW / "dev.jsonl").read_text("utf-8").splitlines()]
    random.Random(seed).shuffle(questions)
    out: list[TaskExample] = []
    used: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for q in questions:
        table = tables[q["table_id"]]
        gold = gold_sql(q["sql"], table)
        if not execute(table, gold):  # drop golds that error or return nothing (uncheckable)
            continue
        text = user_message(table, q["question"])
        eid = example_id(TASK, text)
        if eid in seen:
            continue
        seen.add(eid)
        out.append(
            TaskExample(
                task=TASK,
                example_id=eid,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                gold=gold,
                meta={"table_id": q["table_id"]},
            )
        )
        used[q["table_id"]] = table
        if len(out) >= n:
            break
    return out, used


def save_tables(tables: dict[str, dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for table in tables.values():
            f.write(json.dumps(table) + "\n")
