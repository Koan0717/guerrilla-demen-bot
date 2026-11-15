// pm2 用設定: pm2 start ecosystem.config.js
const path = require("path");

module.exports = {
  apps: [
    {
      name: "demen-bot", // 1つ目のBOTと被らない名前
      cwd: __dirname,
      script: "bot.py",
      interpreter: path.join(__dirname, "venv/bin/python"),
      interpreter_args: "-u", // ログをすぐ pm2 logs に出す
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
    },
  ],
};
