#!/usr/bin/env python3
"""Hash database identifier sets before and after a LiteLLM migration.

The report contains counts and SHA-256 digests only; it never prints identifier
values (including verification tokens). Run it against a restored production
clone before and after starting the v1.95 migration revision.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any


ID_FIELDS = (
    ("LiteLLM_VerificationToken", "token"),
    ("LiteLLM_UserTable", "user_id"),
    ("LiteLLM_TeamTable", "team_id"),
    ("LiteLLM_BudgetTable", "budget_id"),
    ("LiteLLM_ProxyModelTable", "model_id"),
    ("LiteLLM_OrganizationTable", "organization_id"),
    ("LiteLLM_ProjectTable", "project_id"),
    ("LiteLLM_EndUserTable", "end_user_id"),
    ("LiteLLM_EndUserTable", "user_id"),
    ("gateway_generation_jobs", "id"),
)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _column_exists(connection: Any, table: str, column: str) -> bool:
    return bool(
        await connection.fetchval(
            """
            select exists (
              select 1 from information_schema.columns
              where table_schema=current_schema() and table_name=$1 and column_name=$2
            )
            """,
            table,
            column,
        )
    )


async def _snapshot(database_url: str) -> dict[str, Any]:
    import asyncpg

    connection = await asyncpg.connect(database_url)
    try:
        tables: dict[str, Any] = {}
        for table, column in ID_FIELDS:
            label = f"{table}.{column}"
            if not await _column_exists(connection, table, column):
                tables[label] = {"present": False}
                continue
            query = (
                f"select {_quoted(column)}::text as value from {_quoted(table)} "
                f"order by {_quoted(column)}::text nulls first"
            )
            rows = await connection.fetch(query)
            digest = hashlib.sha256()
            null_count = 0
            for row in rows:
                value = row["value"]
                if value is None:
                    null_count += 1
                    encoded = b"<NULL>"
                else:
                    encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            tables[label] = {
                "present": True,
                "count": len(rows),
                "null_count": null_count,
                "sha256": digest.hexdigest(),
            }
        spend_tables = await connection.fetch(
            """
            select table_name
            from information_schema.tables
            where table_schema=current_schema()
              and table_type='BASE TABLE'
              and table_name ilike '%spend%'
            order by table_name
            """
        )
        spend_history: dict[str, int] = {}
        for row in spend_tables:
            table_name = str(row["table_name"])
            spend_history[table_name] = int(
                await connection.fetchval(f"select count(*) from {_quoted(table_name)}")
            )
        constraints = await connection.fetch(
            """
            select conrelid::regclass::text as table_name, conname, convalidated
            from pg_constraint
            where contype='f' and connamespace=current_schema()::regnamespace
            order by conrelid::regclass::text, conname
            """
        )
        foreign_key_names = [f"{row['table_name']}.{row['conname']}" for row in constraints]
        foreign_key_digest = hashlib.sha256()
        for name in foreign_key_names:
            encoded = name.encode("utf-8")
            foreign_key_digest.update(len(encoded).to_bytes(8, "big"))
            foreign_key_digest.update(encoded)
        return {
            "schema": await connection.fetchval("select current_schema()"),
            "tables": tables,
            "spend_history_row_counts": spend_history,
            "foreign_keys": {
                "count": len(constraints),
                "sha256": foreign_key_digest.hexdigest(),
                "unvalidated": [
                    f"{row['table_name']}.{row['conname']}" for row in constraints if not row["convalidated"]
                ],
            },
        }
    finally:
        await connection.close()


def _compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for label, expected in before.get("tables", {}).items():
        actual = after.get("tables", {}).get(label)
        if actual != expected:
            failures.append(label)
    for table_name, expected_count in before.get("spend_history_row_counts", {}).items():
        if after.get("spend_history_row_counts", {}).get(table_name) != expected_count:
            failures.append(f"spend_history_row_counts.{table_name}")
    if after.get("foreign_keys", {}).get("unvalidated"):
        failures.append("foreign_keys.unvalidated")
    return failures


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="Write the snapshot JSON to this path.")
    parser.add_argument("--compare", type=Path, help="Fail if the new snapshot differs from this file.")
    args = parser.parse_args()
    database_url = (os.environ.get("MIGRATION_AUDIT_DATABASE_URL") or "").strip()
    if not database_url:
        parser.error("MIGRATION_AUDIT_DATABASE_URL must point to the isolated database clone")
    report = await _snapshot(database_url)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    if args.compare:
        before = json.loads(args.compare.read_text())
        failures = _compare(before, report)
        if failures:
            print("Identifier integrity check failed: " + ", ".join(failures))
            return 1
        print("Identifier integrity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
