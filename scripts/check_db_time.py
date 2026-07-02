import argparse
import asyncio
import datetime as _dt
import time as _time
from pathlib import Path

import aiosqlite


def _default_db_path() -> Path:
    base_dir = Path(__file__).resolve().parents[1]  # fpbrowser2api/
    return base_dir / "data" / "fpbrowser.db"


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    row = await cur.fetchone()
    return row is not None


async def main() -> int:
    parser = argparse.ArgumentParser(description="对照检查 SQLite 时间（UTC vs 本地时区）")
    parser.add_argument(
        "--db",
        dest="db_path",
        default=str(_default_db_path()),
        help="数据库文件路径（默认：fpbrowser2api/data/fpbrowser.db）",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}")
        return 2

    now_local = _dt.datetime.now().astimezone()
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    print("python")
    print("  local:", now_local.isoformat(sep=" ", timespec="seconds"))
    print("  utc  :", now_utc.isoformat(sep=" ", timespec="seconds"))
    print("  tzname:", _time.tzname)
    print("  utcoffset:", now_local.utcoffset())
    print("  epoch:", int(_time.time()))

    async with aiosqlite.connect(str(db_path)) as db:
        row = await (
            await db.execute(
                """
                SELECT
                  CURRENT_TIMESTAMP                                AS current_timestamp,
                  datetime('now')                                  AS dt_now_utc,
                  datetime('now','localtime')                      AS dt_now_local,
                  datetime('now','+5 minutes')                     AS dt_utc_plus_5m,
                  datetime('now','localtime','+5 minutes')         AS dt_local_plus_5m,
                  CAST((julianday('now','localtime') - julianday('now')) * 24 * 60 AS INTEGER)
                                                               AS local_minus_utc_minutes
                """
            )
        ).fetchone()

        print("\nsqlite")
        cols = [
            "current_timestamp",
            "dt_now_utc",
            "dt_now_local",
            "dt_utc_plus_5m",
            "dt_local_plus_5m",
            "local_minus_utc_minutes",
        ]
        for k, v in zip(cols, row):
            print(f"  {k}: {v}")

        if await _table_exists(db, "task_type_windows"):
            rows = await (
                await db.execute(
                    """
                    SELECT
                      id,
                      cooldown_until,
                      error_cooldown_until,
                      typeof(cooldown_until) AS cooldown_type,
                      updated_at
                    FROM task_type_windows
                    WHERE cooldown_until IS NOT NULL OR error_cooldown_until IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 5
                    """
                )
            ).fetchall()
            print("\nrecent task_type_windows cooldown rows (top 5)")
            if not rows:
                print("  (none)")
            else:
                for r in rows:
                    print(" ", r)
        else:
            print("\n[WARN] table task_type_windows not found; skip cooldown rows check")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
