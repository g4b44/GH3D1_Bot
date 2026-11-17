#!/usr/bin/env python3
"""
TET Trading Alert Bot
Bot Name  : TET_AlertBot
Community : TET Community | GH3D1-TR4D35
Sessions  : New York Time (EST / UTC-5)
Display   : NY Time + Sweden Time (CET / UTC+1)
RR        : 1:3  |  Risk : 1% or 0.5%
Webhook   : POST /webhook  ← TradingView Pine alerts
"""

import logging
import json
from datetime import datetime, timezone, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = "9032865574:AAHgouaTL-G1qcGTsSJCbFarLSPu0rLm5UY"
CHAT_ID      = "8934346233"
WEBHOOK_PORT = 8080          # Railway exposes this port
WEBHOOK_PATH = "/webhook"    # TradingView posts here

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── TIMEZONES ────────────────────────────────────────────────────────────────
UTC   = timezone.utc
NY_TZ = timezone(timedelta(hours=-5))   # EST UTC-5
SE_TZ = timezone(timedelta(hours=+1))   # CET UTC+1

def ny_now():  return datetime.now(NY_TZ)
def se_now():  return datetime.now(SE_TZ)
def utc_now(): return datetime.now(UTC)

# ─── PURGING HOURS (NY Time) ──────────────────────────────────────────────────
PURGING_HOURS_NY = {
    "Asian":    {"ny_hours": [20, 21, 22], "label_ny": "8pm–10pm", "label_se": "02–04",  "emoji": "🌏"},
    "London":   {"ny_hours": [2, 3],       "label_ny": "2am–3am",  "label_se": "08–09",  "emoji": "🌍"},
    "New York": {"ny_hours": [8, 9, 10],   "label_ny": "8am–10am", "label_se": "14–16",  "emoji": "🗽"},
}

def get_active_session():
    h = ny_now().hour
    for name, info in PURGING_HOURS_NY.items():
        if h in info["ny_hours"]:
            return {"session": name, "ny_time": ny_now().strftime("%I:%M %p"),
                    "se_time": se_now().strftime("%H:%M"), "emoji": info["emoji"]}
    return None

def next_purge_text():
    h = ny_now().hour
    lines = []
    for name, info in PURGING_HOURS_NY.items():
        upcoming = [x for x in info["ny_hours"] if x > h]
        if upcoming:
            x = upcoming[0]
            ampm = "am" if x < 12 else "pm"
            h12  = x if x <= 12 else x - 12
            se_h = (x + 6) % 24
            lines.append(f"{info['emoji']} {name}: {h12}{ampm} NY → {se_h:02d}:00 SE")
    return "\n".join(lines) if lines else "Tomorrow — Asian 8pm NY (02:00 SE)"

# ─── CONVERSATION STATES ──────────────────────────────────────────────────────
BIAS, SESSION, PROFILE, TSQ, MODEL1, SMT, SYMBOL, DIRECTION, ENTRY, SL, TP = range(11)

# ─── FORMAT HELPERS ───────────────────────────────────────────────────────────
def _clock_line():
    active = get_active_session()
    if active:
        return (f"⚡ *PURGING ACTIVE* — {active['emoji']} {active['session']}\n"
                f"   NY: {active['ny_time']}  |  SE: {active['se_time']}")
    return "💤 Outside purging hours"

def _clock_stamp():
    return (f"🗽 NY  : {ny_now().strftime('%I:%M %p')} EST\n"
            f"🇸🇪 SE  : {se_now().strftime('%H:%M')} CET")

# ─── FORMAT MANUAL ALERT ──────────────────────────────────────────────────────
def format_alert(data: dict) -> str:
    d_emoji = "🟢 BUY" if data["direction"] == "BUY" else "🔴 SELL"
    try:
        entry  = float(data["entry"])
        sl     = float(data["sl"])
        tp     = float(data["tp"])
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        rr     = round(reward / risk, 2) if risk > 0 else "N/A"
    except Exception:
        rr = "N/A"

    return f"""
╔══════════════════════════════╗
   📡 *TET ALERT BOT*
╚══════════════════════════════╝

{d_emoji} | *{data.get('symbol', 'XAUUSD')}*

━━━━━━ 📋 CHECKLIST ━━━━━━
✅ Daily Bias    : *{data['bias']}*
✅ Session        : *{data['session']}*
✅ Daily Profile  : *{data['profile']}*
✅ 1H TSQ         : *{data['tsq']}*
✅ Model #1       : *{data['model1']}*
✅ SMT Confirm    : *{data['smt']}*

━━━━━━ 💰 TRADE INFO ━━━━━━
📌 Entry   : *{data['entry']}*
🛑 SL      : *{data['sl']}*
🎯 TP      : *{data['tp']}*
📊 RR      : *1:{rr}*
💼 Risk    : *1% — 0.5%*

━━━━━━ ⏰ CLOCK ━━━━━━━━━━
{_clock_stamp()}
{_clock_line()}
━━━━━━━━━━━━━━━━━━━━━━━━━━
_"Don't follow the market,_
_understand what drives it"_
_— GH3D1-TR4D35_
""".strip()

# ─── FORMAT AUTO SIGNAL (from TradingView webhook) ────────────────────────────
def format_auto_signal(payload: dict) -> str:
    """
    Expected JSON from Pine alert:
    {
      "direction"  : "BUY" | "SELL",
      "symbol"     : "XAUUSD",
      "entry"      : 2650.50,
      "sl"         : 2640.00,       ← C2 extreme from Pine
      "checklist"  : {
          "daily_bias" : true,
          "h7"         : true,
          "h1"         : true,
          "purge"      : true,
          "smt"        : true,
          "model1"     : true
      },
      "model_tag"  : "C2",          ← "C2", "C3", "C2+PSP" etc.
      "session"    : "London",
      "rating"     : "A+"
    }
    """
    direction = payload.get("direction", "BUY").upper()
    symbol    = payload.get("symbol", "XAUUSD").upper()
    model_tag = payload.get("model_tag", "C2")
    session   = payload.get("session", "")
    rating    = payload.get("rating", "A+")
    d_emoji   = "🟢 BUY" if direction == "BUY" else "🔴 SELL"

    try:
        entry = float(payload["entry"])
        sl    = float(payload["sl"])
        risk  = abs(entry - sl)
        tp    = round(entry + risk * 3, 2) if direction == "BUY" else round(entry - risk * 3, 2)
        rr    = "3.0"
    except Exception:
        sl = tp = entry = 0.0
        rr = "N/A"

    cl = payload.get("checklist", {})
    def ck(key): return "✅" if cl.get(key) else "⬜"

    active = get_active_session()
    sess_emoji = active["emoji"] if active else "⚡"
    sess_name  = active["session"] if active else session

    return f"""
╔══════════════════════════════╗
   📡 *TET AUTO SIGNAL*
╚══════════════════════════════╝

{d_emoji} | *{symbol}*  |  Model: *{model_tag}*

━━━━━━ 📋 CHECKLIST ━━━━━━
{ck('daily_bias')} Daily Bias
{ck('h7')} 7H Confirmation
{ck('h1')} 1H Key Level
{ck('purge')} Purge Active
{ck('smt')} SMT / PSP
{ck('model1')} Model 1

━━━━━━ 💰 TRADE INFO ━━━━━━
📌 Entry   : *{entry}*
🛑 SL      : *{sl}*  _(C2 extreme)_
🎯 TP      : *{tp}*  _(1:3)_
📊 RR      : *1:{rr}*
💼 Risk    : *1% — 0.5%*

━━━━━━ ⏰ SESSION ━━━━━━━━
{sess_emoji} *PURGING ACTIVE — {sess_name}*
{_clock_stamp()}
⭐ Rating  : *{rating}*
━━━━━━━━━━━━━━━━━━━━━━━━━━
_TET Community | GH3D1-TR4D35_
""".strip()

# ─── CLOCK TEXT ───────────────────────────────────────────────────────────────
def clock_text() -> str:
    active = get_active_session()
    active_line = (
        f"\n⚡ *PURGING HOUR ACTIVE!*\n"
        f"{active['emoji']} {active['session']}\n"
        f"🗽 NY: {active['ny_time']}  |  🇸🇪 SE: {active['se_time']}"
        if active else f"\n💤 No purging hour now.\n⏳ *Next:*\n{next_purge_text()}"
    )
    return f"""
🕐 *SESSION CLOCK*
🗽 NY (EST)  : *{ny_now().strftime('%I:%M %p')}*
🇸🇪 SE (CET)  : *{se_now().strftime('%H:%M')}*
🌐 UTC       : *{utc_now().strftime('%H:%M')}*

━━━━━━ 🔓 SESSION OPENS ━━━━━━
🌏 Asian  → NY: 6:00 PM  | SE: 00:00
🌍 London → NY: 2:00 AM  | SE: 08:00
🗽 NY     → NY: 9:30 AM  | SE: 15:30

━━━━━━ ⚡ PURGING HOURS ━━━━━━
🌏 Asian  → NY: 8–10pm  | SE: 02–04
🌍 London → NY: 2–3am   | SE: 08–09
🗽 NY     → NY: 8–10am  | SE: 14–16
{active_line}
""".strip()

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_active_session()
    banner = (
        f"\n⚡ *PURGING HOUR ACTIVE!*\n"
        f"{active['emoji']} {active['session']}  "
        f"NY: {active['ny_time']} | SE: {active['se_time']}\n"
        if active else "\n💤 No purging hour active now.\n"
    )
    await update.message.reply_text(
        f"🏆 *TET Alert Bot*{banner}\n👇 Xulo waxa aad rabto:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 New Trade Alert", callback_data="new_alert")],
            [InlineKeyboardButton("🕐 Session Clock",   callback_data="clock")],
            [InlineKeyboardButton("📖 Strategy Rules",  callback_data="rules")],
        ])
    )

# ─── BUTTON HANDLER ───────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "new_alert":
        active = get_active_session()
        note = (
            f"\n⚡ Purging active: {active['emoji']} {active['session']} "
            f"(NY {active['ny_time']} / SE {active['se_time']})"
            if active else ""
        )
        await query.edit_message_text(
            f"📊 *Step 1/6 — DAILY BIAS*{note}\n\nDaily bias-ka sidee yahay?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 BULLISH", callback_data="bias_BULLISH"),
                 InlineKeyboardButton("🔴 BEARISH", callback_data="bias_BEARISH")],
            ])
        )
        return BIAS

    elif query.data == "clock":
        await query.edit_message_text(
            clock_text(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="clock"),
                 InlineKeyboardButton("🔙 Back",    callback_data="back_start")]
            ])
        )

    elif query.data == "rules":
        await query.edit_message_text(
            "📋 *TET Strategy Rules*\n\n"
            "*Timeframes:* D → 4H → 1H → Entry\n\n"
            "*Checklist:*\n"
            "1️⃣ Daily Bias (Bullish/Bearish + DOL)\n"
            "2️⃣ 7H/Daily Profile confirmed\n"
            "3️⃣ 1H TSQ (Turtle Soup) — Purging Hours\n"
            "4️⃣ Model #1 (#1 candle) confirmed\n"
            "5️⃣ SMT/2SSMT (XAUUSD vs XAUEUR)\n"
            "6️⃣ Major LQ target known\n"
            "7️⃣ Invalidation level set (0.5 EQ)\n\n"
            "*Purging Hours (NY → Sweden):*\n"
            "🌏 Asian  : 8–10pm NY  →  02–04 SE\n"
            "🌍 London : 2–3am  NY  →  08–09 SE\n"
            "🗽 NY     : 8–10am NY  →  14–16 SE\n\n"
            "*Risk:* 1% or 0.5% | *RR:* 1:3\n\n"
            "_TET Community | GH3D1-TR4D35_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
            ])
        )

    elif query.data == "back_start":
        active = get_active_session()
        banner = (
            f"\n⚡ *PURGING HOUR ACTIVE!*\n"
            f"{active['emoji']} {active['session']}  "
            f"NY: {active['ny_time']} | SE: {active['se_time']}\n"
            if active else "\n💤 No purging hour active now.\n"
        )
        await query.edit_message_text(
            f"🏆 *TET Alert Bot*{banner}\n👇 Xulo waxa aad rabto:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 New Trade Alert", callback_data="new_alert")],
                [InlineKeyboardButton("🕐 Session Clock",   callback_data="clock")],
                [InlineKeyboardButton("📖 Strategy Rules",  callback_data="rules")],
            ])
        )

# ─── CONVERSATION STEPS ───────────────────────────────────────────────────────
async def bias_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["bias"] = query.data.replace("bias_", "")
    await query.edit_message_text(
        "📊 *Step 2/6 — SESSION*\n\nTrade-ka session kee?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌏 Asian  (8-10pm NY / 02-04 SE)",  callback_data="sess_Asian")],
            [InlineKeyboardButton("🌍 London (2-3am NY  / 08-09 SE)",  callback_data="sess_London")],
            [InlineKeyboardButton("🗽 NY     (8-10am NY / 14-16 SE)",  callback_data="sess_NewYork")],
        ])
    )
    return SESSION

async def session_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["session"] = query.data.replace("sess_", "")
    await query.edit_message_text(
        "📊 *Step 3/6 — DAILY PROFILE*\n\nProfile-ka muxuu yahay?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("5pm H/L", callback_data="prof_5pm H/L"),
             InlineKeyboardButton("9pm H/L", callback_data="prof_9pm H/L")],
            [InlineKeyboardButton("1am H/L", callback_data="prof_1am H/L"),
             InlineKeyboardButton("5am H/L", callback_data="prof_5am H/L")],
            [InlineKeyboardButton("9am H/L", callback_data="prof_9am H/L")],
        ])
    )
    return PROFILE

async def profile_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["profile"] = query.data.replace("prof_", "")
    await query.edit_message_text(
        "📊 *Step 4/6 — 1H TSQ*\n\n1H TSQ confirmed?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("TS Confirmed",  callback_data="tsq_TS Confirmed"),
             InlineKeyboardButton("Model-1 Only",  callback_data="tsq_Model-1 Only")],
            [InlineKeyboardButton("TS + Model-1",  callback_data="tsq_TS + Model-1")],
            [InlineKeyboardButton("Breaker Block", callback_data="tsq_Breaker Block")],
        ])
    )
    return TSQ

async def tsq_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tsq"] = query.data.replace("tsq_", "")
    await query.edit_message_text(
        "📊 *Step 5/6 — MODEL #1*\n\nModel #1 confirmed?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES",      callback_data="m1_YES"),
             InlineKeyboardButton("⚠️ Partial", callback_data="m1_Partial")],
        ])
    )
    return MODEL1

async def model1_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["model1"] = query.data.replace("m1_", "")
    await query.edit_message_text(
        "📊 *Step 6/6 — SMT*\n\nSMT (XAUUSD vs XAUEUR)?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Normal SMT",  callback_data="smt_Normal SMT"),
             InlineKeyboardButton("2-Stage SMT", callback_data="smt_2-Stage SMT")],
            [InlineKeyboardButton("PSP + SMT",   callback_data="smt_PSP + SMT"),
             InlineKeyboardButton("No SMT",      callback_data="smt_No SMT")],
        ])
    )
    return SMT

async def smt_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["smt"] = query.data.replace("smt_", "")
    await query.edit_message_text(
        "💰 *TRADE DETAILS*\n\nSymbol-ka geli (tusaale: XAUUSD):",
        parse_mode="Markdown"
    )
    return SYMBOL

async def symbol_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["symbol"] = update.message.text.upper().strip()
    await update.message.reply_text(
        "📌 Direction:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 BUY",  callback_data="dir_BUY"),
             InlineKeyboardButton("🔴 SELL", callback_data="dir_SELL")],
        ])
    )
    return DIRECTION

async def direction_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["direction"] = query.data.replace("dir_", "")
    await query.edit_message_text(
        "📌 *Entry price:*\nMisaalo: 2650.50", parse_mode="Markdown"
    )
    return ENTRY

async def entry_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["entry"] = update.message.text.strip()
    await update.message.reply_text("🛑 *Stop Loss:*\nMisaalo: 2640.00", parse_mode="Markdown")
    return SL

async def sl_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sl"] = update.message.text.strip()
    try:
        entry   = float(context.user_data["entry"])
        sl      = float(context.user_data["sl"])
        risk    = abs(entry - sl)
        d       = context.user_data.get("direction", "BUY")
        auto_tp = round(entry + risk * 3, 2) if d == "BUY" else round(entry - risk * 3, 2)
        await update.message.reply_text(
            f"🎯 *Take Profit:*\n_(1:3 auto-calc: *{auto_tp}*)_\n\nTP-ga geli:",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text("🎯 *Take Profit:*", parse_mode="Markdown")
    return TP

async def tp_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tp"] = update.message.text.strip()
    alert_msg = format_alert(context.user_data)
    await update.message.reply_text(alert_msg, parse_mode="Markdown")
    if CHAT_ID and CHAT_ID != "YOUR_CHAT_ID_HERE":
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text=alert_msg, parse_mode="Markdown")
            await update.message.reply_text("✅ Alert channel-ka loo diray!")
        except Exception as e:
            logger.error(f"Channel error: {e}")
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 Trade cusub: /start",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 New Alert", callback_data="new_alert")]
        ])
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ La joojiyay. /start")
    context.user_data.clear()
    return ConversationHandler.END

# ─── /alert QUICK COMMAND ─────────────────────────────────────────────────────
async def quick_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alert BUY 2650.00 2640.00 2680.00 [SYMBOL]"""
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "⚠️ *Usage:*\n`/alert BUY 2650.00 2640.00 2680.00`\n"
            "ama\n`/alert SELL 2650.00 2660.00 2620.00 XAUUSD`",
            parse_mode="Markdown"
        )
        return
    try:
        direction = args[0].upper()
        entry_p   = float(args[1])
        sl_p      = float(args[2])
        tp_p      = float(args[3])
        symbol    = args[4].upper() if len(args) > 4 else "XAUUSD"
        risk      = abs(entry_p - sl_p)
        reward    = abs(tp_p - entry_p)
        rr        = round(reward / risk, 2) if risk > 0 else "N/A"
        d_emoji   = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
        msg = f"""
╔══════════════════════════════╗
   📡 *TET ALERT BOT — QUICK*
╚══════════════════════════════╝

{d_emoji} | *{symbol}*

━━━━━━ 💰 TRADE INFO ━━━━━━
📌 Entry   : *{entry_p}*
🛑 SL      : *{sl_p}*
🎯 TP      : *{tp_p}*
📊 RR      : *1:{rr}*
💼 Risk    : *1% — 0.5%*

━━━━━━ ⏰ CLOCK ━━━━━━━━━━
{_clock_stamp()}
{_clock_line()}
━━━━━━━━━━━━━━━━━━━━━━━━━━
_TET Community | GH3D1-TR4D35_
""".strip()
        await update.message.reply_text(msg, parse_mode="Markdown")
        if CHAT_ID and CHAT_ID != "YOUR_CHAT_ID_HERE":
            await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ─── /clock & /purge ──────────────────────────────────────────────────────────
async def clock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(clock_text(), parse_mode="Markdown")

async def purge_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_active_session()
    if active:
        msg = (f"⚡ *PURGING HOUR ACTIVE!*\n\n"
               f"{active['emoji']} Session : *{active['session']}*\n"
               f"🗽 NY Time : *{active['ny_time']} EST*\n"
               f"🇸🇪 SE Time : *{active['se_time']} CET*\n\n"
               f"✅ Waa wakhtiga saxda ah ee trade-ka!")
    else:
        msg = (f"💤 *No purging hour active now.*\n\n"
               f"🗽 NY  : {ny_now().strftime('%I:%M %p')}\n"
               f"🇸🇪 SE  : {se_now().strftime('%H:%M')}\n\n"
               f"⏳ *Next purging hours:*\n{next_purge_text()}")
    await update.message.reply_text(msg, parse_mode="Markdown")

# ─── WEBHOOK SERVER (receives TradingView alerts) ─────────────────────────────
async def handle_webhook(request: web.Request) -> web.Response:
    """
    TradingView Pine alert POSTs JSON here.
    Gate: purge active + CISD confirmed + SMT/PSP present (rows 4,5,6).
    """
    try:
        payload = await request.json()
        logger.info(f"Webhook received: {payload}")
    except Exception as e:
        logger.error(f"Webhook JSON parse error: {e}")
        return web.Response(status=400, text="Bad JSON")

    cl = payload.get("checklist", {})
    purge_ok = cl.get("purge", False)
    smt_ok   = cl.get("smt",   False)
    model_ok = cl.get("model1", False)

    # Only fire when rows 4, 5, 6 are all checked
    if not (purge_ok and smt_ok and model_ok):
        logger.info("Webhook: signal filtered out (rows 4/5/6 not all met)")
        return web.Response(status=200, text="filtered")

    # Retrieve the bot from app context
    bot = request.app["bot"]
    msg = format_auto_signal(payload)

    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        logger.info("Auto signal sent to Telegram.")
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return web.Response(status=500, text="Telegram error")

    return web.Response(status=200, text="ok")

async def handle_health(request: web.Request) -> web.Response:
    """Railway health check endpoint."""
    return web.Response(status=200, text="TET Bot alive ✅")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    # Build the Telegram bot application
    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bias_step, pattern="^bias_")],
        states={
            SESSION:   [CallbackQueryHandler(session_step,   pattern="^sess_")],
            PROFILE:   [CallbackQueryHandler(profile_step,   pattern="^prof_")],
            TSQ:       [CallbackQueryHandler(tsq_step,       pattern="^tsq_")],
            MODEL1:    [CallbackQueryHandler(model1_step,    pattern="^m1_")],
            SMT:       [CallbackQueryHandler(smt_step,       pattern="^smt_")],
            SYMBOL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_step)],
            DIRECTION: [CallbackQueryHandler(direction_step, pattern="^dir_")],
            ENTRY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, entry_step)],
            SL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, sl_step)],
            TP:        [MessageHandler(filters.TEXT & ~filters.COMMAND, tp_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("alert", quick_alert))
    tg_app.add_handler(CommandHandler("clock", clock_command))
    tg_app.add_handler(CommandHandler("purge", purge_check))
    tg_app.add_handler(conv)
    tg_app.add_handler(CallbackQueryHandler(button_handler))

    # Build the aiohttp web app for the webhook
    web_app = web.Application()
    web_app["bot"] = tg_app.bot
    web_app.router.add_post(WEBHOOK_PATH, handle_webhook)
    web_app.router.add_get("/",           handle_health)
    web_app.router.add_get("/health",     handle_health)

    logger.info(f"🤖 TET Alert Bot starting — webhook on port {WEBHOOK_PORT}")

    # Run both: Telegram polling + aiohttp webhook server
    import asyncio

    async def run_all():
        # Start the aiohttp server
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()
        logger.info(f"🌐 Webhook server listening on port {WEBHOOK_PORT}")

        # Start Telegram polling
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        logger.info("✅ Telegram polling started")

        # Keep running
        try:
            await asyncio.Event().wait()
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
            await runner.cleanup()

    asyncio.run(run_all())

if __name__ == "__main__":
    main()
