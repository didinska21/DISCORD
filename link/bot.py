import os, json, asyncio, aiohttp, re, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, Application, CommandHandler, MessageHandler, ContextTypes, filters

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True
)
logger = logging.getLogger("bot-link")

# ========== ENV ==========
load_dotenv()
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID     = os.getenv("TELEGRAM_ADMIN_ID", "")
DISCORD_USER_TOKEN    = os.getenv("DISCORD_USER_TOKEN", "")
DISCORD_CHANNEL_ID_LINK = os.getenv("DISCORD_CHANNEL_ID_LINK", "")
SEND_INTERVAL_HOURS   = float(os.getenv("SEND_INTERVAL_HOURS", "6"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("[!] TELEGRAM_BOT_TOKEN belum di-set")
if not DISCORD_USER_TOKEN or not DISCORD_CHANNEL_ID_LINK:
    raise SystemExit("[!] DISCORD_USER_TOKEN & DISCORD_CHANNEL_ID_LINK wajib di-set (link)")

# ========== DATA ==========
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"
if not QUEUE_FILE.exists():
    QUEUE_FILE.write_text("[]", encoding="utf-8")

def load_queue():
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("load_queue error: %s", e)
        return []

def save_queue(q):
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")

# ========== STATE ==========
class State:
    def __init__(self):
        self.interval_ms = int(SEND_INTERVAL_HOURS * 3600 * 1000)
        self.running = False
        self.scheduler_task: asyncio.Task | None = None
        self.next_send_at: datetime | None = None
        self.watchers: dict[int, int] = {}
        self.spinner = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']; self.si = 0
        self.grace_until: datetime | None = None

STATE = State()
context_app: Application | None = None

# ========== UTILS ==========
def is_admin(update: Update) -> bool:
    if not TELEGRAM_ADMIN_ID:
        return True
    try:
        return str(update.effective_user.id) == str(TELEGRAM_ADMIN_ID)
    except Exception:
        return False

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update): return True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="⛔ Tidak ada akses.")
    return False

def fmt_hhmmss(ms: int | None) -> str:
    if ms is None: return "--:--:--"
    if ms < 0: ms = 0
    s = ms // 1000
    return f"{s//3600:02}:{(s%3600)//60:02}:{s%60:02}"

def parse_duration_to_ms(s: str | None) -> int | None:
    if not s: return None
    m = re.fullmatch(r"(\d+)\s*([smh])", s.strip().lower())
    if not m: return None
    n = int(m.group(1)); u = m.group(2)
    return n*1000 if u=="s" else n*60*1000 if u=="m" else n*60*60*1000

def progress_bar(rem, total, width=20):
    if rem is None or total <= 0:
        return "▱" * width
    done = max(0, min(width, round(((total - rem) / total) * width)))
    return "▰" * done + "▱" * (width - done)

# ========== DISCORD ==========
async def discord_send_message(content: str):
    url = f"https://discord.com/api/v9/channels/{DISCORD_CHANNEL_ID_LINK}/messages"
    tries = 0
    while True:
        tries += 1
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json={"content": content},
                                 headers={"Authorization": DISCORD_USER_TOKEN, "User-Agent": "Mozilla/5.0"}) as resp:
                if 200 <= resp.status < 300:
                    logger.info("Discord OK %s", resp.status)
                    return
                if resp.status == 429:
                    data = await resp.json(content_type=None)
                    retry_after = float(data.get("retry_after", 3.0))
                    await asyncio.sleep(retry_after); continue
                if 500 <= resp.status < 600 and tries < 3:
                    await asyncio.sleep(tries); continue
                txt = await resp.text()
                logger.error("Discord error %s: %s", resp.status, txt)
                raise RuntimeError(f"Discord error {resp.status}: {txt}")

# ========== PROCESSOR ==========
async def _send_link_item(item):
    await discord_send_message(item["url"])

# PEEK-THEN-POP
async def process_tick() -> int:
    q = load_queue()
    if not q:
        return 0
    idx = next((i for i,it in enumerate(q) if it.get("type") == "link"), None)
    if idx is None:
        return 0
    item = q[idx]  # PEEK
    try:
        await _send_link_item(item)
        q.pop(idx); save_queue(q)  # sukses → baru pop
        logger.info("Sent 1 link. Queue left=%d", len(q))
        return 1
    except Exception as e:
        logger.exception("Send link failed: %s", e)
        return 0

async def process_all_once() -> int:
    sent = 0
    while True:
        n = await process_tick()
        if n == 0: break
        sent += n
    return sent

# ========== SCHEDULER ==========
async def _notify_finished_and_stop(app: Application):
    final_text = "✅ Semua link sudah terkirim.\nQueue kosong. Scheduler berhenti."
    for chat_id, msg_id in list(STATE.watchers.items()):
        try:
            await app.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=final_text)
        except Exception: pass
    STATE.watchers.clear()
    STATE.running = False
    if STATE.scheduler_task: STATE.scheduler_task.cancel()
    STATE.scheduler_task = None
    STATE.next_send_at = None
    STATE.grace_until = None

async def scheduler_loop():
    await process_tick()
    STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    try:
        while STATE.running:
            now = datetime.now(timezone.utc)
            if STATE.grace_until is not None:
                if load_queue():
                    STATE.grace_until = None
                    STATE.next_send_at = now + timedelta(milliseconds=STATE.interval_ms)
                else:
                    if now >= STATE.grace_until:
                        await _notify_finished_and_stop(context_app); break
                    STATE.next_send_at = STATE.grace_until
            if STATE.next_send_at is None:
                STATE.next_send_at = now + timedelta(milliseconds=STATE.interval_ms)
            sleep_s = max(0.0, (STATE.next_send_at - now).total_seconds())
            await asyncio.sleep(sleep_s)
            if not STATE.running: break
            _ = await process_tick()
            if not load_queue():
                if STATE.grace_until is None:
                    STATE.grace_until = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
                STATE.next_send_at = STATE.grace_until
            else:
                STATE.grace_until = None
                STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    finally:
        logger.info("Scheduler STOP")

def start_scheduler(app: Application) -> bool:
    global context_app
    if STATE.running: return False
    STATE.running = True
    context_app = app
    STATE.scheduler_task = app.create_task(scheduler_loop())
    return True

def stop_scheduler() -> bool:
    if not STATE.running: return False
    STATE.running = False
    if STATE.scheduler_task: STATE.scheduler_task.cancel()
    STATE.scheduler_task = None
    STATE.next_send_at = None
    STATE.grace_until = None
    return True

def set_interval_ms(ms: int):
    STATE.interval_ms = ms
    now = datetime.now(timezone.utc)
    if STATE.running:
        if STATE.grace_until is not None:
            STATE.grace_until = now + timedelta(milliseconds=STATE.interval_ms)
        STATE.next_send_at = now + timedelta(milliseconds=STATE.interval_ms)

# ========== UI ==========
BTN_LINK = "🔗 Kirim Link"
BTN_START = "▶️ Start"
BTN_STOP  = "⏸ Stop"
BTN_SET_INTERVAL = "⏱ Set Interval"
BTN_LIST = "📦 Lihat Antrian"
BTN_REFRESH = "🔄 Refresh"

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_LINK],[BTN_START, BTN_STOP],[BTN_SET_INTERVAL, BTN_LIST],[BTN_REFRESH]],
        resize_keyboard=True, is_persistent=True
    )

# ========== COUNTDOWN ==========
def build_watch_text() -> str:
    q = load_queue()
    running = STATE.running
    total = STATE.interval_ms
    if running and STATE.next_send_at is None:
        STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=total)
    eta_ms = None
    if STATE.next_send_at is not None:
        eta_ms = int((STATE.next_send_at - datetime.now(timezone.utc)).total_seconds()*1000)
    bar = progress_bar(eta_ms, total, 20)
    STATE.si = (STATE.si + 1) % len(STATE.spinner)
    spin = STATE.spinner[STATE.si]
    next_item = q[0]["url"] if q else "(kosong)"
    status_extra = " (grace)" if STATE.grace_until is not None else ""
    return (
        f"{spin} Bot Link Status{status_extra}\n"
        f"State: {'Running' if running else 'Paused'}\n"
        f"Queue: {len(q)}\n"
        f"Next: {fmt_hhmmss(eta_ms)}\n"
        f"{bar}\n"
        f"Next item: {next_item}"
    )

async def watch_loop(app: Application):
    while True:
        await asyncio.sleep(1)
        if not STATE.watchers: continue
        text = build_watch_text()
        for chat_id, msg_id in list(STATE.watchers.items()):
            try:
                await app.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
            except Exception:
                STATE.watchers.pop(chat_id, None)

async def ensure_countdown_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = build_watch_text()
    if chat_id not in STATE.watchers:
        sent = await context.bot.send_message(chat_id=chat_id, text=text)
        STATE.watchers[chat_id] = sent.message_id
    else:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=STATE.watchers[chat_id], text=text)
        except Exception:
            sent = await context.bot.send_message(chat_id=chat_id, text=text)
            STATE.watchers[chat_id] = sent.message_id

# ========== HANDLERS ==========
URL_REGEX = re.compile(r"(https?://[^\s]+|www\.[^\s]+)", re.IGNORECASE)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id,
        text=("Selamat datang di Bot Link.\n"
              "- Kirim URL ke chat ini.\n"
              "- Gunakan tombol di bawah atau command manual."),
        reply_markup=main_menu_keyboard()
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    urls = URL_REGEX.findall(txt)
    if not urls: return
    q = load_queue()
    for u in urls:
        if u.lower().startswith("www."): u = "https://" + u
        q.append({"type": "link", "url": u, "ts": int(datetime.now().timestamp())})
    save_queue(q)
    if STATE.grace_until is not None:
        STATE.grace_until = None
        if STATE.running:
            STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ {len(urls)} link ditambahkan. Antrian: {len(q)}")

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == BTN_LINK:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Mode kirim link aktif. Kirim URL ke chat ini.")
    elif txt == BTN_START: await cmd_start_send(update, context)
    elif txt == BTN_STOP: await cmd_stop_send(update, context)
    elif txt == BTN_SET_INTERVAL:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Gunakan: /set_interval 30s|1m|5m|1h")
    elif txt == BTN_LIST:
        q = load_queue()
        if not q:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 Antrian kosong.")
        else:
            preview = "\n".join([f"{i+1}. {it['url']}" for i,it in enumerate(q[:10])])
            more = f"\n...dan {len(q)-10} lagi" if len(q) > 10 else ""
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Daftar antrian ({len(q)} item):\n{preview}{more}")
    elif txt == BTN_REFRESH: await ensure_countdown_on(update, context)
    else: await on_text(update, context)

async def cmd_start_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not load_queue():
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Antrian kosong. Tambahkan link dulu."); return
    ok = start_scheduler(context.application)
    if ok: await ensure_countdown_on(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Pengiriman dimulai." if ok else "ℹ️ Sudah berjalan.")

async def cmd_stop_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    ok = stop_scheduler()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🛑 Pengiriman dihentikan." if ok else "ℹ️ Memang sudah berhenti.")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format: /set_interval 30s|1m|5m|1h"); return
    ms = parse_duration_to_ms(context.args[0])
    if not ms or ms < 10_000:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format salah / terlalu kecil (min 10s)."); return
    set_interval_ms(ms)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏱ Interval diubah ke {context.args[0]} ({fmt_hhmmss(ms)}).")

async def cmd_sendnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    count = await process_all_once()
    if not load_queue():
        if STATE.running:
            STATE.grace_until = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
            STATE.next_send_at = STATE.grace_until
    elif STATE.running:
        STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Terkirim {count} item.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_countdown_on(update, context)

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_countdown_on(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🔄 Refreshed.")

# ========== BOOT ==========
async def delete_webhook():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params={"drop_pending_updates": "true"}) as r:
            logger.info("deleteWebhook status=%s", r.status)

async def on_post_init(app: Application):
    app.create_task(watch_loop(app))

def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(on_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("start_send", cmd_start_send))
    app.add_handler(CommandHandler("stop_send", cmd_stop_send))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("sendnow", cmd_sendnow))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    return app

def main():
    asyncio.run(delete_webhook())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = build_app()
    logger.info("=== Bot Link BOOT ===")
    logger.info("Token OK, channel=%s, interval=%s ms", DISCORD_CHANNEL_ID_LINK, STATE.interval_ms)
    logger.info("Starting polling…")
    app.run_polling()

if __name__ == "__main__":
    main()
