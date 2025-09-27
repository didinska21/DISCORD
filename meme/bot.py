import os, json, asyncio, aiohttp, logging, random
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
logger = logging.getLogger("bot-meme")

# ========== ENV ==========
load_dotenv()
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID    = os.getenv("TELEGRAM_ADMIN_ID", "")
DISCORD_USER_TOKEN   = os.getenv("DISCORD_USER_TOKEN", "")
DISCORD_CHANNEL_ID   = os.getenv("DISCORD_CHANNEL_ID", "")
SEND_INTERVAL_HOURS  = float(os.getenv("SEND_INTERVAL_HOURS", "6"))

# Optional override via ENV (menit)
JITTER_MIN_MINUTES   = int(os.getenv("JITTER_MIN_MINUTES", "1"))
JITTER_MAX_MINUTES   = int(os.getenv("JITTER_MAX_MINUTES", "30"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("[!] TELEGRAM_BOT_TOKEN belum di-set")
if not DISCORD_USER_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("[!] DISCORD_USER_TOKEN & DISCORD_CHANNEL_ID wajib di-set (meme)")

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
        self.spinner = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']; self.si=0
        self.grace_until: datetime | None = None  # nunggu 1 interval setelah queue kosong

        # New: phase & jitter
        # phase: 'jitter' | 'interval' | None (None bila idle/baru start/masuk grace)
        self.phase: str | None = None
        self.jitter_until: datetime | None = None

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
    import re
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

def rand_jitter_ms() -> int:
    lo = max(1, JITTER_MIN_MINUTES)
    hi = max(lo, JITTER_MAX_MINUTES)
    return random.randint(lo*60_000, hi*60_000)

# ========== DISCORD ==========
async def discord_send_image(buf: bytes, filename: str):
    url = f"https://discord.com/api/v9/channels/{DISCORD_CHANNEL_ID}/messages"
    tries = 0
    while True:
        tries += 1
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({"content": ""}), content_type="application/json")
        form.add_field("files[0]", buf, filename=filename, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, data=form, headers={"Authorization": DISCORD_USER_TOKEN}) as resp:
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
async def _send_photo_item(item):
    p = Path(item["path"])
    data = p.read_bytes()
    await discord_send_image(data, p.name)
    try: p.unlink(missing_ok=True)
    except: pass

# PEEK-THEN-POP: anti hilang
async def process_tick() -> int:
    q = load_queue()
    if not q:
        return 0
    idx = next((i for i,it in enumerate(q) if "path" in it), None)
    if idx is None:
        return 0
    item = q[idx]  # PEEK (jangan pop dulu)
    try:
        await _send_photo_item(item)         # kirim
        q.pop(idx); save_queue(q)            # sukses → pop & simpan
        logger.info("Sent 1 photo. Queue left=%d", len(q))
        return 1
    except Exception as e:
        logger.exception("Send photo failed: %s", e)
        # gagal → queue biarkan utuh
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
    final_text = "✅ Semua foto sudah terkirim.\nQueue kosong. Scheduler berhenti."
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
    STATE.phase = None
    STATE.jitter_until = None

def _enter_grace(now: datetime):
    STATE.grace_until = now + timedelta(milliseconds=STATE.interval_ms)
    STATE.next_send_at = STATE.grace_until
    STATE.phase = None
    STATE.jitter_until = None

def _schedule_jitter(now: datetime):
    jms = rand_jitter_ms()
    STATE.jitter_until = now + timedelta(milliseconds=jms)
    STATE.next_send_at = STATE.jitter_until
    STATE.phase = 'jitter'

def _schedule_interval(now: datetime):
    STATE.next_send_at = now + timedelta(milliseconds=STATE.interval_ms)
    STATE.phase = 'interval'
    STATE.jitter_until = None

async def scheduler_loop():
    # Start dengan JITTER lebih dulu (jika ada queue)
    now = datetime.now(timezone.utc)
    if load_queue():
        _schedule_jitter(now)
    else:
        # fallback: kalau kosong (padahal cmd_start_send sudah cek),
        # tetap masuk grace agar konsisten
        _enter_grace(now)

    try:
        while STATE.running:
            now = datetime.now(timezone.utc)

            # Fase grace: tunggu 1 interval setelah kosong
            if STATE.grace_until is not None:
                if load_queue():
                    # ada item baru → batalkan grace
                    STATE.grace_until = None
                    # Setelah ada item baru, alurnya: interval dulu atau jitter?
                    # Sesuai behavior lama, saat ada item di tengah grace → reset delay utama dulu
                    _schedule_interval(now)
                else:
                    if now >= STATE.grace_until:
                        await _notify_finished_and_stop(context_app); break
                    STATE.next_send_at = STATE.grace_until

            # Pastikan next_send_at ada
            if STATE.next_send_at is None:
                # default aman: bila running dan tidak di grace, mulai dari jitter
                _schedule_jitter(now)

            # Tunggu sampai next_send_at
            sleep_s = max(0.0, (STATE.next_send_at - now).total_seconds())
            await asyncio.sleep(sleep_s)
            if not STATE.running: break

            # Eksekusi sesuai phase
            now = datetime.now(timezone.utc)

            if STATE.phase == 'jitter':
                # Saat jitter habis → kirim 1 item (kalau masih ada)
                if not load_queue():
                    _enter_grace(now)
                    continue

                _ = await process_tick()

                if not load_queue():
                    # Habis kirim ternyata kosong → masuk grace (tetap kasih delay terakhir)
                    _enter_grace(now)
                else:
                    # Setelah kirim → delay utama
                    _schedule_interval(now)

            elif STATE.phase == 'interval':
                # Delay selesai → masuk jitter sebelum kirim berikutnya
                _schedule_jitter(now)

            else:
                # Unknown/None: fallback ke jitter
                _schedule_jitter(now)

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
    STATE.phase = None
    STATE.jitter_until = None
    return True

def set_interval_ms(ms: int):
    STATE.interval_ms = ms
    now = datetime.now(timezone.utc)
    if STATE.running:
        if STATE.grace_until is not None:
            STATE.grace_until = now + timedelta(milliseconds=STATE.interval_ms)
            STATE.next_send_at = STATE.grace_until
        else:
            # kalau sedang di interval, reset ke interval baru; kalau di jitter, biarkan jitter selesai
            if STATE.phase == 'interval':
                _schedule_interval(now)

# ========== UI ==========
BTN_FOTO = "📷 Kirim Foto"
BTN_START = "▶️ Start"
BTN_STOP  = "⏸ Stop"
BTN_SET_INTERVAL = "⏱ Set Interval"
BTN_LIST = "📦 Lihat Antrian"
BTN_REFRESH = "🔄 Refresh"

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_FOTO],[BTN_START, BTN_STOP],[BTN_SET_INTERVAL, BTN_LIST],[BTN_REFRESH]],
        resize_keyboard=True, is_persistent=True
    )

# ========== COUNTDOWN ==========
def build_watch_text() -> str:
    q = load_queue()
    running = STATE.running
    total = STATE.interval_ms

    # Pastikan next_send_at selalu ada saat running/grace
    if running and STATE.next_send_at is None:
        # default aman
        STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=total)

    eta_ms = None
    if STATE.next_send_at is not None:
        eta_ms = int((STATE.next_send_at - datetime.now(timezone.utc)).total_seconds()*1000)

    # Progress bar: pilih total sesuai phase
    pb_total = (
        total if (STATE.phase == 'interval' and STATE.grace_until is None)
        else max(1, (STATE.next_send_at - datetime.now(timezone.utc)).total_seconds()*1000) if STATE.phase == 'jitter'
        else total
    )
    try:
        pb_total = int(pb_total)
    except:
        pb_total = total

    bar = progress_bar(eta_ms, pb_total, 20)
    STATE.si = (STATE.si + 1) % len(STATE.spinner)
    spin = STATE.spinner[STATE.si]
    next_name = Path(q[0]["path"]).name if q else "(kosong)"
    if STATE.grace_until is not None:
        status_extra = " (delay terakhir sebelum stop)"
    elif STATE.phase == 'jitter':
        status_extra = " (tunggu 1-30 menit sebelum kirim)"
    else:
        status_extra = ""

    return (
        f"{spin} Bot Meme Status{status_extra}\n"
        f"State: {'Running' if running else 'Paused'}\n"
        f"Queue: {len(q)}\n"
        f"Next: {fmt_hhmmss(eta_ms)}\n"
        f"{bar}\n"
        f"Next item: {next_name}"
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
            # kalau gagal edit (misal message dihapus), buat baru
            sent = await context.bot.send_message(chat_id=chat_id, text=text)
            STATE.watchers[chat_id] = sent.message_id

# ========== HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=("Selamat datang di Bot Meme.\n"
              "- Kirim foto ke chat ini.\n"
              "- Gunakan tombol di bawah atau command manual."),
        reply_markup=main_menu_keyboard()
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo: return
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    fname = f"{int(datetime.now().timestamp()*1000)}_{photo.file_unique_id}.jpg"
    fpath = DATA_DIR / fname
    await tg_file.download_to_drive(str(fpath))
    q = load_queue(); q.append({"path": str(fpath), "ts": int(datetime.now().timestamp())}); save_queue(q)
    # jika ada item baru saat grace → batalkan grace & reset alur: delay utama dulu
    if STATE.grace_until is not None:
        STATE.grace_until = None
        if STATE.running:
            _schedule_interval(datetime.now(timezone.utc))
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Foto ditambahkan. Antrian: {len(q)}")

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == BTN_FOTO:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Mode kirim foto aktif. Kirim foto ke chat ini.")
    elif txt == BTN_START:
        await cmd_start_send(update, context)
    elif txt == BTN_STOP:
        await cmd_stop_send(update, context)
    elif txt == BTN_SET_INTERVAL:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Gunakan: /set_interval 30s|1m|5m|1h")
    elif txt == BTN_LIST:
        q = load_queue()
        if not q:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 Antrian kosong.")
        else:
            preview = "\n".join([f"{i+1}. {Path(it['path']).name}" for i,it in enumerate(q[:10])])
            more = f"\n...dan {len(q)-10} lagi" if len(q) > 10 else ""
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Daftar antrian ({len(q)} item):\n{preview}{more}")
    elif txt == BTN_REFRESH:
        await ensure_countdown_on(update, context)
    # selain itu: abaikan (meme bot tidak parse URL)

async def cmd_start_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not load_queue():
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Antrian kosong. Tambahkan foto dulu.")
        return
    ok = start_scheduler(context.application)
    if ok: await ensure_countdown_on(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Pengiriman dimulai (dengan jitter 1–30 menit sebelum kirim pertama)." if ok else "ℹ️ Sudah berjalan.")

async def cmd_stop_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    ok = stop_scheduler()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🛑 Pengiriman dihentikan." if ok else "ℹ️ Memang sudah berhenti.")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format: /set_interval 30s|1m|5m|1h")
        return
    ms = parse_duration_to_ms(context.args[0])
    if not ms or ms < 10_000:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format salah / terlalu kecil (min 10s).")
        return
    set_interval_ms(ms)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏱ Interval diubah ke {context.args[0]} ({fmt_hhmmss(ms)}).")

async def cmd_sendnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    # /sendnow tetap bypass jitter dan kirim semua sekali jalan
    count = await process_all_once()
    if not load_queue():
        # kosong → mulai grace, jangan langsung stop
        if STATE.running:
            STATE.grace_until = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
            STATE.next_send_at = STATE.grace_until
            STATE.phase = None
            STATE.jitter_until = None
    elif STATE.running:
        # setelah manual kirim, siklus normal: delay utama dulu
        _schedule_interval(datetime.now(timezone.utc))
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
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    return app

def main():
    asyncio.run(delete_webhook())
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    app = build_app()
    logger.info("=== Bot Meme BOOT ===")
    logger.info("Token OK, channel=%s, interval=%s ms", DISCORD_CHANNEL_ID, STATE.interval_ms)
    logger.info("Starting polling…")
    app.run_polling()

if __name__ == "__main__":
    main()
