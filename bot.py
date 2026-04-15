import asyncio
import json
import logging
import os
import re
import html
import aiohttp
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# Load env variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TOPIC_ID = int(os.getenv("TOPIC_ID", "0")) or None
ADMIN_IDS = set()
_admin_raw = os.getenv("ADMIN_ID", "")
for _id in _admin_raw.split(","):
    _id = _id.strip()
    if _id:
        ADMIN_IDS.add(_id)

def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

GRAFANA_URL = os.getenv("GRAFANA_URL", "https://bi.cinerama.uz/d/b4233477-01c3-4642-9c4e-1077f48bb7d1/tariffs?orgId=1&from=now%2FM&to=now%2FM&timezone=browser&var-tariff=$__all")
GRAFANA_USER = os.getenv("GRAFANA_USER", "")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "")

if not BOT_TOKEN or not CHAT_ID:
    print("Please set BOT_TOKEN and CHAT_ID in the .env file.")
    exit(1)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

DATA_FILE = Path("data.json")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# PostgreSQL setup
if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    
    # Fix Heroku's postgres:// -> postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    def _get_db_conn():
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    
    def _init_db():
        conn = _get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_data (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        cur.execute("INSERT INTO bot_data (id, data) VALUES (1, '{}'::jsonb) ON CONFLICT (id) DO NOTHING")
        conn.commit()
        cur.close()
        conn.close()
    
    try:
        _init_db()
        logging.info("PostgreSQL database initialized.")
    except Exception as e:
        logging.error(f"Failed to init PostgreSQL: {e}")
    
    def load_data():
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute("SELECT data FROM bot_data WHERE id = 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            return row[0] if row else {}
        except Exception as e:
            logging.error(f"DB load error: {e}")
            return {}
    
    def save_data(data):
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE bot_data SET data = %s WHERE id = 1",
                (psycopg2.extras.Json(data),)
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"DB save error: {e}")

else:
    # Fallback to file-based storage for local development
    def load_data():
        if not DATA_FILE.exists():
            return {}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_data(data):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

def get_today_str():
    return datetime.now().strftime("%Y-%m-%d")

import html
import re

def parse_jira_links(text):
    safe_text = html.escape(text)
    jira_base = "https://cineramauzb.atlassian.net/jira/software/projects/CDT/boards/201?selectedIssue="
    
    # Normalize: 'CDT 380', 'CDT380' → 'CDT-380'
    safe_text = re.sub(r'CDT\s+(\d+)', r'CDT-\1', safe_text, flags=re.IGNORECASE)
    safe_text = re.sub(r'CDT(\d+)', r'CDT-\1', safe_text, flags=re.IGNORECASE)
    
    lines = safe_text.split('\n')
    formatted_lines = []
    current_section = None
    
    for line in lines:
        lower_line = line.lower()
        if 'yesterday' in lower_line or 'вчера' in lower_line:
            current_section = 'yesterday'
        elif 'today' in lower_line or 'сегодня' in lower_line:
            current_section = 'today'
        elif 'blocked' in lower_line or 'блокер' in lower_line:
            current_section = 'blocked'
            
        if current_section == 'yesterday':
            line = re.sub(r'(CDT-\d+)(\)?)', rf'<a href="{jira_base}\1">\1</a>\2 ✅', line)
        elif current_section == 'today':
            line = re.sub(r'(CDT-\d+)(\)?)', rf'<a href="{jira_base}\1">\1</a>\2 ⏳', line)
        else:
            line = re.sub(r'(CDT-\d+)', rf'<a href="{jira_base}\1">\1</a>', line)
            
        formatted_lines.append(line)
        
    return '\n'.join(formatted_lines)

async def validate_standup_with_ai(text):
    """Use AI to validate if standup contains real task descriptions."""
    if not OPENROUTER_API_KEY:
        return None  # Skip AI validation if no key
    
    prompt = (
        "You are a standup report validator for a software development team (Cinerama). "
        "Your job is to ensure employees write REAL task descriptions, but be reasonable—accept both technical and management/PM tasks.\n\n"
        "REJECT if ANY of the following:\n"
        "- Sections are empty (just 'Yesterday:' / 'Today:' with no real content)\n"
        "- Content is test/fake text (like 'test', 'asdf', 'abc', '123', 'xxx', random characters)\n"
        "- Content is meaningless filler ('...', '---', 'nothing', 'n/a', single characters/words)\n"
        "- Ticket numbers are obviously fake (e.g. CDT-123123123123)\n\n"
        "ACCEPT if the report contains any of the following:\n"
        "- Real technical tasks ('fixed bug', 'deployed app', 'CDT-344')\n"
        "- Real PM/Management/HR tasks (e.g., 'Fully documentation', 'Search for candidates', 'Monitor dev-team', 'Discuss tasks')\n"
        "- Real design tasks (e.g., 'List designer tasks', 'UX/UI updates')\n"
        "If a person wrote actual sentences about work like meetings, docs, or hiring, it is VALID. Do not be overly strict about 'high-level' phrasing.\n\n"
        f"Standup report:\n{text}\n\n"
        "Respond ONLY in this exact JSON format, no other text:\n"
        '{"valid": true} or {"valid": false, "reason": "brief explanation in English"}'
    )
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "google/gemini-2.0-flash-001",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logging.warning(f"AI validation API error: {resp.status}")
                    return None
                result = await resp.json()
                content = result["choices"][0]["message"]["content"].strip()
                # Parse JSON from response
                content = re.sub(r'^```json\s*', '', content)
                content = re.sub(r'\s*```$', '', content)
                return json.loads(content)
    except Exception as e:
        logging.warning(f"AI validation failed: {e}")
        return None  # Allow standup if AI is unavailable

async def screenshot_grafana():
    """Take a screenshot of the Grafana dashboard using Playwright."""
    if not GRAFANA_USER or not GRAFANA_PASS:
        logging.warning("GRAFANA_USER or GRAFANA_PASS not set")
        return None
    
    try:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1600, "height": 900},
                ignore_https_errors=True
            )
            page = await context.new_page()
            
            # Login to Grafana
            login_url = GRAFANA_URL.split('/d/')[0] + '/login'
            await page.goto(login_url, wait_until="networkidle", timeout=30000)
            
            await page.fill('input[name="user"]', GRAFANA_USER)
            await page.fill('input[name="password"]', GRAFANA_PASS)
            await page.click('button[type="submit"]')
            
            # Wait for login to complete
            await page.wait_for_timeout(3000)
            
            # Navigate to dashboard
            await page.goto(GRAFANA_URL, wait_until="networkidle", timeout=60000)
            
            # Wait for charts to render
            await page.wait_for_timeout(8000)
            
            # Take screenshot
            screenshot = await page.screenshot(full_page=True, type="png")
            
            await browser.close()
            return screenshot
    except Exception as e:
        logging.error(f"Screenshot failed: {e}")
        return None

admin_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Stop Pinging", callback_data="stop_ping"), InlineKeyboardButton(text="▶️ Start Pinging", callback_data="start_ping")],
        [InlineKeyboardButton(text="📢 Ping Now", callback_data="ping_now"), InlineKeyboardButton(text="📋 Send Summary", callback_data="send_summary_now")],
        [InlineKeyboardButton(text="🚨 Send 12:00 Report", callback_data="send_noon_report"), InlineKeyboardButton(text="📸 Dashboard", callback_data="screenshot_dashboard")],
        [InlineKeyboardButton(text="👥 Employee List", callback_data="list_emp"), InlineKeyboardButton(text="➕ Add Employee", callback_data="add_emp")],
        [InlineKeyboardButton(text="➖ Remove Employee", callback_data="rem_emp"), InlineKeyboardButton(text="📁 Report History", callback_data="history_list")],
        [InlineKeyboardButton(text="✏️ Edit Reports", callback_data="edit_list"), InlineKeyboardButton(text="🗑 Clear Report", callback_data="clear_list")]
    ]
)

# State for admin edit flow
admin_edit_state = {}

def get_employees():
    data = load_data()
    emps = data.get("employees", {})
    if isinstance(emps, list):
        emps = {u: "Developer" for u in emps}
    return emps

def get_user_map():
    """Returns the username -> user_id mapping."""
    data = load_data()
    return data.get("user_map", {})

def save_user_mapping(username, user_id):
    """Saves a username -> user_id mapping for proper mentions."""
    data = load_data()
    if "user_map" not in data:
        data["user_map"] = {}
    data["user_map"][username.lower()] = str(user_id)
    save_data(data)

def build_mention(username, user_map):
    """Build a proper HTML mention. Uses tg://user?id= if we know the user_id, otherwise falls back to @username."""
    uid = user_map.get(username.lower())
    if uid:
        return f'<a href="tg://user?id={uid}">@{username}</a>'
    return f"@{username}"

def add_employee(username, role):
    data = load_data()
    if "employees" not in data or isinstance(data["employees"], list):
        data["employees"] = {}
    data["employees"][username] = role
    save_data(data)

def remove_employee(username):
    data = load_data()
    if "employees" in data and isinstance(data["employees"], dict):
        if username in data["employees"]:
            del data["employees"][username]
    elif "employees" in data and isinstance(data["employees"], list):
        if username in data["employees"]:
            data["employees"].remove(username)
    save_data(data)

async def send_standup_prompt():
    """Sends the daily standup reminder to the team chat."""
    emps = get_employees()
    user_map = get_user_map()
    mentions = " ".join([build_mention(u, user_map) for u in emps]) if emps else ""
    text = (
        f"🌅 <b>Daily Standup</b>\n\n"
        f"{mentions}\n"
        f"Please reply to this message with your plan:\n\n"
        f"✅ Yesterday: what did you finish? (ticket number)\n"
        f"🎯 Today: what will you complete? (ticket number)\n"
        f"🚧 Blocked: yes/no — if yes, what do you need?"
    )
    prompt_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Append to your Standup", callback_data="append_standup_btn")]
        ]
    )
    try:
        msg = await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", reply_markup=prompt_keyboard, message_thread_id=TOPIC_ID)
        # Save the message ID loosely
        data = load_data()
        today = get_today_str()
        if today not in data:
            data[today] = {}
        data[today]["prompt_message_id"] = msg.message_id
        data["pinging_paused"] = False  # Reset on a new day
        save_data(data)
    except Exception as e:
        logging.error(f"Failed to send prompt: {e}")

@dp.message(Command("add"), F.chat.type == "private")
async def cmd_add_emp(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply(f"⛔️ Access denied. Your Telegram ID: {message.from_user.id}\n(Add it as ADMIN_ID in .env to manage the list)")
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Usage: /add @username [Role]\nExample: /add @iva_nov Frontend")
        return
    username = args[0].lstrip('@').lower()
    role = " ".join(args[1:]) if len(args) > 1 else "Developer"
    add_employee(username, role)
    await message.reply(f"✅ User @{username} has been added with role: {role}.")

@dp.message(Command("remove"), F.chat.type == "private")
async def cmd_remove_emp(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Usage: /remove @username")
        return
    username = args[0].lstrip('@').lower()
    remove_employee(username)
    await message.reply(f"❌ User @{username} has been removed from the list.")

@dp.message(Command("list"), F.chat.type == "private")
async def cmd_list_emp(message: Message):
    if not is_admin(message.from_user.id):
        return
    emps = get_employees()
    if not emps:
        await message.reply("Employee list is empty.")
        return
    text = "👥 <b>Employee List:</b>\n" + "\n".join([f"- @{e} ({r})" for e, r in emps.items()])
    await message.reply(text, parse_mode="HTML")

from aiogram.types import BotCommandScopeChat

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=message.chat.id))
    except Exception:
        pass

    if not is_admin(message.from_user.id):
        await message.reply(f"⛔️ Access denied. Your Telegram ID: {message.from_user.id}\n(Add it as ADMIN_ID in .env)", reply_markup=ReplyKeyboardRemove())
        return
    
    # Remove old keyboard by sending a quick message
    msg = await message.answer("🔄 Updating interface...", reply_markup=ReplyKeyboardRemove())
    await msg.delete()
    
    data = load_data()
    now_hour = datetime.now().hour
    if now_hour >= 18 or now_hour < 9:
        status_text = "OFF HOURS 🌙"
    else:
        status_text = "STOPPED ⏸" if data.get("pinging_paused", False) else "ACTIVE ▶️"
    
    await message.reply(f"Control Panel (Pinging: <b>{status_text}</b>):", reply_markup=admin_keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "list_emp")
async def cb_list_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    emps = get_employees()
    if not emps:
        await callback.message.reply("Employee list is empty.")
    else:
        text = "👥 <b>Employee List:</b>\n" + "\n".join([f"- @{e} ({r})" for e, r in emps.items()])
        await callback.message.reply(text, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "add_emp")
async def cb_add_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.reply(
        "📝 <b>How to add an employee?</b>\n\n"
        "Type the <code>/add</code> command, then their Telegram username (with @) and (optionally) their role.\n\n"
        "<i>Example:</i>\n"
        "/add @ivan Frontend\n"
        "/add @anna QA",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "rem_emp")
async def cb_rem_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.reply(
        "🗑 <b>How to remove an employee?</b>\n\n"
        "Type the <code>/remove</code> command and the employee's username.\n\n"
        "<i>Example:</i>\n"
        "/remove @ivan",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "stop_ping")
async def cb_stop_ping(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    data = load_data()
    data["pinging_paused"] = True
    save_data(data)
    now_hour = datetime.now().hour
    status_text = "OFF HOURS 🌙 (paused ⏸)" if now_hour >= 18 or now_hour < 9 else "STOPPED ⏸"
    try:
        await callback.message.edit_text(f"Control Panel (Pinging: <b>{status_text}</b>):", reply_markup=admin_keyboard, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Pinging stopped")

@dp.callback_query(F.data == "start_ping")
async def cb_start_ping(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    data = load_data()
    data["pinging_paused"] = False
    save_data(data)
    now_hour = datetime.now().hour
    status_text = "OFF HOURS 🌙 (enabled ▶️)" if now_hour >= 18 or now_hour < 9 else "ACTIVE ▶️"
    try:
        await callback.message.edit_text(f"Control Panel (Pinging: <b>{status_text}</b>):", reply_markup=admin_keyboard, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Pinging started")

@dp.callback_query(F.data == "ping_now")
async def cb_ping_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("📢 Pinging now...")
    await check_missing_standups(force=True)

@dp.callback_query(F.data == "send_summary_now")
async def cb_send_summary_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("📋 Sending summary...")
    await auto_send_summary()

@dp.callback_query(F.data == "send_noon_report")
async def cb_send_noon_report(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("🚨 Sending 12:00 report to admins...")
    await report_missing_standups_at_noon(force=True)

@dp.callback_query(F.data == "screenshot_dashboard")
async def cb_screenshot_dashboard(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("📸 Taking screenshot... Please wait ~15 sec")
    
    screenshot = await screenshot_grafana()
    
    if screenshot:
        from aiogram.types import BufferedInputFile
        photo = BufferedInputFile(screenshot, filename="dashboard.png")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=photo,
                    caption=f"📊 <b>Grafana Dashboard</b>\n{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to send screenshot to {admin_id}: {e}")
    else:
        await callback.message.reply("❌ Failed to take screenshot. Check logs.")

@dp.callback_query(F.data == "history_list")
async def cb_history_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    data = load_data()
    dates = []
    for k in data.keys():
        if re.match(r"^\d{4}-\d{2}-\d{2}$", k):
            dates.append(k)
    dates.sort(reverse=True)
    if not dates:
        await callback.message.reply("Report history is empty.")
        await callback.answer()
        return
    
    # We will show at most the last 30 days
    kb_buttons = []
    for d in dates[:30]:
        kb_buttons.append([InlineKeyboardButton(text=f"📅 {d}", callback_data=f"show_hist_{d}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.reply("Select a date to view the report:", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("show_hist_"))
async def cb_show_hist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    date_str = callback.data.replace("show_hist_", "")
    data = load_data()
    
    if date_str not in data or "replies" not in data[date_str] or not data[date_str]["replies"]:
        await callback.message.reply(f"No reports found for {date_str}.")
        await callback.answer()
        return

    emps = get_employees()
    summary_text = [f"📋 <b>Standup Summary for {date_str}</b>\n"]
    
    for uid, info in data[date_str]["replies"].items():
        uname = (info.get("username") or "").lower()
        role = emps.get(uname, "Developer") if isinstance(emps, dict) else "Developer"
        summary_text.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")

    await callback.message.reply("\n".join(summary_text), parse_mode="HTML")
    await callback.answer()

# --- Edit Reports flow ---

@dp.callback_query(F.data == "edit_list")
async def cb_edit_list(callback: CallbackQuery):
    """Show dates available for editing."""
    if not is_admin(callback.from_user.id):
        return
    data = load_data()
    dates = [k for k in data.keys() if re.match(r"^\d{4}-\d{2}-\d{2}$", k)]
    dates.sort(reverse=True)
    if not dates:
        await callback.message.reply("No saved reports to edit.")
        await callback.answer()
        return
    
    kb_buttons = []
    for d in dates[:30]:
        kb_buttons.append([InlineKeyboardButton(text=f"✏️ {d}", callback_data=f"edit_date_{d}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.reply("Select a date to edit reports:", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_date_"))
async def cb_edit_date(callback: CallbackQuery):
    """Show employees for the selected date to edit."""
    if not is_admin(callback.from_user.id):
        return
    date_str = callback.data.replace("edit_date_", "")
    data = load_data()
    
    if date_str not in data or "replies" not in data[date_str] or not data[date_str]["replies"]:
        await callback.message.reply(f"No reports found for {date_str}.")
        await callback.answer()
        return
    
    kb_buttons = []
    for uid, info in data[date_str]["replies"].items():
        name = info.get("name", uid)
        kb_buttons.append([InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"edit_emp_{date_str}_{uid}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.reply(f"Select employee to edit for {date_str}:", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_emp_"))
async def cb_edit_emp(callback: CallbackQuery):
    """Show current report and ask admin to send corrected text."""
    if not is_admin(callback.from_user.id):
        return
    # Parse: edit_emp_2026-04-02_6028050747
    parts = callback.data.replace("edit_emp_", "").split("_", 1)
    if len(parts) != 2:
        await callback.answer("Error parsing data")
        return
    date_str, target_uid = parts
    data = load_data()
    
    if date_str not in data or "replies" not in data[date_str] or target_uid not in data[date_str]["replies"]:
        await callback.message.reply("Report not found.")
        await callback.answer()
        return
    
    info = data[date_str]["replies"][target_uid]
    
    # Set admin edit state
    admin_edit_state[str(callback.from_user.id)] = {
        "date": date_str,
        "target_uid": target_uid,
        "name": info.get("name", target_uid)
    }
    
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_edit")]]
    )
    
    await callback.message.reply(
        f"✏️ <b>Editing report for {info['name']} ({date_str})</b>\n\n"
        f"<b>Current report:</b>\n{info['text']}\n\n"
        f"——————————————\n"
        f"📝 Send the corrected report text now (as a plain message).",
        parse_mode="HTML",
        reply_markup=cancel_kb
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel_edit")
async def cb_cancel_edit(callback: CallbackQuery):
    if str(callback.from_user.id) in admin_edit_state:
        del admin_edit_state[str(callback.from_user.id)]
    await callback.message.reply("❌ Edit cancelled.")
    await callback.answer()

@dp.message(F.chat.type == "private", F.text)
async def handle_admin_edit(message: Message):
    """Catches plain text in private chat from admin when in edit state."""
    if message.text.startswith("/"):
        return
    uid = str(message.from_user.id)
    if uid not in ADMIN_IDS or uid not in admin_edit_state:
        return
    
    state = admin_edit_state.pop(uid)
    date_str = state["date"]
    target_uid = state["target_uid"]
    emp_name = state["name"]
    
    data = load_data()
    if date_str not in data or "replies" not in data[date_str] or target_uid not in data[date_str]["replies"]:
        await message.reply("⚠️ Report not found. It may have been cleared.")
        return
    
    new_text = parse_jira_links(message.text)
    data[date_str]["replies"][target_uid]["text"] = new_text
    data[date_str]["replies"][target_uid]["time"] = data[date_str]["replies"][target_uid].get("time", "") + " (edited)"
    save_data(data)
    
    await message.reply(
        f"✅ Report for <b>{emp_name}</b> ({date_str}) has been updated!\n\n"
        f"<b>New text:</b>\n{new_text}",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "clear_list")
async def cb_clear_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    data = load_data()
    dates = []
    for k in data.keys():
        if re.match(r"^\d{4}-\d{2}-\d{2}$", k):
            dates.append(k)
    dates.sort(reverse=True)
    if not dates:
        await callback.message.reply("No saved data to clear.")
        await callback.answer()
        return
    
    # We will show at most the last 30 days
    kb_buttons = []
    for d in dates[:30]:
        kb_buttons.append([InlineKeyboardButton(text=f"🗑 Clear {d}", callback_data=f"clear_hist_{d}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.reply("Select a date to clear:", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_hist_"))
async def cb_clear_hist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    date_str = callback.data.replace("clear_hist_", "")
    data = load_data()
    
    if date_str in data and "replies" in data[date_str]:
        data[date_str]["replies"] = {}
        save_data(data)
        await callback.message.reply(f"🧼 Data for <b>{date_str}</b> has been cleared!", parse_mode="HTML")
    else:
        await callback.message.reply(f"No data to delete for {date_str}.")
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.message(Command("plan"))
async def cmd_plan(message: Message):
    """Manually trigger the standup prompt."""
    if str(message.chat.id) != str(CHAT_ID):
        await message.reply("This command is only available in the designated team chat.")
        return
    if TOPIC_ID and message.message_thread_id != TOPIC_ID:
        await message.reply("📌 Please send this command in the <b>To-do</b> topic.", parse_mode="HTML")
        return
    await send_standup_prompt()

@dp.message(F.reply_to_message, ~Command("plan"), ~Command("summary"))
async def handle_replies(message: Message):
    """Listens for replies to the bot's standup prompt and saves them."""
    if str(message.chat.id) != str(CHAT_ID):
        return
    if TOPIC_ID and message.message_thread_id != TOPIC_ID:
        return

    # Check if msg is a reply to the bot
    if message.reply_to_message.from_user.id != bot.id:
        return

    data = load_data()
    today = get_today_str()

    if today not in data:
        data[today] = {}
    
    if "replies" not in data[today]:
        data[today]["replies"] = {}

    user_id = str(message.from_user.id)
    name = message.from_user.full_name or message.from_user.username or user_id
    username = message.from_user.username

    # Save username -> user_id mapping for proper mentions
    if username:
        save_user_mapping(username, message.from_user.id)

    text_lower = message.text.lower()
    
    # If user already submitted today, treat as append
    if user_id in data[today]["replies"]:
        if "yesterday" not in text_lower and "today" not in text_lower:
            old_text = data[today]["replies"][user_id]["text"]
            appended_text = parse_jira_links(message.text)
            data[today]["replies"][user_id]["text"] = old_text + "\n<b>Update:</b>\n" + appended_text
            save_data(data)
            await message.reply("✅ Your update has been appended to today's report!")
            return

    # AI validation: check if standup has real task descriptions
    ai_result = await validate_standup_with_ai(message.text)
    
    if ai_result and not ai_result["valid"]:
        await message.reply(
            f"⚠️ <b>Please describe your tasks properly!</b>\n\n"
            f"{ai_result['reason']}\n\n"
            f"<i>Example:</i>\nYesterday: finished CDT-344\nToday: working on CDT-376",
            parse_mode="HTML"
        )
        return

    linked_text = parse_jira_links(message.text)

    data[today]["replies"][user_id] = {
        "name": name,
        "username": username,
        "text": linked_text,
        "time": datetime.now().strftime("%H:%M:%S")
    }
    
    save_data(data)

    await message.reply("✅ Plan recorded! Thank you.")

@dp.callback_query(F.data == "append_standup_btn")
async def cb_append_standup_btn(callback: CallbackQuery):
    await callback.answer("If you forgot something, just send a new Reply to the bot's message and it will automatically append your text to your report!", show_alert=True)

async def check_missing_standups(force=False):
    """Pings employees who haven't submitted their standup today."""
    data = load_data()
    if not force and data.get("pinging_paused", False):
        return
        
    emps = get_employees()
    if not emps:
        return
        
    today = get_today_str()
    
    if today not in data or "prompt_message_id" not in data[today]:
        # Auto-send standup prompt if it wasn't sent today (e.g. bot started late)
        await send_standup_prompt()
        data = load_data()  # Reload after sending prompt
        if today not in data or "prompt_message_id" not in data[today]:
            return
        
    replies = data[today].get("replies", {})
    
    submitted_usernames = set()
    for uid, info in replies.items():
        if info.get("username"):
            submitted_usernames.add(info["username"].lower())
            
    user_map = get_user_map()
    missing = []
    for emp in emps:
        if emp not in submitted_usernames:
            missing.append(build_mention(emp, user_map))
            
    if missing:
        # Delete previous ping message
        old_ping_id = data[today].get("ping_message_id")
        if old_ping_id:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=old_ping_id)
            except Exception:
                pass

        mentions = " ".join(missing)
        text = f"⏳ {mentions}\nWe're still waiting for your Daily Standup!\n\nPlease reply to the morning standup message with your plan."
        sent_msg = None
        try:
            prompt_id = data[today]["prompt_message_id"]
            sent_msg = await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", reply_to_message_id=prompt_id, message_thread_id=TOPIC_ID)
        except Exception:
            try:
                sent_msg = await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", message_thread_id=TOPIC_ID)
            except Exception as e:
                logging.error(f"Failed to ping missing employees: {e}")

        # Save new ping message ID for deletion next time
        if sent_msg:
            data[today]["ping_message_id"] = sent_msg.message_id
            save_data(data)

async def report_missing_standups_at_noon(force=False):
    """Notifies @Shahzod_Rustamjon about employees who haven't submitted their standup by 12:00."""
    data = load_data()
    if not force and data.get("pinging_paused", False):
        return
        
    emps = get_employees()
    if not emps:
        return
        
    today = get_today_str()
    
    if today not in data or "prompt_message_id" not in data[today]:
        return
        
    replies = data[today].get("replies", {})
    
    # Check who has replied
    submitted_user_ids = list(replies.keys())
    submitted_usernames = [replies[uid].get("username", "").lower() for uid in submitted_user_ids if replies[uid].get("username")]
    
    user_map = data.get("user_map", {})
    
    missing = []
    for emp in emps:
        if emp not in submitted_usernames:
            missing.append(build_mention(emp, user_map))
            
    if missing:
        mentions = ", ".join(missing)
        text = f"🚨 <b>Daily Standup Report (12:00)</b>\n\nThese employees haven't submitted their standup yet:\n{mentions}"
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send 12:00 report to admin {admin_id}: {e}")

async def auto_send_summary():
    """Automatically sends the compiled summary of today's standup at 18:00."""
    data = load_data()
    today = get_today_str()

    if today not in data or "replies" not in data[today] or not data[today]["replies"]:
        try:
            await bot.send_message(chat_id=CHAT_ID, text="📋 No standup reports were submitted today.", message_thread_id=TOPIC_ID)
        except Exception:
            pass
        return

    emps = get_employees()
    summary_text = [f"📋 <b>Standup Summary for {today}</b>\n"]
    
    for uid, info in data[today]["replies"].items():
        uname = (info.get("username") or "").lower()
        role = emps.get(uname, "Developer") if isinstance(emps, dict) else "Developer"
        summary_text.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")

    try:
        await bot.send_message(chat_id=CHAT_ID, text="\n".join(summary_text), parse_mode="HTML", message_thread_id=TOPIC_ID)
    except Exception as e:
        logging.error(f"Failed to send auto summary: {e}")

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    """Sends a compiled summary of today's standup."""
    if str(message.chat.id) != str(CHAT_ID):
        await message.reply("This command is only available in the designated team chat.")
        return
    if TOPIC_ID and message.message_thread_id != TOPIC_ID:
        await message.reply("📌 Please send this command in the <b>To-do</b> topic.", parse_mode="HTML")
        return

    data = load_data()
    today = get_today_str()

    if today not in data or "replies" not in data[today] or not data[today]["replies"]:
        await message.reply("No plans submitted for today yet.")
        return

    emps = get_employees()
    summary_text = [f"📋 <b>Standup Summary for {today}</b>\n"]
    
    for uid, info in data[today]["replies"].items():
        uname = (info.get("username") or "").lower()
        role = emps.get(uname, "Developer") if isinstance(emps, dict) else "Developer"
        summary_text.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")

    await message.answer("\n".join(summary_text), parse_mode="HTML")

async def auto_pause_pinging():
    data = load_data()
    data["pinging_paused"] = True
    save_data(data)
    logging.info("Auto-paused pinging at 12:00")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id, 
                text="⏰ It's 12:00 PM.\nControl Panel (Pinging: <b>STOPPED</b> ⏸).\n\nIt will resume automatically tomorrow at 9:00 AM.", 
                reply_markup=admin_keyboard, 
                parse_mode="HTML"
            )
        except Exception:
            pass

async def reset_pinging():
    data = load_data()
    data["pinging_paused"] = False
    save_data(data)
    logging.info("Auto-resumed pinging at 09:00")

async def main():
    # Schedule the background task
    scheduler.add_job(
        send_standup_prompt,
        "cron",
        day_of_week="mon-fri",
        hour=9,
        minute=0
    )
    scheduler.add_job(
        check_missing_standups,
        "cron",
        day_of_week="mon-fri",
        hour="9-17",
        minute="*/15"
    )
    scheduler.add_job(
        auto_pause_pinging,
        "cron",
        day_of_week="mon-fri",
        hour=12,
        minute=0
    )
    scheduler.add_job(
        reset_pinging,
        "cron",
        day_of_week="mon-fri",
        hour=9,
        minute=0
    )
    scheduler.add_job(
        auto_send_summary,
        "cron",
        day_of_week="mon-fri",
        hour=18,
        minute=0
    )
    scheduler.add_job(
        report_missing_standups_at_noon,
        "cron",
        day_of_week="mon-fri",
        hour=12,
        minute=0
    )
    scheduler.start()

    # Start polling
    logging.info("Starting bot polling...")
    await bot.set_my_commands([
        BotCommand(command="start", description="Admin Control Panel (DM)"),
        BotCommand(command="add", description="Add employee (DM)"),
        BotCommand(command="remove", description="Remove employee (DM)"),
        BotCommand(command="list", description="Employee list (DM)"),
        BotCommand(command="plan", description="Request standup (Group)"),
        BotCommand(command="summary", description="Standup summary (Group)")
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Start web dashboard server
    from aiohttp import web as aio_web
    
    async def handle_dashboard(request):
        return aio_web.FileResponse('./dashboard.html')
    
    async def handle_api_data(request):
        data = load_data()
        return aio_web.json_response(data)
    
    app = aio_web.Application()
    app.router.add_get('/', handle_dashboard)
    app.router.add_get('/api/data', handle_api_data)
    
    port = int(os.getenv('PORT', 8080))
    runner = aio_web.AppRunner(app)
    await runner.setup()
    site = aio_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Dashboard running on port {port}")
    
    # Start bot polling (this blocks)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
