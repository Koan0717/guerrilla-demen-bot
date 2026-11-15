# ゲリラ出面表BOT

ゲリラに出た枠をボタンで記録して、月ごとに「誰が何回出て、いくら払うか」を自動で集計する Discord BOT。

- 枠: 21:00 / 21:30 / 22:00 / 22:30 / 23:00 / 23:30
- 単価: 1回 240円（`/単価` で変更可。変更前の記録は前の単価のまま計算）
- 支払額 = 出た回数 × 単価

## コマンド

| コマンド | 内容 |
|---|---|
| `/パネル設置` | **おすすめ**。打ったチャンネルに常設パネルを置く。毎日自動で「今日」に切り替わり、ボタンで 参加記録 / 自分の記録 / 今月の集計 が見られる（サーバー管理権限。置き直すと前のパネルは消える） |
| `/出面表 [日付]` | 参加ボタン付きの出面表を出す。出た枠のボタンを押すだけ（もう一回押すと取消） |
| `/集計 [月] [csv出力]` | その月の回数・支払額ランキング、枠ごとの延べ参加、合計。CSV付きにもできる |
| `/自分 [月] [誰]` | 自分（または指定した人）の参加履歴と支払額。自分にだけ表示 |
| `/記録追加 誰 枠 [日付]` | 押し忘れた人の分を代わりに追加 |
| `/記録削除 誰 枠 [日付]` | 間違えた記録を消す |
| `/単価 金額` | 1回あたりの金額を変更（サーバー管理権限） |
| `/自動投稿 時刻` | 毎日その時刻に、そのチャンネルへ出面表を自動で出す（例 `20:50`、`off`で停止。サーバー管理権限） |

日付は `11/12`・`2026-11-12`・`12`・`昨日` など、月は `11`・`2026-11`・`先月` などで指定できます（省略で今日/今月）。時刻はすべて日本時間で、**朝5時までは前日扱い**（深夜に23:30枠を押しても前日分に入る）。

## セットアップ

1. https://discord.com/developers/applications で New Application → **Bot** タブで **Reset Token** してトークンをコピー
2. **OAuth2 → URL Generator** で scopes に `bot` と `applications.commands`、権限に `Send Messages` `Embed Links` `Attach Files` を付けて、出てきたURLからサーバーに招待
3. このフォルダの `.env.example` をコピーして `.env` にし、トークンを書く
   - `GUILD_ID`（サーバーID）も書くとコマンドがすぐ出てくる（書かないと反映に最大1時間くらいかかる）
4. 起動
   ```
   pip install -r requirements.txt
   python bot.py
   ```
   Windowsなら `start.bat` をダブルクリックでもOK

データはこのフォルダの `demen.db` に保存されます（消すと記録も消えるので注意）。

## VPS (pm2) で常時動かす

```bash
sudo apt install -y python3-venv git          # 初回だけ
git clone https://github.com/Koan0717/guerrilla-demen-bot.git
cd guerrilla-demen-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env              # トークンを書く
pm2 start ecosystem.config.js
pm2 save                                       # 再起動後も自動で立ち上がるように
```

- ログ: `pm2 logs demen-bot` / 再起動: `pm2 restart demen-bot`
- 更新: `git pull && venv/bin/pip install -r requirements.txt && pm2 restart demen-bot`
- バックアップするなら `demen.db` をコピーしておく
