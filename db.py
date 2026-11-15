"""出面表のデータ保存まわり（BOTとWebで共通）

環境変数 DATABASE_URL があれば Postgres（Supabase など）、なければこのフォルダの SQLite を使う。
BOT（VPS）と Web（Vercel）で同じ記録を見るには、両方に同じ DATABASE_URL を設定する。
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("demen.db")

# ゲリラの時間枠 (キー: 表示名)
SLOTS = {
    "2100": "21:00",
    "2130": "21:30",
    "2200": "22:00",
    "2230": "22:30",
    "2300": "23:00",
    "2330": "23:30",
}
DEFAULT_PRICE = 240

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS attendance (
        guild_id   BIGINT  NOT NULL,
        date       TEXT    NOT NULL,  -- YYYY-MM-DD (日本時間)
        slot       TEXT    NOT NULL,  -- 2100 など
        user_id    BIGINT  NOT NULL,
        user_name  TEXT    NOT NULL,
        price      INTEGER NOT NULL,  -- 記録した時点の単価
        created_at TEXT,
        PRIMARY KEY (guild_id, date, slot, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        guild_id          BIGINT PRIMARY KEY,
        price             INTEGER NOT NULL DEFAULT 240,
        panel_channel_id  BIGINT,
        panel_time        TEXT,     -- HH:MM 自動投稿時刻
        last_posted_date  TEXT,
        board_channel_id  BIGINT,   -- 常設パネル
        board_message_id  BIGINT,
        board_date        TEXT,
        board_dirty       INTEGER NOT NULL DEFAULT 0  -- Webで変更があったら1
    )
    """,
]
# 古いDBに後から足した列
EXTRA_COLUMNS = {
    "attendance": [("created_at", "TEXT")],
    "settings": [
        ("board_channel_id", "BIGINT"),
        ("board_message_id", "BIGINT"),
        ("board_date", "TEXT"),
        ("board_dirty", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


class DB:
    """SQLite / Postgres を同じ書き方で使うための薄いラッパー。切断されたら自動で繋ぎ直す"""

    def __init__(self, url=None, path=DB_PATH):
        self.url = url
        self.path = path
        self.is_pg = bool(url)
        self._c = None
        self._init_schema()

    def _raw(self):
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            if self._c is None or self._c.closed or self._c.broken:
                # prepare_threshold=None: Supabase の接続プーラー対策
                self._c = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
        elif self._c is None:
            # Web（FastAPI）は別スレッドから呼ぶので、同一スレッド制限を外す
            self._c = sqlite3.connect(self.path, check_same_thread=False)
            self._c.row_factory = sqlite3.Row
        return self._c

    def execute(self, sql, params=()):
        if self.is_pg:
            import psycopg

            sql = sql.replace("?", "%s")
            try:
                return self._raw().execute(sql, params)
            except psycopg.OperationalError:
                self._c = None  # 切れていたら1回だけ繋ぎ直して再実行
                return self._raw().execute(sql, params)
        return self._raw().execute(sql, params)

    def commit(self):
        if not self.is_pg:
            self._raw().commit()

    def close(self):
        if self._c is not None:
            self._c.close()
            self._c = None

    def _init_schema(self):
        for stmt in SCHEMA:
            self.execute(stmt)
        for table, cols in EXTRA_COLUMNS.items():
            if self.is_pg:
                for col, typ in cols:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
            else:
                have = {r["name"] for r in self.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols:
                    if col not in have:
                        self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.commit()


def connect(path=DB_PATH):
    return DB(url=os.environ.get("DATABASE_URL") or None, path=path)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 設定 ----------
def get_settings(conn, guild_id):
    row = conn.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO settings (guild_id, price) VALUES (?, ?) ON CONFLICT (guild_id) DO NOTHING",
            (guild_id, DEFAULT_PRICE),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row


def set_price(conn, guild_id, price):
    get_settings(conn, guild_id)
    conn.execute("UPDATE settings SET price=? WHERE guild_id=?", (price, guild_id))
    conn.commit()


def set_auto_post(conn, guild_id, channel_id, time_str):
    get_settings(conn, guild_id)
    conn.execute(
        "UPDATE settings SET panel_channel_id=?, panel_time=? WHERE guild_id=?",
        (channel_id, time_str, guild_id),
    )
    conn.commit()


def mark_posted(conn, guild_id, date):
    conn.execute("UPDATE settings SET last_posted_date=? WHERE guild_id=?", (date, guild_id))
    conn.commit()


def auto_post_targets(conn):
    return conn.execute(
        "SELECT * FROM settings WHERE panel_channel_id IS NOT NULL AND panel_time IS NOT NULL"
    ).fetchall()


# ---------- 常設パネル ----------
def set_panel(conn, guild_id, channel_id, message_id, date):
    get_settings(conn, guild_id)
    conn.execute(
        "UPDATE settings SET board_channel_id=?, board_message_id=?, board_date=?, board_dirty=0 WHERE guild_id=?",
        (channel_id, message_id, date, guild_id),
    )
    conn.commit()


def set_panel_date(conn, guild_id, date):
    """パネルを書き換えた → 日付を記録して変更フラグを下ろす"""
    conn.execute("UPDATE settings SET board_date=?, board_dirty=0 WHERE guild_id=?", (date, guild_id))
    conn.commit()


def mark_panel_dirty(conn, guild_id):
    """Webで記録が変わった → BOTにパネルを描き直してもらう"""
    conn.execute("UPDATE settings SET board_dirty=1 WHERE guild_id=?", (guild_id,))
    conn.commit()


def panel_targets(conn):
    return conn.execute("SELECT * FROM settings WHERE board_message_id IS NOT NULL").fetchall()


# ---------- 出席 ----------
def is_attending(conn, guild_id, date, slot, user_id):
    return conn.execute(
        "SELECT 1 FROM attendance WHERE guild_id=? AND date=? AND slot=? AND user_id=?",
        (guild_id, date, slot, user_id),
    ).fetchone() is not None


def add(conn, guild_id, date, slot, user_id, user_name, price=None, created_at=None):
    """追加。すでにあれば False"""
    if is_attending(conn, guild_id, date, slot, user_id):
        return False
    if price is None:
        price = get_settings(conn, guild_id)["price"]
    conn.execute(
        "INSERT INTO attendance (guild_id, date, slot, user_id, user_name, price, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (guild_id, date, slot, user_id, user_name, price, created_at or _now()),
    )
    conn.commit()
    return True


def remove(conn, guild_id, date, slot, user_id):
    """削除。なければ False"""
    cur = conn.execute(
        "DELETE FROM attendance WHERE guild_id=? AND date=? AND slot=? AND user_id=?",
        (guild_id, date, slot, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def toggle(conn, guild_id, date, slot, user_id, user_name):
    """押すたびに 参加⇔取消。参加になったら True"""
    if remove(conn, guild_id, date, slot, user_id):
        return False
    add(conn, guild_id, date, slot, user_id, user_name)
    return True


def day_board(conn, guild_id, date):
    """{slot: [(user_id, user_name), ...]}"""
    board = {s: [] for s in SLOTS}
    for r in conn.execute(
        "SELECT slot, user_id, user_name FROM attendance WHERE guild_id=? AND date=?"
        " ORDER BY created_at, user_name",
        (guild_id, date),
    ).fetchall():
        board.setdefault(r["slot"], []).append((r["user_id"], r["user_name"]))
    return board


def month_summary(conn, guild_id, month):
    """month: YYYY-MM → [{user_id, user_name, count, amount}] 回数の多い順"""
    rows = conn.execute(
        """
        SELECT a.user_id AS user_id,
               (SELECT a2.user_name FROM attendance a2
                 WHERE a2.guild_id=a.guild_id AND a2.user_id=a.user_id
                 ORDER BY a2.date DESC LIMIT 1) AS user_name,
               COUNT(*) AS count, SUM(a.price) AS amount
          FROM attendance a
         WHERE a.guild_id=? AND a.date LIKE ?
         GROUP BY a.guild_id, a.user_id
         ORDER BY count DESC, amount DESC
        """,
        (guild_id, f"{month}-%"),
    ).fetchall()
    return [{**dict(r), "count": int(r["count"]), "amount": int(r["amount"])} for r in rows]


def month_slot_counts(conn, guild_id, month):
    """枠ごとの延べ参加数"""
    counts = {s: 0 for s in SLOTS}
    for r in conn.execute(
        "SELECT slot, COUNT(*) AS c FROM attendance WHERE guild_id=? AND date LIKE ? GROUP BY slot",
        (guild_id, f"{month}-%"),
    ).fetchall():
        counts[r["slot"]] = int(r["c"])
    return counts


def user_month_detail(conn, guild_id, user_id, month):
    """[(date, slot, price), ...]"""
    return conn.execute(
        "SELECT date, slot, price FROM attendance WHERE guild_id=? AND user_id=? AND date LIKE ? ORDER BY date, slot",
        (guild_id, user_id, f"{month}-%"),
    ).fetchall()


def month_rows(conn, guild_id, month):
    return conn.execute(
        "SELECT date, slot, user_id, user_name, price FROM attendance WHERE guild_id=? AND date LIKE ? ORDER BY date, slot, user_name",
        (guild_id, f"{month}-%"),
    ).fetchall()
