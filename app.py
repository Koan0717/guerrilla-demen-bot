"""出面表 Web 版（Vercel で動かす FastAPI アプリ）

Discord でログイン → 対象サーバーのメンバーだけが使える。
記録は BOT と同じデータベース（DATABASE_URL）に読み書きする。
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import db

BASE = Path(__file__).parent


def _load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

GUILD_ID = int(os.environ.get("GUILD_ID") or 0)
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or "dev-only-secret").encode()
SESSION_DAYS = 30
DAY_START_HOUR = 5  # BOTと同じ: 朝5時までは前日扱い
JST = timezone(timedelta(hours=9))
DISCORD_API = "https://discord.com/api/v10"
SECURE_COOKIE = BASE_URL.startswith("https://")
if SECURE_COOKIE and not os.environ.get("SESSION_SECRET"):
    raise RuntimeError("本番では SESSION_SECRET（長いランダム文字列）を設定してね")

app = FastAPI(title="ゲリラ出面表")

_conn = None


def get_db():
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


def today():
    return (datetime.now(JST) - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def valid_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (TypeError, ValueError):
        return False


def valid_month(s):
    try:
        return datetime.strptime(s, "%Y-%m").strftime("%Y-%m") == s
    except (TypeError, ValueError):
        return False


# ---------- セッション（署名付きクッキー） ----------
def _sign(data: bytes) -> str:
    return hmac.new(SESSION_SECRET, data, hashlib.sha256).hexdigest()


def make_cookie(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return f"{raw}.{_sign(raw.encode())}"


def read_cookie(value: str | None):
    if not value or "." not in value:
        return None
    raw, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(raw.encode())):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def current_user(request: Request):
    user = read_cookie(request.cookies.get("session"))
    if not user:
        raise HTTPException(401, "ログインしてね")
    return user


# ---------- Discord ログイン ----------
@app.get("/login")
def login():
    if not CLIENT_ID:
        return Response("DISCORD_CLIENT_ID が設定されていません", status_code=500)
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/callback",
        "response_type": "code",
        "scope": "identify guilds.members.read",
        "state": state,
        "prompt": "none",
    }
    res = RedirectResponse(f"https://discord.com/oauth2/authorize?{urlencode(params)}")
    res.set_cookie("oauth_state", state, max_age=600, httponly=True, secure=SECURE_COOKIE, samesite="lax")
    return res


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?error=cancel")
    if not state or state != request.cookies.get("oauth_state"):
        return RedirectResponse("/?error=state")

    with httpx.Client(timeout=10) as http:
        tok = http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{BASE_URL}/callback",
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
        if tok.status_code != 200:
            return RedirectResponse("/?error=token")
        headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        me = http.get(f"{DISCORD_API}/users/@me", headers=headers).json()
        member = http.get(f"{DISCORD_API}/users/@me/guilds/{GUILD_ID}/member", headers=headers)

    if member.status_code != 200:
        return RedirectResponse("/?error=notmember")
    m = member.json()
    name = m.get("nick") or me.get("global_name") or me.get("username")
    avatar = None
    if me.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64"

    payload = {
        "id": str(me["id"]),
        "name": name,
        "avatar": avatar,
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }
    res = RedirectResponse("/")
    res.set_cookie(
        "session", make_cookie(payload), max_age=SESSION_DAYS * 86400,
        httponly=True, secure=SECURE_COOKIE, samesite="lax",
    )
    res.delete_cookie("oauth_state")
    return res


@app.get("/logout")
def logout():
    res = RedirectResponse("/")
    res.delete_cookie("session")
    return res


# ---------- API ----------
@app.get("/api/me")
def api_me(user=Depends(current_user)):
    s = db.get_settings(get_db(), GUILD_ID)
    return {
        "user": {"id": user["id"], "name": user["name"], "avatar": user.get("avatar")},
        "today": today(),
        "price": s["price"],
        "slots": [{"key": k, "label": v} for k, v in db.SLOTS.items()],
    }


BOT_ONLINE_SECONDS = 90  # BOTは30秒ごとに生存報告するので、これより古ければ止まっているとみなす


@app.get("/api/status")
def api_status(user=Depends(current_user)):
    """設定画面の「接続状況」。DBが落ちていても必ず答える"""
    global _conn
    url = os.environ.get("DATABASE_URL") or ""
    database = {"ok": False, "kind": "Postgres" if url else "SQLite（この端末のファイル）", "latency_ms": None, "error": None}
    bot = {"state": "unknown", "name": None, "seen": None, "seconds_ago": None, "started": None, "latency_ms": None}
    row = None
    try:
        t = time.perf_counter()
        conn = get_db()
        db.ping(conn)
        database["latency_ms"] = round((time.perf_counter() - t) * 1000)
        database["ok"] = True
        row = db.get_settings(conn, GUILD_ID)
    except Exception as e:
        _conn = None
        database["error"] = _explain(e)

    if row is not None:
        seen = row["bot_seen"]
        if not seen:
            bot["state"] = "never"
        else:
            ago = int(time.time() - datetime.fromisoformat(seen).timestamp())
            bot.update(
                state="online" if ago <= BOT_ONLINE_SECONDS else "offline",
                name=row["bot_name"], seen=seen, seconds_ago=max(ago, 0),
                started=row["bot_started"], latency_ms=row["bot_latency_ms"],
            )
    return {"database": database, "bot": bot, "guild_id": str(GUILD_ID), "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/day")
def api_day(date: str, user=Depends(current_user)):
    if not valid_date(date):
        raise HTTPException(400, "日付が変")
    board = db.day_board(get_db(), GUILD_ID, date)
    uid = int(user["id"])
    return {
        "date": date,
        "slots": [
            {
                "key": k,
                "label": label,
                "members": [{"id": str(i), "name": n} for i, n in board.get(k, [])],
                "joined": any(i == uid for i, _ in board.get(k, [])),
            }
            for k, label in db.SLOTS.items()
        ],
    }


class ToggleBody(BaseModel):
    date: str
    slot: str


@app.post("/api/toggle")
def api_toggle(body: ToggleBody, user=Depends(current_user)):
    if not valid_date(body.date) or body.slot not in db.SLOTS:
        raise HTTPException(400, "日付か枠が変")
    if body.date > today():
        raise HTTPException(400, "未来の日は記録できないよ")
    conn = get_db()
    joined = db.toggle(conn, GUILD_ID, body.date, body.slot, int(user["id"]), user["name"])
    db.mark_panel_dirty(conn, GUILD_ID)  # Discordのパネルにも反映してもらう
    return {"joined": joined}


@app.get("/api/summary")
def api_summary(month: str, user=Depends(current_user)):
    if not valid_month(month):
        raise HTTPException(400, "月が変")
    conn = get_db()
    rows = db.month_summary(conn, GUILD_ID, month)
    days = len({r["date"] for r in db.month_rows(conn, GUILD_ID, month)})
    return {
        "month": month,
        "ranking": [{**r, "user_id": str(r["user_id"])} for r in rows],
        "slots": [
            {"key": k, "label": db.SLOTS[k], "count": c}
            for k, c in db.month_slot_counts(conn, GUILD_ID, month).items()
        ],
        "total_count": sum(r["count"] for r in rows),
        "total_amount": sum(r["amount"] for r in rows),
        "days": days,
    }


@app.get("/api/my")
def api_my(month: str, user=Depends(current_user)):
    if not valid_month(month):
        raise HTTPException(400, "月が変")
    rows = db.user_month_detail(get_db(), GUILD_ID, int(user["id"]), month)
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(db.SLOTS.get(r["slot"], r["slot"]))
    return {
        "month": month,
        "days": [{"date": d, "slots": s} for d, s in by_day.items()],
        "count": len(rows),
        "amount": sum(int(r["price"]) for r in rows),
    }


@app.get("/api/export.csv")
def api_export(month: str, user=Depends(current_user)):
    if not valid_month(month):
        raise HTTPException(400, "月が変")
    conn = get_db()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["名前", "回数", "支払額"])
    for r in db.month_summary(conn, GUILD_ID, month):
        w.writerow([r["user_name"], r["count"], r["amount"]])
    w.writerow([])
    w.writerow(["日付", "時間", "名前", "単価"])
    for r in db.month_rows(conn, GUILD_ID, month):
        w.writerow([r["date"], db.SLOTS.get(r["slot"], r["slot"]), r["user_name"], r["price"]])
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=guerrilla_{month}.csv"},
    )


@app.exception_handler(HTTPException)
def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _explain(exc: Exception) -> str:
    """想定外のエラーを、画面に出せる短い説明にする（パスワード等は伏せる）"""
    msg = f"{type(exc).__name__}: {exc}"
    url = os.environ.get("DATABASE_URL") or ""
    if url:
        msg = msg.replace(url, "<DATABASE_URL>")
        if "@" in url and ":" in url.split("@")[0]:
            pw = url.split("@")[0].rsplit(":", 1)[1]
            if pw:
                msg = msg.replace(pw, "****")
    if not url and os.environ.get("VERCEL"):
        return "DATABASE_URL が Vercel に設定されていません（Settings → Environment Variables に入れて Redeploy）"
    if "psycopg" in type(exc).__module__ or "OperationalError" in msg:
        return "データベースに繋がりません: " + msg[:300]
    return "サーバーエラー: " + msg[:300]


@app.exception_handler(Exception)
def server_error(request, exc):
    import traceback

    traceback.print_exc()  # Vercel のログにも残す
    global _conn
    _conn = None  # 次のリクエストで繋ぎ直す
    return JSONResponse({"error": _explain(exc)}, status_code=500)


# ---------- 画面 ----------
@app.get("/")
def index():
    return FileResponse(BASE / "web" / "index.html")
