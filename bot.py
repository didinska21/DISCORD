# bot.py
import os
import json
import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ================= ENV =================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID  = os.getenv("TELEGRAM_ADMIN_ID", "")  # user id Telegram kamu (angka)
DISCORD_USER_TOKEN = os.getenv("DISCORD_USER_TOKEN", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")           # untuk FOTO
DISCORD_CHANNEL_ID_LINK = os.getenv("DISCORD_CHANNEL_ID_LINK", "") # untuk LINK
SEND_INTERVAL_HOURS = float(os.getenv("SEND_INTERVAL_HOURS", "6"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("[!] TELEGRAM_BOT_TOKEN belum di-set")
if not DISCORD_USER_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("[!] DISCORD_USER_TOKEN & DISCORD_CHANNEL_ID wajib di-set")
if not DISCORD_CHANNEL_ID_LINK:
    print("[!] Peringatan: DISCORD_CHANNEL_ID_LINK belum di-set. Kirim link akan gagal jika tidak diisi atau tidak ada override per item.")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"
if not QUEUE_FILE.exists():
    QUEUE_FILE.write_text("[]", encoding="utf-8")


# =============== Queue helpers ===============
def load_queue():
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_queue(q):
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")


# =============== UI Buttons ===============
BTN_FOTO = "📷 Kirim Foto"
BTN_LINK = "🔗 Kirim Link"
BTN_BACK = "⬅️ Kembali"

BTN_START = "▶️ Start"
BTN_STOP = "⏸ Stop"

BTN_SET_INTERVAL = "⏱ Set Interval"
BTN_START_ALL = "▶️ Start All"
BTN_STOP_ALL  = "⏸ Stop All"

BTN_COUNT_ON  = "⏳ Countdown ON"
BTN_COUNT_OFF = "🕒 Countdown OFF"

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    # 2 kolom seperti contoh screenshot
    return ReplyKeyboardMarkup(
        [
            [BTN_FOTO, BTN_LINK],
            [BTN_SET_INTERVAL, BTN_COUNT_ON],
            [BTN_START_ALL, BTN_STOP_ALL],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )

def back_with_controls_keyboard() -> ReplyKeyboardMarkup:
    # submenu Foto/Link: ada Start/Stop + Countdown + Back
    return ReplyKeyboardMarkup(
        [
            [BTN_START, BTN_STOP],
            [BTN_COUNT_ON, BTN_COUNT_OFF],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )

def set_interval_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["30s", "1m"],
            ["5m", "1h"],
            ["6h", BTN_BACK],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


# =============== Global State ===============
class State:
    def __init__(self):
        self.interval_ms = int(SEND_INTERVAL_HOURS * 60 * 60 * 1000)
        self.running = False
        self.scheduler_task: asyncio.Task | None = None
        self.next_send_at: datetime | None = None
        self.watchers: dict[int, int] = {}  # chat_id -> message_id (countdown message id)
        self.spinner_frames = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        self.spinner_idx = 0
        # mode per chat: "" | "photo" | "link" | "set_interval"
        self.modes: dict[int, str] = {}

STATE = State()


# =============== Utils ===============
def is_admin(update: Update) -> bool:
    if not TELEGRAM_ADMIN_ID:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return str(uid) == str(TELEGRAM_ADMIN_ID)

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update):
        return True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="⛔ Kamu tidak punya akses perintah ini.")
    return False

def fmt_hhmmss(ms: int | None) -> str:
    if ms is None: return "--:--:--"
    if ms < 0: ms = 0
    s = ms // 1000
    hh = str(s // 3600).zfill(2)
    mm = str((s % 3600) // 60).zfill(2)
    ss = str(s % 60).zfill(2)
    return f"{hh}:{mm}:{ss}"

def progress_bar(remaining_ms: int | None, total_ms: int, width: int = 24) -> str:
    if remaining_ms is None or total_ms <= 0:
        return "▱" * width
    done = max(0, min(width, round(((total_ms - remaining_ms) / total_ms) * width)))
    left = width - done
    return "▰" * done + "▱" * left

def parse_duration_to_ms(s: str | None) -> int | None:
    if not s: return None
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smh])", s)
    if not m: return None
    num = int(m.group(1)); unit = m.group(2)
    return num*1000 if unit=='s' else num*60*1000 if unit=='m' else num*60*60*1000 if unit=='h' else None


# =============== Discord sender ===============
async def discord_send_image(buffer: bytes, filename: str, channel_id: str | None = None) -> None:
    channel_id = channel_id or DISCORD_CHANNEL_ID
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    tries = 0
    while True:
        tries += 1
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({"content": ""}), content_type="application/json")
        form.add_field("files[0]", buffer, filename=filename, content_type="application/octet-stream")

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                url,
                data=form,
                headers={"Authorization": DISCORD_USER_TOKEN, "User-Agent": "Mozilla/5.0"},
            ) as resp:
                if 200 <= resp.status < 300:
                    return
                if resp.status == 429:
                    data = await resp.json(content_type=None)
                    retry_after = float(data.get("retry_after", 3.0))
                    await asyncio.sleep(retry_after); continue
                if 500 <= resp.status < 600 and tries < 3:
                    await asyncio.sleep(1 * tries); continue
                text = await resp.text()
                raise RuntimeError(f"Discord error {resp.status}: {text}")

async def discord_send_message(content: str, channel_id: str | None = None) -> None:
    channel_id = channel_id or DISCORD_CHANNEL_ID_LINK
    if not channel_id:
        raise RuntimeError("DISCORD_CHANNEL_ID_LINK kosong dan tidak ada override 'to' di item.")
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    tries = 0
    while True:
        tries += 1
        payload = {"content": content}
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                url,
                json=payload,
                headers={"Authorization": DISCORD_USER_TOKEN, "User-Agent": "Mozilla/5.0"},
            ) as resp:
                if 200 <= resp.status < 300:
                    return
                if resp.status == 429:
                    data = await resp.json(content_type=None)
                    retry_after = float(data.get("retry_after", 3.0))
                    await asyncio.sleep(retry_after); continue
                if 500 <= resp.status < 600 and tries < 3:
                    await asyncio.sleep(1 * tries); continue
                text = await resp.text()
                raise RuntimeError(f"Discord error {resp.status}: {text}")


# =============== Processor ===============
async def process_one() -> bool:
    q = load_queue()
    if not q:
        return False
    item = q.pop(0)   # {path, ts, from}  |  {"type":"link","url":..., "ts":..., "to":...}
    try:
        # FOTO (format lama)
        if "path" in item and item.get("type") != "link":
            p = Path(item["path"])
            data = p.read_bytes()
            await discord_send_image(data, p.name, channel_id=item.get("to"))
            try: p.unlink(missing_ok=True)
            except Exception: pass
            save_queue(q)
            print(f"[✔] (foto) Terkirim ke Discord: {p.name}")
            return True

        # LINK (format baru)
        if item.get("type") == "link":
            url_text = item["url"]
            target_channel = item.get("to") or DISCORD_CHANNEL_ID_LINK
            await discord_send_message(url_text, channel_id=target_channel)
            save_queue(q)
            print(f"[✔] (link) Terkirim ke Discord: {url_text}")
            return True

        raise RuntimeError(f"Unknown queue item format: {item}")

    except Exception as e:
        print("[!] Gagal kirim:", e)
        q.insert(0, item); save_queue(q)
        return False

async def process_all_once() -> int:
    sent = 0
    while await process_one():
        sent += 1
    return sent


# =============== Scheduler (manual start/stop + dynamic interval) ===============
async def scheduler_loop():
    print("[▶] Scheduler STARTED")
    # kirim sekali saat start (konfirmasi kalau ada)
    await process_one()
    STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)

    try:
        while STATE.running:
            now = datetime.now(timezone.utc)
            if STATE.next_send_at is None:
                STATE.next_send_at = now + timedelta(milliseconds=STATE.interval_ms)
            sleep_ms = max(0, int((STATE.next_send_at - now).total_seconds() * 1000))
            await asyncio.sleep(sleep_ms / 1000.0)
            if not STATE.running:
                break
            await process_one()
            STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    finally:
        print("[⏸] Scheduler STOPPED")

def start_scheduler(app: Application) -> bool:
    if STATE.running:
        return False
    STATE.running = True
    STATE.scheduler_task = app.create_task(scheduler_loop())
    return True

def stop_scheduler() -> bool:
    if not STATE.running:
        return False
    STATE.running = False
    if STATE.scheduler_task:
        STATE.scheduler_task.cancel()
    STATE.scheduler_task = None
    STATE.next_send_at = None
    return True

def set_interval_ms(new_ms: int):
    STATE.interval_ms = new_ms
    if STATE.running:
        STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
        print(f"[🔁] Interval diubah saat running: {STATE.interval_ms} ms")
    else:
        print(f"[🛠] Interval diubah (paused): {STATE.interval_ms} ms")


# =============== Watch / animated countdown (tanpa JobQueue) ===============
def build_watch_text() -> str:
    q = load_queue()
    running = STATE.running
    total = STATE.interval_ms
    if running and STATE.next_send_at:
        eta_ms = int((STATE.next_send_at - datetime.now(timezone.utc)).total_seconds() * 1000)
    else:
        eta_ms = None
    bar = progress_bar(eta_ms, total, 24)
    STATE.spinner_idx = (STATE.spinner_idx + 1) % len(STATE.spinner_frames)
    spin = STATE.spinner_frames[STATE.spinner_idx]
    next_name = Path(q[0]["path"]).name if (q and "path" in q[0] and q[0].get("type") != "link") else ("(link)" if q else "(tidak ada item)")
    # Plain text
    return (
        f"{spin} Bot Status\n"
        f"State: {'Running' if running else 'Paused'}\n"
        f"Queue: {len(q)}\n"
        f"Next: {fmt_hhmmss(eta_ms)}\n"
        f"{bar}\n"
        f"Next item: {next_name}\n"
        f"/start_send, /stop_send, /set_interval 1m|6h, /sendnow"
    )

async def watch_loop(app: Application):
    while True:
        await asyncio.sleep(1.0)
        if not STATE.watchers:
            continue
        text = build_watch_text()
        for chat_id, message_id in list(STATE.watchers.items()):
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                )
            except Exception:
                STATE.watchers.pop(chat_id, None)

# helper: nyalakan countdown otomatis utk chat tertentu
async def ensure_countdown_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # jika belum ada message countdown, kirim baru
    if chat_id not in STATE.watchers:
        sent = await context.bot.send_message(chat_id=chat_id, text=build_watch_text())
        STATE.watchers[chat_id] = sent.message_id


# =============== Telegram Handlers ===============
URL_REGEX = re.compile(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', re.IGNORECASE)

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, welcome: bool = False):
    chat_id = update.effective_chat.id
    STATE.modes[chat_id] = ""  # di menu
    text = "Selamat datang! Bot antrian untuk kirim ke Discord.\n\nPilih menu:" if welcome else "Pilih menu:"
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=main_menu_keyboard())

# === MEDIA: Foto ===
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        mode = STATE.modes.get(chat_id, "")
        if mode != "photo":
            await context.bot.send_message(chat_id=chat_id, text="Kamu belum di mode 📷 Kirim Foto. Pilih menu dulu.")
            return
        if not update.message or not update.message.photo:
            return
        photo = update.message.photo[-1]  # resolusi tertinggi
        tg_file = await context.bot.get_file(photo.file_id)
        fname = f"{int(datetime.now().timestamp()*1000)}_{photo.file_id}.jpg"
        fpath = DATA_DIR / fname
        await tg_file.download_to_drive(custom_path=str(fpath))

        q = load_queue()
        q.append({"path": str(fpath), "ts": int(datetime.now().timestamp()), "from": update.effective_user.id})
        save_queue(q)

        await context.bot.send_message(chat_id=chat_id, text=f"✅ Foto diterima & masuk antrian.\nAntrian: {len(q)} item.")
        print(f"[+] Queue: {fname}")
    except Exception as e:
        print("[photo] error:", e)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Gagal memproses foto.")

# === MENU & TEKS (Link, Set Interval, Start/Stop, Countdown) ===
async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    mode = STATE.modes.get(chat_id, "")

    # Navigasi
    if text == BTN_BACK:
        await send_main_menu(update, context)
        return

    # Countdown ON/OFF (berlaku di semua mode)
    if text == BTN_COUNT_ON:
        sent = await context.bot.send_message(chat_id=chat_id, text=build_watch_text())
        STATE.watchers[chat_id] = sent.message_id
        return
    if text == BTN_COUNT_OFF:
        msg_id = STATE.watchers.pop(chat_id, None)
        if msg_id:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⏹ Countdown dimatikan.")
            except Exception:
                pass
        return

    # Menu utama
    if mode == "" and text == BTN_FOTO:
        STATE.modes[chat_id] = "photo"
        await context.bot.send_message(
            chat_id=chat_id,
            text="Mode 📷 Kirim Foto.\nKirim foto ke chat ini.\nTersedia: ▶️ Start, ⏸ Stop, ⏳ Countdown ON/OFF, ⬅️ Kembali.",
            reply_markup=back_with_controls_keyboard()
        ); return

    if mode == "" and text == BTN_LINK:
        STATE.modes[chat_id] = "link"
        await context.bot.send_message(
            chat_id=chat_id,
            text="Mode 🔗 Kirim Link.\nKirim satu/lebih URL dalam satu pesan.\nTersedia: ▶️ Start, ⏸ Stop, ⏳ Countdown ON/OFF, ⬅️ Kembali.",
            reply_markup=back_with_controls_keyboard()
        ); return

    if mode == "" and text == BTN_SET_INTERVAL:
        STATE.modes[chat_id] = "set_interval"
        await context.bot.send_message(
            chat_id=chat_id,
            text="Masukkan interval (contoh: 30s, 1m, 5m, 1h, 6h) atau pilih tombol:",
            reply_markup=set_interval_keyboard()
        ); return

    if mode == "" and text == BTN_START_ALL:
        q = load_queue()
        if not q:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Antrian kosong. Tambahkan foto atau link dulu.")
            return
        started = start_scheduler(context.application)
        if started:
            await ensure_countdown_on(update, context)  # AUTO COUNTDOWN ON
        await context.bot.send_message(chat_id=chat_id, text="✅ Pengiriman dimulai." if started else "ℹ️ Pengiriman sudah berjalan.")
        return

    if mode == "" and text == BTN_STOP_ALL:
        stopped = stop_scheduler()
        await context.bot.send_message(chat_id=chat_id, text="🛑 Pengiriman dihentikan." if stopped else "ℹ️ Sudah berhenti.")
        return

    # Submenu Foto/Link: Start/Stop
    if text == BTN_START:
        q = load_queue()
        if not q:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Antrian kosong. Tambahkan foto/link dulu.")
            return
        started = start_scheduler(context.application)
        if started:
            await ensure_countdown_on(update, context)  # AUTO COUNTDOWN ON
        await context.bot.send_message(chat_id=chat_id, text="✅ Pengiriman dimulai." if started else "ℹ️ Pengiriman sudah berjalan.")
        return

    if text == BTN_STOP:
        stopped = stop_scheduler()
        await context.bot.send_message(chat_id=chat_id, text="🛑 Pengiriman dihentikan." if stopped else "ℹ️ Sudah berhenti.")
        return

    # Mode Set Interval
    if mode == "set_interval":
        ms = parse_duration_to_ms(text)
        if not ms or ms < 10_000:
            await context.bot.send_message(chat_id=chat_id, text="Format salah/min 10s. Coba 30s / 1m / 5m / 1h / 6h atau ⬅️ Kembali.")
            return
        set_interval_ms(ms)
        await context.bot.send_message(chat_id=chat_id, text=f"⏱ Interval diubah ke {text} ({fmt_hhmmss(ms)}).")
        await send_main_menu(update, context)
        return

    # Mode Link: terima URL biasa
    if mode == "link":
        urls = URL_REGEX.findall(text)
        if not urls:
            await context.bot.send_message(chat_id=chat_id, text="Tidak ada URL terdeteksi. Coba lagi.")
            return
        q = load_queue()
        for u in urls:
            if u.lower().startswith("www."):
                u = "https://" + u
            q.append({"type": "link", "url": u, "ts": int(datetime.now().timestamp())})
        save_queue(q)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {len(urls)} link masuk antrian. Total: {len(q)}")
        return


# ====== Commands (opsional/legacy tetap disediakan) ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update, context, welcome=True)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else "unknown"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Your Telegram user id: {uid}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = load_queue()
    running = STATE.running
    eta_ms = int((STATE.next_send_at - datetime.now(timezone.utc)).total_seconds() * 1000) if (running and STATE.next_send_at) else None
    next_name = Path(q[0]["path"]).name if (q and "path" in q[0] and q[0].get("type") != "link") else ("(link)" if q else "(tidak ada)")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"Status\n• State: {'Running' if running else 'Paused'}"
            f"\n• Queue: {len(q)} item"
            f"\n• Next send in: {fmt_hhmmss(eta_ms)}"
            f"\n• Next item: {next_name}"
        ),
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = load_queue()
    if not q:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 Antrian kosong.")
        return
    for item in q:
        try:
            if item.get("type") == "link":
                to_chan = item.get("to") or DISCORD_CHANNEL_ID_LINK or "(belum di-set)"
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"🔗 LINK → {item['url']}\n→ target: {to_chan}"
                )
            elif "path" in item:
                p = Path(item["path"])
                if p.exists():
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=p.read_bytes())
            await asyncio.sleep(0.1)
        except Exception as e:
            print("[/list] gagal preview item:", e)

async def cmd_sendnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    count = await process_all_once()
    if STATE.running:
        STATE.next_send_at = datetime.now(timezone.utc) + timedelta(milliseconds=STATE.interval_ms)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"{'Terkirim ' + str(count) + ' item.' if count>0 else 'Tidak ada item di antrian.'}")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    q = load_queue()
    for it in q:
        try: 
            if "path" in it:
                Path(it["path"]).unlink(missing_ok=True)
        except Exception: 
            pass
    save_queue([])
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Antrian dikosongkan.")

async def cmd_watchstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    sent = await context.bot.send_message(chat_id=update.effective_chat.id, text=build_watch_text())
    STATE.watchers[update.effective_chat.id] = sent.message_id

async def cmd_watchstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    chat_id = update.effective_chat.id
    msg_id = STATE.watchers.pop(chat_id, None)
    if msg_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="Watch dihentikan.")
        except Exception: pass
    await context.bot.send_message(chat_id=chat_id, text="Watch dimatikan untuk chat ini.")

async def cmd_start_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    ok = start_scheduler(context.application)
    if ok:
        await ensure_countdown_on(update, context)  # AUTO COUNTDOWN ON
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Pengiriman dimulai." if ok else "Sudah berjalan.")

async def cmd_stop_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    ok = stop_scheduler()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Pengiriman dihentikan." if ok else "Memang sedang berhenti.")

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format: /set_interval 1m | 6h | 30s")
        return
    raw = context.args[0]
    ms = parse_duration_to_ms(raw)
    if not ms or ms < 10_000:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Format salah atau terlalu kecil (min 10s). Contoh: 1m, 6h, 30s")
        return
    set_interval_ms(ms)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"Interval diubah ke {raw} ({fmt_hhmmss(ms)}). {'(berlaku sekarang)' if STATE.running else '(aktif saat /start_send)'}")


# =============== Boot ===============
async def delete_webhook():
    async with aiohttp.ClientSession() as sess:
        await sess.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "true"},
        )

async def on_post_init(app: Application):
    # jalanin animasi countdown tiap 1 detik (tanpa JobQueue)
    app.create_task(watch_loop(app))

def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(on_post_init).build()

    # COMMANDS
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("sendnow",    cmd_sendnow))
    app.add_handler(CommandHandler("clear",      cmd_clear))
    app.add_handler(CommandHandler("watchstart", cmd_watchstart))  # legacy
    app.add_handler(CommandHandler("watchstop",  cmd_watchstop))   # legacy
    app.add_handler(CommandHandler("start_send", cmd_start_send))
    app.add_handler(CommandHandler("stop_send",  cmd_stop_send))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))

    # MEDIA FOTO
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    # MENU/TOMBOL & teks biasa (untuk link / set interval / kontrol)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))

    return app

def main():
    # matikan webhook dulu biar polling aman
    asyncio.run(delete_webhook())

    app = build_app()

    print("[ℹ] Bot siap. Gunakan /start untuk menampilkan menu.")
    print("[ℹ] Menu: Kirim Foto / Kirim Link / Set Interval / Start All / Stop All + Countdown.")

    # Python 3.12: pastikan ada event loop sebelum run_polling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # run_polling sinkron; mengelola loop sendiri
    app.run_polling()

if __name__ == "__main__":
    main()
