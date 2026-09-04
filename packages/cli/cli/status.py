from __future__ import annotations

import argparse
import sqlite3

from datetime import UTC, datetime
from pathlib import Path

from core.domain.policy import MAX_TOTAL_ATTEMPTS

from cli.store.outcomes import state_path_for_output


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="fetch-status",
        description="Inspect durable outcomes and breaker state for one fetch run.",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--minutes",
        type=int,
        help="also show outcomes finished in the last N minutes",
    )
    args = parser.parse_args(argv)
    if args.minutes is not None and args.minutes < 1:
        parser.error("--minutes must be >= 1")
    path = state_path_for_output(args.output)
    if not path.exists():
        parser.error(f"state database not found at {path}")

    _inspect(path, args.minutes)


def _inspect(path: Path, minutes: int | None) -> None:

    with sqlite3.connect(path) as connection:
        outcome_rows = connection.execute(
            """
            SELECT status, count(*)
              FROM outcomes
             GROUP BY status
             ORDER BY status
            """
        ).fetchall()
        failed = connection.execute(
            """
            SELECT
              sum(case when attempt_count < ? then 1 else 0 end),
              sum(case when attempt_count >= ? then 1 else 0 end)
              FROM outcomes
             WHERE status = 'failed'
            """,
            (MAX_TOTAL_ATTEMPTS, MAX_TOTAL_ATTEMPTS),
        ).fetchone()
        providers = _provider_rows(connection)
        breakers = _breaker_rows(connection)
        runs = _run_rows(connection)
        recent = _recent_rows(connection, minutes) if minutes else []

    retryable, terminal = failed or (0, 0)
    print(f"State database: {path}")
    print("Outcomes:")
    for status, count in outcome_rows:
        print(f"  {status}: {count}")
    print(f"Failed and retryable: {retryable or 0}")
    print(f"Failed and terminal: {terminal or 0}")

    if providers:
        print("Outcomes by recorded provider:")
        for provider, status, count in providers:
            print(f"  {provider:12} {status:10} {count:6}")

    if breakers:
        print("Breaker state:")
        for source, provider, consecutive, level, open_until, updated_at in breakers:
            active = bool(open_until) and datetime.fromisoformat(
                open_until
            ) > datetime.now(UTC)
            state = "open" if active else "closed"
            suffix = f" until {open_until}" if active else ""
            print(
                f"  {source}:{provider} {state}{suffix} "
                f"(failures={consecutive}, level={level}, updated={updated_at})"
            )

    if runs:
        print("Recorded runs:")
        for run_id, host, sites, run_providers, started_at, finished_at in runs:
            state = "finished" if finished_at else "interrupted or still running"
            print(
                f"  {run_id} on {host} ({sites}; {run_providers}) "
                f"started {started_at}, {state}"
            )

    if minutes is not None:
        print(f"Recent outcomes (last {minutes} minutes):")
        for provider, status, count in recent:
            print(f"  {provider:12} {status:10} {count:6}")


def _provider_rows(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outcomes)")}
    if "provider" not in columns:
        return [
            ("unknown", str(status), int(count))
            for status, count in connection.execute(
                "SELECT status, count(*) FROM outcomes GROUP BY status ORDER BY status"
            )
        ]

    return [
        (str(provider), str(status), int(count))
        for provider, status, count in connection.execute(
            """
            SELECT coalesce(nullif(provider, ''), 'unknown'), status, count(*)
              FROM outcomes
             GROUP BY provider, status
             ORDER BY provider, status
            """
        )
    ]


def _breaker_rows(
    connection: sqlite3.Connection,
) -> list[tuple[str, str, int, int, str, str]]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "breaker_states" not in tables:
        return []

    return [
        (
            str(source),
            str(provider),
            int(consecutive),
            int(level),
            str(open_until),
            str(updated_at),
        )
        for source, provider, consecutive, level, open_until, updated_at in connection.execute(
            """
            SELECT source, provider, consecutive_failures, level, open_until, updated_at
              FROM breaker_states
             ORDER BY source, provider
            """
        )
    ]


def _run_rows(
    connection: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str, str]]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "runs" not in tables:
        return []

    return [
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
        )
        for row in connection.execute(
            """
            SELECT run_id, host, sites, providers, started_at, finished_at
              FROM runs
             ORDER BY started_at DESC
            """
        )
    ]


def _recent_rows(
    connection: sqlite3.Connection,
    minutes: int,
) -> list[tuple[str, str, int]]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outcomes)")}
    provider = (
        "coalesce(nullif(provider, ''), 'unknown')"
        if "provider" in columns
        else "'unknown'"
    )
    return [
        (str(name), str(status), int(count))
        for name, status, count in connection.execute(
            f"""
            SELECT {provider}, status, count(*)
              FROM outcomes
             WHERE julianday(finished_at) > julianday('now', ?)
             GROUP BY provider, status
             ORDER BY provider, status
            """,
            (f"-{minutes} minutes",),
        )
    ]


if __name__ == "__main__":
    main()
