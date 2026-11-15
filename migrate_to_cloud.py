"""手元の demen.db（SQLite）の記録を、DATABASE_URL のデータベースへコピーする

使い方（.env に DATABASE_URL を書いてから）:
    python migrate_to_cloud.py              # このフォルダの demen.db を移す
    python migrate_to_cloud.py 別の場所/demen.db

同じ記録が既にあればスキップするので、何回実行しても大丈夫。
"""
import os
import sqlite3
import sys
from pathlib import Path

import db

BASE = Path(__file__).parent
env = BASE / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
if not os.environ.get("DATABASE_URL"):
    raise SystemExit(".env に DATABASE_URL を書いてね")
if not src_path.exists():
    raise SystemExit(f"{src_path} が見つからない")

src = sqlite3.connect(src_path)
src.row_factory = sqlite3.Row
dst = db.connect()

cols = {r["name"] for r in src.execute("PRAGMA table_info(attendance)")}
added = 0
rows = src.execute("SELECT * FROM attendance").fetchall()
for r in rows:
    ok = db.add(
        dst, r["guild_id"], r["date"], r["slot"], r["user_id"], r["user_name"],
        price=r["price"], created_at=r["created_at"] if "created_at" in cols else None,
    )
    added += ok

for s in src.execute("SELECT * FROM settings").fetchall():
    keys = s.keys()
    db.get_settings(dst, s["guild_id"])
    db.set_price(dst, s["guild_id"], s["price"])
    if s["panel_channel_id"]:
        db.set_auto_post(dst, s["guild_id"], s["panel_channel_id"], s["panel_time"])
    if "board_message_id" in keys and s["board_message_id"]:
        db.set_panel(dst, s["guild_id"], s["board_channel_id"], s["board_message_id"], s["board_date"])

print(f"記録 {len(rows)}件中 {added}件をコピーしたよ（残りは既にあった分）")
