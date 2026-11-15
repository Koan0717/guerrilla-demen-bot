"""ゲリラ出面表BOT

ボタンで参加枠を記録 → 月ごとに「何回出たか・いくら払うか」を集計する。
"""
import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

import db

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).parent


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # 入れておくとコマンドが即反映される

conn = db.connect()

SLOT_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in db.SLOTS.items()]


# ---------- 日付ヘルパー ----------
DAY_START_HOUR = 5  # 朝5時までは前日扱い（23:30枠を日付またいで押しても前日に入る）


def today():
    return (datetime.now(JST) - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def this_month():
    return datetime.now(JST).strftime("%Y-%m")


def parse_date(text):
    """'2026-11-12' / '11/12' / '12' / '今日' / '昨日' / 空 → YYYY-MM-DD"""
    now = datetime.now(JST)
    if not text or text in ("今日", "きょう"):
        return today()
    if text in ("昨日", "きのう"):
        return (datetime.strptime(today(), "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    text = text.strip().replace("/", "-")
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
            d = datetime.strptime(text, "%Y-%m-%d")
        elif re.fullmatch(r"\d{1,2}-\d{1,2}", text):
            d = datetime.strptime(f"{now.year}-{text}", "%Y-%m-%d")
        elif re.fullmatch(r"\d{1,2}", text):
            d = datetime(now.year, now.month, int(text))
        else:
            return None
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d")


def parse_month(text):
    """'2026-11' / '11' / '先月' / 空 → YYYY-MM"""
    now = datetime.now(JST)
    if not text or text in ("今月",):
        return now.strftime("%Y-%m")
    if text == "先月":
        return (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    text = text.strip().replace("/", "-").removesuffix("月")
    if re.fullmatch(r"\d{4}-\d{1,2}", text):
        y, m = text.split("-")
    elif re.fullmatch(r"\d{1,2}", text):
        y, m = now.year, text
    else:
        return None
    m = int(m)
    if not 1 <= m <= 12:
        return None
    return f"{int(y):04d}-{m:02d}"


def jp_date(date):
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"{d.month}/{d.day}({'月火水木金土日'[d.weekday()]})"


def yen(n):
    return f"{n:,}円"


# ---------- 出面表パネル ----------
def board_embed(guild_id, date):
    board = db.day_board(conn, guild_id, date)
    price = db.get_settings(conn, guild_id)["price"]
    total = sum(len(v) for v in board.values())
    e = discord.Embed(
        title=f"📋 出面表 {jp_date(date)}",
        description=f"出た枠のボタンを押してね（もう一回押すと取り消し）\n単価: **{yen(price)}** / 回",
        color=0x2ECC71,
    )
    for key, label in db.SLOTS.items():
        members = board.get(key, [])
        value = " ".join(f"<@{uid}>" for uid, _ in members) if members else "—"
        e.add_field(name=f"{label}（{len(members)}人）", value=value, inline=False)
    e.set_footer(text=f"この日の延べ参加: {total}回")
    return e


class SlotButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"demen:(?P<date>\d{4}-\d{2}-\d{2}):(?P<slot>\d{4})",
):
    def __init__(self, date, slot):
        super().__init__(
            discord.ui.Button(
                label=db.SLOTS[slot],
                style=discord.ButtonStyle.primary,
                custom_id=f"demen:{date}:{slot}",
            )
        )
        self.date = date
        self.slot = slot

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["date"], match["slot"])

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        joined = db.toggle(conn, interaction.guild_id, self.date, self.slot, user.id, user.display_name)
        await interaction.response.edit_message(
            embed=board_embed(interaction.guild_id, self.date), view=board_view(self.date)
        )
        msg = "参加を記録したよ ✅" if joined else "取り消したよ ↩️"
        await interaction.followup.send(
            f"{jp_date(self.date)} {db.SLOTS[self.slot]} … {msg}", ephemeral=True
        )
        await refresh_panel(interaction.guild_id)


def board_view(date):
    view = discord.ui.View(timeout=None)
    for i, slot in enumerate(db.SLOTS):
        item = SlotButton(date, slot)
        item.item.row = i // 3
        view.add_item(item)
    return view


# ---------- 常設パネル ----------
def panel_embed(guild_id):
    e = board_embed(guild_id, today())
    e.title = f"📌 ゲリラ出面表パネル ｜ {jp_date(today())}"
    e.description = (
        "出た枠のボタンを押してね（もう一回押すと取り消し）\n"
        f"単価: **{yen(db.get_settings(conn, guild_id)['price'])}** / 回 ・ 朝{DAY_START_HOUR}時で次の日に切り替わるよ"
    )
    summary = db.month_summary(conn, guild_id, this_month())
    e.set_footer(
        text=f"今月の延べ参加: {sum(r['count'] for r in summary)}回 / {yen(sum(r['amount'] for r in summary))}"
    )
    return e


class PanelView(discord.ui.View):
    """日付を持たない常設パネル。押した時点の「今日」に記録する"""

    def __init__(self):
        super().__init__(timeout=None)
        for i, (slot, label) in enumerate(db.SLOTS.items()):
            btn = discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary, custom_id=f"panel:slot:{slot}", row=i // 3
            )
            btn.callback = self._make_slot_cb(slot)
            self.add_item(btn)

    def _make_slot_cb(self, slot):
        async def cb(interaction: discord.Interaction):
            date = today()
            user = interaction.user
            joined = db.toggle(conn, interaction.guild_id, date, slot, user.id, user.display_name)
            db.set_panel_date(conn, interaction.guild_id, date)
            await interaction.response.edit_message(embed=panel_embed(interaction.guild_id), view=self)
            msg = "参加を記録したよ ✅" if joined else "取り消したよ ↩️"
            await interaction.followup.send(f"{jp_date(date)} {db.SLOTS[slot]} … {msg}", ephemeral=True)

        return cb

    @discord.ui.button(label="自分の記録", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="panel:me", row=2)
    async def me_btn(self, interaction: discord.Interaction, button):
        await interaction.response.send_message(
            embed=me_embed(interaction.guild_id, interaction.user, this_month()), ephemeral=True
        )

    @discord.ui.button(label="今月の集計", emoji="💰", style=discord.ButtonStyle.success, custom_id="panel:summary", row=2)
    async def summary_btn(self, interaction: discord.Interaction, button):
        await interaction.response.send_message(
            embed=summary_embed(interaction.guild_id, this_month()), ephemeral=True
        )


async def post_board(channel, date):
    await channel.send(embed=board_embed(channel.guild.id, date), view=board_view(date))


# ---------- Bot 本体 ----------
class DemenBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_dynamic_items(SlotButton)
        self.panel_view = PanelView()
        self.add_view(self.panel_view)
        panel_refresh_loop.start()
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        auto_post_loop.start()

    async def on_ready(self):
        print(f"ログインしました: {self.user} (ID: {self.user.id})")


bot = DemenBot()


@tasks.loop(seconds=30)
async def auto_post_loop():
    now = datetime.now(JST)
    hhmm, date = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    for s in db.auto_post_targets(conn):
        if s["last_posted_date"] == date or hhmm < s["panel_time"]:
            continue
        channel = bot.get_channel(s["panel_channel_id"])
        if channel is None:
            continue
        try:
            await post_board(channel, date)
            db.mark_posted(conn, s["guild_id"], date)
        except discord.HTTPException as e:
            print(f"自動投稿に失敗: {e}")


@tasks.loop(minutes=1)
async def panel_refresh_loop():
    """日付が変わったら常設パネルを新しい日に書き換える"""
    date = today()
    for s in db.panel_targets(conn):
        if s["board_date"] == date:
            continue
        channel = bot.get_channel(s["board_channel_id"])
        if channel is None:
            continue
        try:
            msg = channel.get_partial_message(s["board_message_id"])
            await msg.edit(embed=panel_embed(s["guild_id"]), view=bot.panel_view)
            db.set_panel_date(conn, s["guild_id"], date)
        except discord.NotFound:
            db.set_panel(conn, s["guild_id"], None, None, None)  # パネルが消されていたら解除
        except discord.HTTPException as e:
            print(f"パネル更新に失敗: {e}")


async def refresh_panel(gid):
    """記録をコマンドやほかの表で変えたとき、常設パネルの表示も更新する"""
    s = db.get_settings(conn, gid)
    if not s["board_message_id"]:
        return
    channel = bot.get_channel(s["board_channel_id"])
    if channel is None:
        return
    try:
        await channel.get_partial_message(s["board_message_id"]).edit(embed=panel_embed(gid), view=bot.panel_view)
        db.set_panel_date(conn, gid, today())
    except discord.HTTPException:
        pass


@auto_post_loop.before_loop
@panel_refresh_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


# ---------- コマンド ----------
@bot.tree.command(name="出面表", description="出面表（参加ボタン）を出す")
@app_commands.describe(日付="例: 11/12, 2026-11-12, 昨日（省略で今日）")
@app_commands.guild_only()
async def cmd_board(interaction: discord.Interaction, 日付: str = None):
    date = parse_date(日付)
    if date is None:
        return await interaction.response.send_message("日付の形がわからんかった… 例: `11/12`", ephemeral=True)
    await interaction.response.send_message(embed=board_embed(interaction.guild_id, date), view=board_view(date))


@bot.tree.command(name="集計", description="月ごとの参加回数と支払額を出す")
@app_commands.describe(月="例: 11, 2026-11, 先月（省略で今月）", csv出力="CSVファイルも付ける")
@app_commands.guild_only()
async def cmd_summary(interaction: discord.Interaction, 月: str = None, csv出力: bool = False):
    month = parse_month(月)
    if month is None:
        return await interaction.response.send_message("月の形がわからんかった… 例: `11` / `2026-11`", ephemeral=True)
    gid = interaction.guild_id
    files = []
    if csv出力:
        files.append(make_csv(gid, month, db.month_summary(conn, gid, month)))
    await interaction.response.send_message(embed=summary_embed(gid, month), files=files)


def summary_embed(gid, month):
    rows = db.month_summary(conn, gid, month)
    y, m = month.split("-")
    e = discord.Embed(title=f"💰 {int(y)}年{int(m)}月 ゲリラ集計", color=0xF1C40F)
    if not rows:
        e.description = "この月の記録はまだないよ"
        return e

    total_count = sum(r["count"] for r in rows)
    total_amount = sum(r["amount"] for r in rows)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        head = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{head} <@{r['user_id']}> **{r['count']}回** → **{yen(r['amount'])}**")
    e.description = "\n".join(lines)

    slot_counts = db.month_slot_counts(conn, gid, month)
    e.add_field(
        name="枠ごとの延べ参加",
        value=" / ".join(f"{db.SLOTS[s]}: {c}" for s, c in slot_counts.items()),
        inline=False,
    )
    days = len({r["date"] for r in db.month_rows(conn, gid, month)})
    e.add_field(name="合計", value=f"延べ **{total_count}回** / **{yen(total_amount)}**（{days}日分）", inline=False)
    return e


def make_csv(gid, month, summary):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["名前", "回数", "支払額"])
    for r in summary:
        w.writerow([r["user_name"], r["count"], r["amount"]])
    w.writerow([])
    w.writerow(["日付", "時間", "名前", "単価"])
    for r in db.month_rows(conn, gid, month):
        w.writerow([r["date"], db.SLOTS.get(r["slot"], r["slot"]), r["user_name"], r["price"]])
    data = buf.getvalue().encode("utf-8-sig")  # Excelで文字化けしないようBOM付き
    return discord.File(io.BytesIO(data), filename=f"ゲリラ集計_{month}.csv")


@bot.tree.command(name="自分", description="自分の参加履歴と支払額を見る（自分だけに表示）")
@app_commands.describe(月="例: 11, 2026-11, 先月（省略で今月）", 誰="他の人を見るとき")
@app_commands.guild_only()
async def cmd_me(interaction: discord.Interaction, 月: str = None, 誰: discord.Member = None):
    month = parse_month(月)
    if month is None:
        return await interaction.response.send_message("月の形がわからんかった… 例: `11`", ephemeral=True)
    target = 誰 or interaction.user
    await interaction.response.send_message(embed=me_embed(interaction.guild_id, target, month), ephemeral=True)


def me_embed(gid, target, month):
    rows = db.user_month_detail(conn, gid, target.id, month)
    y, m = month.split("-")
    e = discord.Embed(title=f"🧾 {target.display_name} の {int(y)}年{int(m)}月", color=0x3498DB)
    if not rows:
        e.description = "この月の記録はないよ"
    else:
        by_day = {}
        for r in rows:
            by_day.setdefault(r["date"], []).append(db.SLOTS.get(r["slot"], r["slot"]))
        e.description = "\n".join(f"{jp_date(d)}  {' / '.join(s)}" for d, s in by_day.items())[:4000]
        e.add_field(name="合計", value=f"**{len(rows)}回** → **{yen(sum(r['price'] for r in rows))}**")
    return e


@bot.tree.command(name="パネル設置", description="このチャンネルに常設の出面表パネルを置く（毎日自動で切り替わる）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_panel(interaction: discord.Interaction):
    gid = interaction.guild_id
    old = db.get_settings(conn, gid)
    await interaction.response.send_message(embed=panel_embed(gid), view=bot.panel_view)
    msg = await interaction.original_response()
    db.set_panel(conn, gid, interaction.channel_id, msg.id, today())
    # 前のパネルがあれば消す（1サーバー1枚）
    if old["board_message_id"]:
        ch = bot.get_channel(old["board_channel_id"])
        if ch is not None:
            try:
                await ch.get_partial_message(old["board_message_id"]).delete()
            except discord.HTTPException:
                pass


@bot.tree.command(name="記録追加", description="押し忘れた人の記録を追加する")
@app_commands.describe(誰="対象のメンバー", 枠="時間", 日付="例: 11/12（省略で今日）")
@app_commands.choices(枠=SLOT_CHOICES)
@app_commands.guild_only()
async def cmd_add(interaction: discord.Interaction, 誰: discord.Member, 枠: app_commands.Choice[str], 日付: str = None):
    date = parse_date(日付)
    if date is None:
        return await interaction.response.send_message("日付の形がわからんかった… 例: `11/12`", ephemeral=True)
    ok = db.add(conn, interaction.guild_id, date, 枠.value, 誰.id, 誰.display_name)
    msg = "追加したよ ✅" if ok else "もう記録されてるよ"
    await interaction.response.send_message(f"{誰.mention} {jp_date(date)} {枠.name} … {msg}")
    await refresh_panel(interaction.guild_id)


@bot.tree.command(name="記録削除", description="間違えた記録を消す")
@app_commands.describe(誰="対象のメンバー", 枠="時間", 日付="例: 11/12（省略で今日）")
@app_commands.choices(枠=SLOT_CHOICES)
@app_commands.guild_only()
async def cmd_remove(interaction: discord.Interaction, 誰: discord.Member, 枠: app_commands.Choice[str], 日付: str = None):
    date = parse_date(日付)
    if date is None:
        return await interaction.response.send_message("日付の形がわからんかった… 例: `11/12`", ephemeral=True)
    ok = db.remove(conn, interaction.guild_id, date, 枠.value, 誰.id)
    msg = "消したよ 🗑️" if ok else "その記録は見つからんかった"
    await interaction.response.send_message(f"{誰.mention} {jp_date(date)} {枠.name} … {msg}")
    await refresh_panel(interaction.guild_id)


@bot.tree.command(name="単価", description="1回あたりの金額を変える（これ以降の記録に適用）")
@app_commands.describe(金額="円（例: 240）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_price(interaction: discord.Interaction, 金額: app_commands.Range[int, 0, 100000]):
    db.set_price(conn, interaction.guild_id, 金額)
    await interaction.response.send_message(f"単価を **{yen(金額)}** にしたよ（これから記録する分に適用）")
    await refresh_panel(interaction.guild_id)


@bot.tree.command(name="自動投稿", description="毎日決まった時間にこのチャンネルへ出面表を出す")
@app_commands.describe(時刻="例: 20:50（オフにするなら off）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_auto(interaction: discord.Interaction, 時刻: str):
    gid = interaction.guild_id
    if 時刻.lower() in ("off", "オフ", "なし"):
        db.set_auto_post(conn, gid, None, None)
        return await interaction.response.send_message("自動投稿をオフにしたよ")
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", 時刻.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return await interaction.response.send_message("時刻は `20:50` みたいに書いてね", ephemeral=True)
    hhmm = f"{int(m[1]):02d}:{m[2]}"
    db.set_auto_post(conn, gid, interaction.channel_id, hhmm)
    # 今日すでに過ぎてる時刻なら今日分は出さない
    if datetime.now(JST).strftime("%H:%M") >= hhmm:
        db.mark_posted(conn, gid, today())
    await interaction.response.send_message(f"毎日 **{hhmm}** にこのチャンネルへ出面表を出すよ 📋")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(".env に DISCORD_TOKEN=... を書いてね（README参照）")
    bot.run(TOKEN)
