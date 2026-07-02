import argparse
import asyncio
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
    parser = argparse.ArgumentParser(
        description="把 DB 内的 UTC 时间字段一次性平移到本地时间（谨慎使用）"
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=str(_default_db_path()),
        help="数据库文件路径（默认：fpbrowser2api/data/fpbrowser.db）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行迁移（不加则只打印将要修改的行数）",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}")
        return 2

    async with aiosqlite.connect(str(db_path)) as db:
        if not await _table_exists(db, "task_type_windows"):
            print("[ERROR] table task_type_windows not found")
            return 2

        # 计算本地与 UTC 的分钟差（例如中国时区一般是 480）
        row = await (
            await db.execute(
                """
                SELECT CAST((julianday('now','localtime') - julianday('now')) * 24 * 60 AS INTEGER)
                """
            )
        ).fetchone()
        offset_min = int((row or [0])[0] or 0)
        if offset_min == 0:
            print("[WARN] local_minus_utc_minutes == 0; 你可能本来就在 UTC 时区，迁移可能不需要")

        # 当前版本里：
        # - error_cooldown_until 过去由 SQLite datetime('now', ...) 写入（UTC）
        # - 切到 localtime 口径后，为避免旧值导致比较错位，推荐一次性平移它。
        cur = await db.execute(
            "SELECT COUNT(*) FROM task_type_windows WHERE error_cooldown_until IS NOT NULL"
        )
        cnt = int(((await cur.fetchone()) or [0])[0] or 0)
        print(f"local_minus_utc_minutes: {offset_min}")
        print(f"rows to update (task_type_windows.error_cooldown_until not null): {cnt}")

        if not args.apply:
            print("dry-run: 未执行更新。加 --apply 才会真正修改数据库。")
            return 0

        modifier = f"{offset_min} minutes"
        await db.execute(
            """
            UPDATE task_type_windows
            SET error_cooldown_until = datetime(error_cooldown_until, ?)
            WHERE error_cooldown_until IS NOT NULL
            """,
            (modifier,),
        )
        cur2 = await db.execute("SELECT changes()")
        changed = int(((await cur2.fetchone()) or [0])[0] or 0)
        await db.commit()
        print(f"updated rows: {changed}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
