"""出面表BOT のデータ保存まわり (SQLite)"""
import sqlite3
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


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            guild_id   INTEGER NOT NULL,
            date       TEXT    NOT NULL,  -- YYYY-MM-DD (日本時間)
            slot       TEXT    NOT NULL,  -- 2100 など
            user_id    INTEGER NOT NULL,
            user_name  TEXT    NOT NULL,
            price      INTEGER NOT NULL,  -- 記録した時点の単価
            PRIMARY KEY (guild_id, date, slot, user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            guild_id          INTEGER PRIMARY KEY,
            price             INTEGER NOT NULL DEFAULT 240,
            panel_channel_id  INTEGER,
            panel_time        TEXT,     -- HH:MM 自動投稿時刻
            last_posted_date  TEXT
        );
        """
    )
    # 常設パネル用の列（古いDBにも後から追加）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
    for col, typ in (("board_channel_id", "INTEGER"), ("board_message_id", "INTEGER"), ("board_date", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


# ---------- 常設パネル ----------
def set_panel(conn, guild_id, channel_id, message_id, date):
    get_settings(conn, guild_id)
    conn.execute(
        "UPDATE settings SET board_channel_id=?, board_message_id=?, board_date=? WHERE guild_id=?",
        (channel_id, message_id, date, guild_id),
    )
    conn.commit()


def set_panel_date(conn, guild_id, date):
    conn.execute("UPDATE settings SET board_date=? WHERE guild_id=?", (date, guild_id))
    conn.commit()


def panel_targets(conn):
    return conn.execute("SELECT * FROM settings WHERE board_message_id IS NOT NULL").fetchall()


# ---------- 設定 ----------
def get_settings(conn, guild_id):
    row = conn.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO settings (guild_id, price) VALUES (?, ?)", (guild_id, DEFAULT_PRICE))
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


# ---------- 出席 ----------
def is_attending(conn, guild_id, date, slot, user_id):
    return conn.execute(
        "SELECT 1 FROM attendance WHERE guild_id=? AND date=? AND slot=? AND user_id=?",
        (guild_id, date, slot, user_id),
    ).fetchone() is not None


def add(conn, guild_id, date, slot, user_id, user_name):
    """追加。すでにあれば False"""
    if is_attending(conn, guild_id, date, slot, user_id):
        return False
    price = get_settings(conn, guild_id)["price"]
    conn.execute(
        "INSERT INTO attendance VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, date, slot, user_id, user_name, price),
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
        "SELECT slot, user_id, user_name FROM attendance WHERE guild_id=? AND date=? ORDER BY rowid",
        (guild_id, date),
    ):
        board.setdefault(r["slot"], []).append((r["user_id"], r["user_name"]))
    return board


def month_summary(conn, guild_id, month):
    """month: YYYY-MM → [{user_id, user_name, count, amount}] 回数の多い順"""
    rows = conn.execute(
        """
        SELECT user_id,
               (SELECT a2.user_name FROM attendance a2
                 WHERE a2.guild_id=a.guild_id AND a2.user_id=a.user_id
                 ORDER BY a2.date DESC LIMIT 1) AS user_name,
               COUNT(*) AS count, SUM(price) AS amount
          FROM attendance a
         WHERE guild_id=? AND date LIKE ?
         GROUP BY user_id
         ORDER BY count DESC, amount DESC
        """,
        (guild_id, f"{month}-%"),
    ).fetchall()
    return [dict(r) for r in rows]


def month_slot_counts(conn, guild_id, month):
    """枠ごとの延べ参加数"""
    counts = {s: 0 for s in SLOTS}
    for r in conn.execute(
        "SELECT slot, COUNT(*) AS c FROM attendance WHERE guild_id=? AND date LIKE ? GROUP BY slot",
        (guild_id, f"{month}-%"),
    ):
        counts[r["slot"]] = r["c"]
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
