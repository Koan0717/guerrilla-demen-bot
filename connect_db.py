"""BOT（VPS）をサイトと同じデータベースに繋ぐ

Web の「設定」タブで「BOTを接続」を押すと出てくるコマンドから実行される:
    venv/bin/python connect_db.py https://<サイトのURL> <接続コード>

やること:
  1. サイトに接続コードを渡して DATABASE_URL を受け取る
  2. このフォルダの .env に DATABASE_URL（と空なら GUILD_ID）を書き込む
  3. 実際に繋がるか確かめる
  4. 手元の demen.db に記録があれば、そのデータベースへコピーする（重複はスキップ）
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
ENV = BASE / ".env"


def fail(msg):
    print(f"✗ {msg}")
    sys.exit(1)


def fetch(site, code):
    req = urllib.request.Request(
        f"{site.rstrip('/')}/api/bot-link/redeem",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        fail(msg or f"サイトがエラーを返しました（{e.code}）")
    except urllib.error.URLError as e:
        fail(f"サイトに繋がりません: {e.reason}")


def write_env(values, only_if_empty=()):
    """.env の該当行を書き換える（無ければ足す）。他の行はそのまま残す"""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            current = line.split("=", 1)[1].strip()
            if key not in only_if_empty or not current:
                lines[i] = f"{key}={values[key]}"
            done.add(key)
    for key, v in values.items():
        if key not in done:
            lines.append(f"{key}={v}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)  # パスワード入りなので自分だけ読めるように
    except OSError:
        pass


def main():
    if len(sys.argv) != 3:
        fail("使い方: python connect_db.py <サイトのURL> <接続コード>")
    site, code = sys.argv[1], sys.argv[2]

    print("サイトから接続先を受け取っています…")
    data = fetch(site, code)
    url = data.get("database_url")
    if not url:
        fail("接続先を受け取れませんでした")

    values = {"DATABASE_URL": url}
    if data.get("guild_id") and data["guild_id"] != "0":
        values["GUILD_ID"] = data["guild_id"]
    write_env(values, only_if_empty={"GUILD_ID"})
    print("✓ .env に DATABASE_URL を書き込みました")

    os.environ["DATABASE_URL"] = url
    sys.path.insert(0, str(BASE))
    try:
        import db

        db.ping(db.connect())
    except Exception as e:
        fail(f"データベースに繋がりません: {type(e).__name__}: {str(e).replace(url, '<DATABASE_URL>')[:300]}")
    print("✓ データベースに接続できました")

    if (BASE / "demen.db").exists():
        print("手元の demen.db の記録をデータベースへコピーしています…")
        r = subprocess.run([sys.executable, str(BASE / "migrate_to_cloud.py")], cwd=BASE)
        if r.returncode != 0:
            print("△ コピーに失敗しました。あとで `venv/bin/python migrate_to_cloud.py` を試してね")

    print("✓ 完了。BOTを再起動すると、サイトの設定タブで「稼働中」になります")


if __name__ == "__main__":
    main()
