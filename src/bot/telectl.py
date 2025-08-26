# src/bot/telectl.py
import os, json, time
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

# Load .env so TELEGRAM_BOT_TOKEN / STATE_PATH are visible
load_dotenv()

STATE_PATH = Path(os.getenv("STATE_PATH", "./data/state.json")).resolve()
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
if not STATE_PATH.exists():
    STATE_PATH.write_text(json.dumps({"positions": {}, "equity_usd": None, "peak_equity_usd": None}, indent=2))

def _load() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"positions": {}}

def _save(d: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(d, indent=2))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _load()
    tg = d.get("telegram", {})
    if "owner_chat_id" not in tg:
        tg["owner_chat_id"] = update.effective_chat.id
        d["telegram"] = tg
        _save(d)
        msg = "👋 Linked. This chat is now the owner."
    else:
        msg = "Already linked."
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _load()
    mode = d.get("mode", "normal")
    pos = d.get("positions", {})
    lines = [f"🟢 Status | mode={mode}", time.strftime("%Y-%m-%d %H:%M:%S")]
    if pos:
        lines.append(f"positions: {len(pos)}")
        for sym, p in pos.items():
            try:
                lines.append(f"• {sym}: qty={p.get('qty'):.6f} entry={p.get('entry'):.4f} stop={p.get('stop'):.4f}")
            except Exception:
                lines.append(f"• {sym}: {p}")
    else:
        lines.append("positions: none")
    await update.message.reply_text("\n".join(lines))

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("usage: /mode <name>\nex: /mode hype")
    m = context.args[0].lower()
    d = _load()
    d["mode"] = m
    _save(d)
    await update.message.reply_text(f"mode set to: {m}")

async def objective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("usage: /objective <type> [params]\nex: /objective target 1000 8")
    d = _load()
    d["objective"] = {"raw": " ".join(context.args), "ts": time.time()}
    _save(d)
    await update.message.reply_text(f"objective noted: {' '.join(context.args)}")

async def moonshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _load()
    d["moonshot"] = {"request": " ".join(context.args) if context.args else "suggest", "ts": time.time()}
    _save(d)
    await update.message.reply_text("moonshot request noted (stub).")

async def auto_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        return await update.message.reply_text("usage: /auto_old on|off")
    val = context.args[0].lower() == "on"
    d = _load()
    d["auto_old"] = val
    _save(d)
    await update.message.reply_text(f"auto_old set to: {val}")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"your id: {update.effective_user.id}")

def main():
    token = os.getenv("TELEGRAM_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("objective", objective))
    app.add_handler(CommandHandler("moonshot", moonshot))
    app.add_handler(CommandHandler("auto_old", auto_old))
    app.run_polling(allowed_updates=["message"])
    app.add_handler(CommandHandler("whoami", whoami))

if __name__ == "__main__":
    main()
