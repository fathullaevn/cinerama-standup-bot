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
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove, BotCommand, ChatMemberUpdated
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# Load env variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
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
GRAFANA_USER = os.getenv("GRAFANA_USER", "nuriddin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "nuriddin")

SUPERSET_URL = os.getenv("SUPERSET_URL", "https://ba.cinerama.uz/superset/dashboard/55/?native_filters_key=oga88LqofF8")
SUPERSET_USER = os.getenv("SUPERSET_USER", "nuriddin")
SUPERSET_PASS = os.getenv("SUPERSET_PASS", "nuriddin")

if not BOT_TOKEN:
    print("Please set BOT_TOKEN in the .env file.")
    exit(1)

# ─── Multi-Group Configuration ────────────────────────────────────────────────
def _build_groups():
    groups = []
    cid = os.getenv("CHAT_ID", "")
    if cid:
        tid_raw = os.getenv("TOPIC_ID", "0")
        tid = int(tid_raw) if tid_raw and tid_raw.strip() != "0" else None
        groups.append({"key": "group1", "chat_id": cid, "topic_id": tid,
                        "name": os.getenv("GROUP_NAME_1", "Dev Team")})
    for i in range(2, 10):
        cid = os.getenv(f"CHAT_ID_{i}", "")
        if not cid:
            break
        tid_raw = os.getenv(f"TOPIC_ID_{i}", "0")
        tid = int(tid_raw) if tid_raw and tid_raw.strip() != "0" else None
        groups.append({"key": f"group{i}", "chat_id": cid, "topic_id": tid,
                        "name": os.getenv(f"GROUP_NAME_{i}", f"Группа {i}")})
    return groups

def get_all_groups():
    groups = _build_groups()
    data = load_data()
    dynamic = data.get("dynamic_groups", [])
    groups.extend(dynamic)
    return groups

def get_group_by_chat_id(chat_id):
    s = str(chat_id)
    for g in get_all_groups():
        if str(g["chat_id"]) == s:
            return g
    return None

def get_group_by_key(key):
    for g in get_all_groups():
        if g["key"] == key:
            return g
    return None

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

DATA_FILE = Path("data.json")

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

def migrate_data_if_needed():
    """Migrate flat data.json to group-keyed structure."""
    data = load_data()
    if not data:
        return
    if any(re.match(r'^group\d+$', k) for k in data.keys()):
        return  # Already migrated
    logging.info("Migrating data.json to multi-group format...")
    save_data({"group1": data})
    logging.info("Migration complete.")

def load_group_data(group_key: str) -> dict:
    return load_data().get(group_key, {})

def save_group_data(group_key: str, group_data: dict):
    data = load_data()
    data[group_key] = group_data
    save_data(data)

def get_today_str():
    return datetime.now().strftime("%Y-%m-%d")

import html
import re

def parse_jira_links(text, gkey=""):
    safe_text = html.escape(text)
    g = get_group_by_key(gkey) if gkey else None
    
    jira_base = "https://cineramauzb.atlassian.net/jira/software/projects/CDT/boards/201?selectedIssue="
    if g and "marketing" in g.get("name", "").lower():
        jira_base = "https://www.turontrello.uz/#/board/1dc0b1b8-3533-4cc0-bf46-5f826ef6b3fa/"
    
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

async def validate_standup_with_ai(text, gkey=""):
    """Use AI to validate if standup contains real task descriptions."""
    if not OPENROUTER_API_KEY:
        return None  # Skip AI validation if no key
        
    g = get_group_by_key(gkey) if gkey else None
    is_ru = g and "marketing" in g.get("name", "").lower()
    
    example_text = "Вчера: сделал ...\nСегодня: делаю ..." if is_ru else "Yesterday: finished CDT-344\nToday: working on CDT-376"
    
    prompt = (
        "You are a standup report validator for a software development/marketing team. "
        "Your job is to ensure employees write REAL task descriptions.\n\n"
        "REJECT if ANY of the following:\n"
        "- Sections are empty (just 'Yesterday:' / 'Today:' or 'Вчера:' / 'Сегодня:' with no real content)\n"
        "- Content is test/fake text (like 'test', 'asdf', 'abc', '123', 'xxx', random characters)\n"
        "- Content is meaningless filler ('...', '---', 'nothing', 'n/a', single characters/words)\n"
        "- Ticket numbers are obviously fake (e.g. CDT-123123123123)\n\n"
        "ACCEPT if the report contains any of the following:\n"
        "- Real technical tasks ('fixed bug', 'deployed app', 'CDT-344')\n"
        "- Real PM/Management/HR/Marketing tasks (e.g., 'Fully documentation', 'Search for candidates', 'Дизайн', 'Посты в инсту')\n"
        "If a person wrote actual sentences about work like meetings, docs, or hiring, it is VALID. The text can be in Russian or English.\n\n"
        f"Standup report:\n{text}\n\n"
        "Respond ONLY in this exact JSON format, no other text:\n"
        '{"valid": true} or {"valid": false, "reason": "brief explanation in the user\'s language"}'
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
    """Take Grafana dashboard screenshot."""
    if not GRAFANA_USER or not GRAFANA_PASS:
        logging.warning("GRAFANA_USER or GRAFANA_PASS not set")
        return None
        
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1920, "height": 1080}, ignore_https_errors=True)
            page = await context.new_page()

            login_url = GRAFANA_URL.split('/d/')[0] + '/login'
            logging.info(f"[Grafana] Logging in at {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await page.fill('input[name="user"]', GRAFANA_USER)
            await page.fill('input[name="password"]', GRAFANA_PASS)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

            logging.info("[Grafana] Loading dashboard...")
            kiosk_url = GRAFANA_URL + ("&" if "?" in GRAFANA_URL else "?") + "kiosk"
            await page.goto(kiosk_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(15000)

            screenshot = await page.screenshot(full_page=True, type="png")
            logging.info(f"[Grafana] Captured: {len(screenshot)} bytes")
            await browser.close()
            return screenshot
    except Exception as e:
        logging.error(f"Screenshot failed: {e}")
        return None

async def screenshot_superset():
    """Take a screenshot of the Superset dashboard using Playwright."""
    if not SUPERSET_USER or not SUPERSET_PASS:
        logging.warning("SUPERSET_USER or SUPERSET_PASS not set")
        return None
    
    try:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = await context.new_page()
            
            login_url = SUPERSET_URL.split('/superset/')[0] + '/login/'
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            
            await page.fill('input#username', SUPERSET_USER)
            await page.fill('input#password', SUPERSET_PASS)
            await page.click('input[type="submit"], button[type="submit"]')
            
            await page.wait_for_timeout(3000)
            
            await page.goto(SUPERSET_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)
            
            screenshot = await page.screenshot(full_page=True, type="png")
            
            await browser.close()
            return screenshot
    except Exception as e:
        logging.error(f"Superset screenshot failed: {e}")
        return None

def build_group_select_keyboard():
    buttons = [[InlineKeyboardButton(text=f"👥 {g['name']}", callback_data=f"grp_{g['key']}")]
               for g in get_all_groups()]
    buttons.append([InlineKeyboardButton(text="➕ Add Group", callback_data="add_group_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_admin_keyboard(gkey):
    k = gkey
    
    keyboard = []
    
    if k == "group1":
        keyboard.extend([
            [InlineKeyboardButton(text="⏸ Stop", callback_data=f"stop_ping_{k}"),
             InlineKeyboardButton(text="▶️ Start", callback_data=f"start_ping_{k}")],
            [InlineKeyboardButton(text="📢 Ping Now", callback_data=f"ping_now_{k}"),
             InlineKeyboardButton(text="📋 Summary", callback_data=f"send_summary_{k}")],
            [InlineKeyboardButton(text="🚨 12:00 Report", callback_data=f"noon_report_{k}")],
            [InlineKeyboardButton(text="📸 Grafana", callback_data=f"scr_grafana_{k}"),
             InlineKeyboardButton(text="📸 Superset", callback_data=f"scr_superset_{k}"),
             InlineKeyboardButton(text="📸 Both", callback_data=f"scr_both_{k}")]
        ])
    else:
        # Only show Summary button for other groups (no pinging/screenshots)
        keyboard.append([InlineKeyboardButton(text="📋 Summary", callback_data=f"send_summary_{k}")])
        
    keyboard.extend([
        [InlineKeyboardButton(text="👥 Employees", callback_data=f"list_emp_{k}"),
         InlineKeyboardButton(text="➕ Add", callback_data=f"add_emp_{k}")],
        [InlineKeyboardButton(text="➖ Remove", callback_data=f"rem_emp_{k}"),
         InlineKeyboardButton(text="📁 History", callback_data=f"hist_{k}")],
        [InlineKeyboardButton(text="✏️ Edit", callback_data=f"edit_list_{k}"),
         InlineKeyboardButton(text="🗑 Clear", callback_data=f"clear_list_{k}")],
        [InlineKeyboardButton(text="🔙 Groups", callback_data="back_to_groups")],
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# State for admin edit flow
admin_edit_state = {}

def get_employees(group_key):
    gdata = load_group_data(group_key)
    emps = gdata.get("employees", {})
    if isinstance(emps, list):
        emps = {u: "Developer" for u in emps}
    return emps

def get_user_map(group_key):
    return load_group_data(group_key).get("user_map", {})

def save_user_mapping(group_key, username, user_id):
    gdata = load_group_data(group_key)
    if "user_map" not in gdata:
        gdata["user_map"] = {}
    gdata["user_map"][username.lower()] = str(user_id)
    save_group_data(group_key, gdata)

def build_mention(username, user_map):
    uid = user_map.get(username.lower())
    if uid:
        return f'<a href="tg://user?id={uid}">@{username}</a>'
    return f"@{username}"

def add_employee(group_key, username, role):
    gdata = load_group_data(group_key)
    if "employees" not in gdata or isinstance(gdata["employees"], list):
        gdata["employees"] = {}
    gdata["employees"][username] = role
    save_group_data(group_key, gdata)

def remove_employee(group_key, username):
    gdata = load_group_data(group_key)
    emps = gdata.get("employees", {})
    if isinstance(emps, dict) and username in emps:
        del emps[username]
    elif isinstance(emps, list) and username in emps:
        emps.remove(username)
    gdata["employees"] = emps
    save_group_data(group_key, gdata)

async def send_standup_prompt(group):
    """Sends the daily standup reminder to a group."""
    gkey = group["key"]
    chat_id = group["chat_id"]
    topic_id = group["topic_id"]
    emps = get_employees(gkey)
    user_map = get_user_map(gkey)
    mentions = " ".join([build_mention(u, user_map) for u in emps]) if emps else ""
    is_ru = "marketing" in group.get("name", "").lower()
    
    if is_ru:
        text = (
            f"🌅 <b>Ежедневный Стендап</b>\n\n"
            f"{mentions}\n"
            f"Пожалуйста, ответьте на это сообщение вашим планом:\n\n"
            f"✅ Вчера: что вы завершили? (номер задачи)\n"
            f"🎯 Сегодня: что вы будете делать? (номер задачи)\n"
            f"🚧 Блок: да/нет — если да, то что вам нужно?"
        )
        btn_text = "➕ Дополнить стендап"
    else:
        text = (
            f"🌅 <b>Daily Standup</b>\n\n"
            f"{mentions}\n"
            f"Please reply to this message with your plan:\n\n"
            f"✅ Yesterday: what did you finish? (ticket number)\n"
            f"🎯 Today: what will you complete? (ticket number)\n"
            f"🚧 Blocked: yes/no — if yes, what do you need?"
        )
        btn_text = "➕ Append to your Standup"
        
    prompt_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data="append_standup_btn")]]
    )
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                     reply_markup=prompt_keyboard, message_thread_id=topic_id)
        gdata = load_group_data(gkey)
        today = get_today_str()
        if today not in gdata:
            gdata[today] = {}
        gdata[today]["prompt_message_id"] = msg.message_id
        if datetime.now().hour < 12:
            gdata["pinging_paused"] = False
        save_group_data(gkey, gdata)
    except Exception as e:
        logging.error(f"Failed to send prompt to {gkey}: {e}")

@dp.message(Command("add"), F.chat.type == "private")
async def cmd_add_emp(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply(f"\u26d4\ufe0f Access denied. Your ID: {message.from_user.id}")
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("Usage: /add <group_key> @username [Role]\nExample: /add group1 @ivan Frontend")
        return
    gkey = args[0]
    if not get_group_by_key(gkey):
        keys = ", ".join(g["key"] for g in GROUPS)
        await message.reply(f"Unknown group. Available: {keys}")
        return
    username = args[1].lstrip('@').lower()
    role = " ".join(args[2:]) if len(args) > 2 else "Developer"
    add_employee(gkey, username, role)
    await message.reply(f"\u2705 @{username} added to {gkey} as {role}.")

@dp.message(Command("remove"), F.chat.type == "private")
async def cmd_remove_emp(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("Usage: /remove <group_key> @username")
        return
    gkey, username = args[0], args[1].lstrip('@').lower()
    remove_employee(gkey, username)
    await message.reply(f"\u274c @{username} removed from {gkey}.")

@dp.message(Command("list"), F.chat.type == "private")
async def cmd_list_emp(message: Message):
    if not is_admin(message.from_user.id):
        return
    lines = []
    for g in GROUPS:
        emps = get_employees(g["key"])
        lines.append(f"<b>{g['name']} ({g['key']}):</b>")
        if emps:
            lines += [f"  - @{e} ({r})" for e, r in emps.items()]
        else:
            lines.append("  (empty)")
    await message.reply("\n".join(lines), parse_mode="HTML")

from aiogram.types import BotCommandScopeChat

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=message.chat.id))
    except Exception:
        pass
    if not is_admin(message.from_user.id):
        await message.reply(f"\u26d4\ufe0f Access denied. Your ID: {message.from_user.id}", reply_markup=ReplyKeyboardRemove())
        return
    msg = await message.answer("\U0001f504 Updating...", reply_markup=ReplyKeyboardRemove())
    await msg.delete()
    await message.reply("\U0001f4ca Select a group:", reply_markup=build_group_select_keyboard())

@dp.callback_query(F.data == "back_to_groups")
async def cb_back_to_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        await callback.message.edit_text("\U0001f4ca Select a group:", reply_markup=build_group_select_keyboard())
    except Exception:
        await callback.message.reply("\U0001f4ca Select a group:", reply_markup=build_group_select_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("grp_"))
async def cb_group_select(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    gkey = callback.data[4:]
    g = get_group_by_key(gkey)
    if not g:
        await callback.answer("Group not found")
        return
    gdata = load_group_data(gkey)
    now_hour = datetime.now().hour
    if now_hour >= 18 or now_hour < 9:
        status = "OFF HOURS \U0001f319"
    else:
        status = "STOPPED \u23f8" if gdata.get("pinging_paused") else "ACTIVE \u25b6\ufe0f"
    try:
        await callback.message.edit_text(
            f"<b>{g['name']}</b> | Pinging: <b>{status}</b>",
            reply_markup=build_admin_keyboard(gkey), parse_mode="HTML")
    except Exception:
        await callback.message.reply(
            f"<b>{g['name']}</b> | Pinging: <b>{status}</b>",
            reply_markup=build_admin_keyboard(gkey), parse_mode="HTML")
    await callback.answer()


def _parse_gkey(data: str, prefix: str):
    """Extract group key from callback data by stripping prefix."""
    return data[len(prefix):]

@dp.callback_query(F.data.startswith("list_emp_"))
async def cb_list_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "list_emp_")
    emps = get_employees(gkey)
    g = get_group_by_key(gkey)
    name = g["name"] if g else gkey
    if not emps:
        await callback.message.reply(f"<b>{name}</b>: Employee list is empty.", parse_mode="HTML")
    else:
        text = f"👥 <b>{name} Employees:</b>\n" + "\n".join([f"- @{e} ({r})" for e, r in emps.items()])
        await callback.message.reply(text, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("add_emp_"))
async def cb_add_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "add_emp_")
    await callback.message.reply(
        f"📝 To add: <code>/add {gkey} @username Role</code>\nExample: <code>/add {gkey} @ivan Frontend</code>",
        parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("rem_emp_"))
async def cb_rem_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "rem_emp_")
    await callback.message.reply(
        f"🗑 To remove: <code>/remove {gkey} @username</code>",
        parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("stop_ping_"))
async def cb_stop_ping(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "stop_ping_")
    gdata = load_group_data(gkey)
    gdata["pinging_paused"] = True
    save_group_data(gkey, gdata)
    g = get_group_by_key(gkey)
    try:
        await callback.message.edit_text(
            f"<b>{g['name']}</b> | Pinging: <b>STOPPED ⏸</b>",
            reply_markup=build_admin_keyboard(gkey), parse_mode="HTML")
    except Exception: pass
    await callback.answer("Pinging stopped")

@dp.callback_query(F.data.startswith("start_ping_"))
async def cb_start_ping(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "start_ping_")
    gdata = load_group_data(gkey)
    gdata["pinging_paused"] = False
    save_group_data(gkey, gdata)
    g = get_group_by_key(gkey)
    try:
        await callback.message.edit_text(
            f"<b>{g['name']}</b> | Pinging: <b>ACTIVE ▶️</b>",
            reply_markup=build_admin_keyboard(gkey), parse_mode="HTML")
    except Exception: pass
    await callback.answer("Pinging started")

@dp.callback_query(F.data.startswith("ping_now_"))
async def cb_ping_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "ping_now_")
    g = get_group_by_key(gkey)
    await callback.answer("📢 Pinging...")
    if g: await check_missing_standups(g, force=True)

@dp.callback_query(F.data.startswith("send_summary_"))
async def cb_send_summary(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "send_summary_")
    g = get_group_by_key(gkey)
    await callback.answer("📋 Sending summary...")
    if g: await auto_send_summary(g)

@dp.callback_query(F.data.startswith("noon_report_"))
async def cb_noon_report(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "noon_report_")
    g = get_group_by_key(gkey)
    await callback.answer("🚨 Sending noon report...")
    if g: await report_missing_standups_at_noon(g, force=True)

@dp.callback_query(F.data.startswith("scr_"))
async def cb_screenshot(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rest = callback.data[4:]  # e.g. "grafana_group1"
    parts = rest.rsplit("_", 1)
    target = parts[0]  # grafana/superset/both
    
    labels = {"grafana": "Grafana", "superset": "Superset", "both": "Both"}
    await callback.answer(f"📸 {labels.get(target, target)} processing...")
    
    # We edit message or send new
    processing_msg = await callback.message.reply(f"📸 Taking {labels.get(target, target)} screenshot, please wait (~15-20 sec)...")
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    from aiogram.types import BufferedInputFile
    
    # Take and send screenshots inline
    if target in ("grafana", "both"):
        try:
            img_bytes = await screenshot_grafana()
            if img_bytes:
                photo = BufferedInputFile(img_bytes, filename="grafana.png")
                await bot.send_photo(chat_id=callback.from_user.id, photo=photo, caption=f"📊 <b>Grafana — Tariffs</b>\n{now}", parse_mode="HTML")
            else:
                await bot.send_message(chat_id=callback.from_user.id, text="❌ Failed to take Grafana screenshot.")
        except Exception as e:
            logging.error(f"Grafana send error: {e}")
            await bot.send_message(chat_id=callback.from_user.id, text=f"❌ Grafana error: {e}")
            
    if target in ("superset", "both"):
        try:
            img_bytes = await screenshot_superset()
            if img_bytes:
                photo = BufferedInputFile(img_bytes, filename="superset.png")
                await bot.send_photo(chat_id=callback.from_user.id, photo=photo, caption=f"📊 <b>Superset — Daily Results</b>\n{now}", parse_mode="HTML")
            else:
                await bot.send_message(chat_id=callback.from_user.id, text="❌ Failed to take Superset screenshot.")
        except Exception as e:
            logging.error(f"Superset send error: {e}")
            await bot.send_message(chat_id=callback.from_user.id, text=f"❌ Superset error: {e}")
            
    try:
        await processing_msg.delete()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("hist_"))
async def cb_hist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "hist_")
    gdata = load_group_data(gkey)
    dates = sorted([k for k in gdata if re.match(r"^\d{4}-\d{2}-\d{2}$", k)], reverse=True)
    if not dates:
        await callback.message.reply("Report history is empty."); await callback.answer(); return
    kb = [[InlineKeyboardButton(text=f"📅 {d}", callback_data=f"showhist_{gkey}_{d}")] for d in dates[:20]]
    await callback.message.reply("Select a date:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("showhist_"))
async def cb_show_hist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rest = callback.data[9:]  # group1_2026-04-02
    gkey, date_str = rest.split("_", 1)
    gdata = load_group_data(gkey)
    emps = get_employees(gkey)
    day = gdata.get(date_str, {})
    if not day.get("replies"):
        await callback.message.reply(f"No reports for {date_str}."); await callback.answer(); return
    lines = [f"📋 <b>Standup {date_str}</b>\n"]
    for uid, info in day["replies"].items():
        role = emps.get((info.get("username") or "").lower(), "Developer")
        lines.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")
    await callback.message.reply("\n".join(lines), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_list_"))
async def cb_edit_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "edit_list_")
    gdata = load_group_data(gkey)
    dates = sorted([k for k in gdata if re.match(r"^\d{4}-\d{2}-\d{2}$", k)], reverse=True)
    if not dates:
        await callback.message.reply("No saved reports."); await callback.answer(); return
    kb = [[InlineKeyboardButton(text=f"✏️ {d}", callback_data=f"editdate_{gkey}_{d}")] for d in dates[:20]]
    await callback.message.reply("Select date to edit:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("editdate_"))
async def cb_edit_date(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rest = callback.data[9:]
    gkey, date_str = rest.split("_", 1)
    gdata = load_group_data(gkey)
    day = gdata.get(date_str, {})
    if not day.get("replies"):
        await callback.message.reply(f"No reports for {date_str}."); await callback.answer(); return
    kb = [[InlineKeyboardButton(text=f"✏️ {info.get('name', uid)}",
           callback_data=f"editemp_{gkey}_{date_str}_{uid}")]
          for uid, info in day["replies"].items()]
    await callback.message.reply(f"Select employee for {date_str}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("editemp_"))
async def cb_edit_emp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rest = callback.data[8:]  # group1_2026-04-02_123456
    parts = rest.split("_")
    gkey = parts[0]
    # date is YYYY-MM-DD = parts[1]+_+parts[2]+_+parts[3], uid = parts[4]
    date_str = "-".join(parts[1:4])
    target_uid = parts[4]
    gdata = load_group_data(gkey)
    day = gdata.get(date_str, {})
    if not day.get("replies") or target_uid not in day["replies"]:
        await callback.message.reply("Report not found."); await callback.answer(); return
    info = day["replies"][target_uid]
    admin_edit_state[str(callback.from_user.id)] = {"gkey": gkey, "date": date_str, "uid": target_uid, "name": info.get("name", target_uid)}
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_edit")]])
    await callback.message.reply(
        f"✏️ <b>Editing {info['name']} ({date_str})</b>\n\n<b>Current:</b>\n{info['text']}\n\n📝 Send new text:",
        parse_mode="HTML", reply_markup=cancel_kb)
    await callback.answer()

@dp.callback_query(F.data == "cancel_edit")
async def cb_cancel_edit(callback: CallbackQuery):
    admin_edit_state.pop(str(callback.from_user.id), None)
    await callback.message.reply("❌ Edit cancelled.")
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_list_"))
async def cb_clear_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    gkey = _parse_gkey(callback.data, "clear_list_")
    gdata = load_group_data(gkey)
    dates = sorted([k for k in gdata if re.match(r"^\d{4}-\d{2}-\d{2}$", k)], reverse=True)
    if not dates:
        await callback.message.reply("No data to clear."); await callback.answer(); return
    kb = [[InlineKeyboardButton(text=f"🗑 {d}", callback_data=f"clearhist_{gkey}_{d}")] for d in dates[:20]]
    await callback.message.reply("Select date to clear:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("clearhist_"))
async def cb_clear_hist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rest = callback.data[10:]
    gkey, date_str = rest.split("_", 1)
    gdata = load_group_data(gkey)
    if date_str in gdata and "replies" in gdata[date_str]:
        gdata[date_str]["replies"] = {}
        save_group_data(gkey, gdata)
        await callback.message.reply(f"🧼 <b>{date_str}</b> cleared!", parse_mode="HTML")
    else:
        await callback.message.reply(f"No data for {date_str}.")
    try: await callback.message.delete()
    except Exception: pass
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
    gkey = state["gkey"]
    date_str = state["date"]
    target_uid = state["uid"]
    emp_name = state["name"]
    gdata = load_group_data(gkey)
    day = gdata.get(date_str, {})
    if not day.get("replies") or target_uid not in day["replies"]:
        await message.reply("⚠️ Report not found.")
        return
    new_text = parse_jira_links(message.text)
    day["replies"][target_uid]["text"] = new_text
    day["replies"][target_uid]["time"] = day["replies"][target_uid].get("time", "") + " (edited)"
    save_group_data(gkey, gdata)
    await message.reply(
        f"✅ Report for <b>{emp_name}</b> ({date_str}) updated!\n\n<b>New text:</b>\n{new_text}",
        parse_mode="HTML")

@dp.message(Command("plan"))
async def cmd_plan(message: Message):
    """Manually trigger the standup prompt."""
    g = get_group_by_chat_id(message.chat.id)
    if not g:
        await message.reply("This command is only available in the designated team chats.")
        return
    if g["topic_id"] and message.message_thread_id != g["topic_id"]:
        await message.reply("📌 Please use this command in the standup topic.", parse_mode="HTML")
        return
    await send_standup_prompt(g)

@dp.message(F.reply_to_message, ~Command("plan"), ~Command("summary"))
async def handle_replies(message: Message):
    """Listens for replies to the bot's standup prompt and saves them."""
    g = get_group_by_chat_id(message.chat.id)
    if not g:
        return
    if g["topic_id"] and message.message_thread_id != g["topic_id"]:
        return
    if message.reply_to_message.from_user.id != bot.id:
        return

    gkey = g["key"]
    gdata = load_group_data(gkey)
    today = get_today_str()
    if today not in gdata:
        gdata[today] = {}
    if "replies" not in gdata[today]:
        gdata[today]["replies"] = {}

    user_id = str(message.from_user.id)
    name = message.from_user.full_name or message.from_user.username or user_id
    username = message.from_user.username

    if username:
        save_user_mapping(gkey, username, message.from_user.id)

    text_lower = (message.text or "").lower()
    if user_id in gdata[today]["replies"]:
        if not any(w in text_lower for w in ["yesterday", "today", "вчера", "сегодня"]):
            old_text = gdata[today]["replies"][user_id]["text"]
            gdata[today]["replies"][user_id]["text"] = old_text + "\n<b>Update:</b>\n" + parse_jira_links(message.text, gkey)
            save_group_data(gkey, gdata)
            await message.reply("✅ Your update has been appended!" if "marketing" not in g["name"].lower() else "✅ Ваше обновление добавлено!")
            return

    ai_result = await validate_standup_with_ai(message.text, gkey)
    if ai_result and not ai_result["valid"]:
        is_ru = "marketing" in g["name"].lower()
        if is_ru:
            await message.reply(
                f"⚠️ <b>Пожалуйста, опишите ваши задачи подробнее!</b>\n\n{ai_result['reason']}\n\n"
                f"<i>Пример:</i>\nВчера: сделал дизайн для ...\nСегодня: пишу посты для ...",
                parse_mode="HTML")
        else:
            await message.reply(
                f"⚠️ <b>Please describe your tasks properly!</b>\n\n{ai_result['reason']}\n\n"
                f"<i>Example:</i>\nYesterday: finished CDT-344\nToday: working on CDT-376",
                parse_mode="HTML")
        return

    linked_text = parse_jira_links(message.text, gkey)
    gdata[today]["replies"][user_id] = {
        "name": name, "username": username,
        "text": linked_text, "time": datetime.now().strftime("%H:%M:%S")
    }
    save_group_data(gkey, gdata)
    await message.reply("✅ Plan recorded! Thank you.")

@dp.callback_query(F.data == "append_standup_btn")
async def cb_append_standup_btn(callback: CallbackQuery):
    await callback.answer("Just reply to the bot's standup message to append!", show_alert=True)

async def check_missing_standups(group, force=False):
    """Pings employees who haven't submitted their standup today."""
    gkey = group["key"]
    chat_id = group["chat_id"]
    topic_id = group["topic_id"]
    gdata = load_group_data(gkey)
    if not force and gdata.get("pinging_paused", False):
        return
    emps = get_employees(gkey)
    if not emps:
        return
    today = get_today_str()
    if today not in gdata or "prompt_message_id" not in gdata[today]:
        if datetime.now().hour >= 12:
            return
        await send_standup_prompt(group)
        gdata = load_group_data(gkey)
        if today not in gdata or "prompt_message_id" not in gdata[today]:
            return
    replies = gdata[today].get("replies", {})
    submitted_usernames = {info["username"].lower() for info in replies.values() if info.get("username")}
    submitted_uids = {str(uid) for uid in replies.keys()}
    user_map = get_user_map(gkey)
    missing = []
    for e in emps:
        uid = str(user_map.get(e, ""))
        if e not in submitted_usernames and uid not in submitted_uids and e not in submitted_uids:
            missing.append(build_mention(e, user_map))
    if missing:
        old_ping_id = gdata[today].get("ping_message_id")
        if old_ping_id:
            try: await bot.delete_message(chat_id=chat_id, message_id=old_ping_id)
            except Exception: pass
        if "marketing" in group.get("name", "").lower():
            text = f"⏳ {' '.join(missing)}\nМы всё ещё ждём ваш стендап!\n\nПожалуйста, ответьте на утреннее сообщение."
        else:
            text = f"⏳ {' '.join(missing)}\nWe're still waiting for your Daily Standup!\n\nPlease reply to the morning standup message."
        sent_msg = None
        try:
            prompt_id = gdata[today]["prompt_message_id"]
            sent_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                              reply_to_message_id=prompt_id, message_thread_id=topic_id)
        except Exception:
            try: sent_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", message_thread_id=topic_id)
            except Exception as e: logging.error(f"Failed to ping {gkey}: {e}")
        if sent_msg:
            gdata[today]["ping_message_id"] = sent_msg.message_id
            save_group_data(gkey, gdata)

async def report_missing_standups_at_noon(group, force=False):
    gkey = group["key"]
    gdata = load_group_data(gkey)
    if not force and gdata.get("pinging_paused", False):
        return
    emps = get_employees(gkey)
    if not emps:
        return
    today = get_today_str()
    if today not in gdata or "prompt_message_id" not in gdata[today]:
        return
    replies = gdata[today].get("replies", {})
    submitted_usernames = {info["username"].lower() for info in replies.values() if info.get("username")}
    submitted_uids = {str(uid) for uid in replies.keys()}
    user_map = get_user_map(gkey)
    missing = []
    for e in emps:
        uid = str(user_map.get(e, ""))
        if e not in submitted_usernames and uid not in submitted_uids and e not in submitted_uids:
            missing.append(build_mention(e, user_map))
    if missing:
        g = get_group_by_key(gkey)
        text = f"🚨 <b>{g['name']} — 12:00 Report</b>\n\nMissing standups:\n{', '.join(missing)}"
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except Exception as e: logging.error(f"Noon report error: {e}")

async def auto_send_summary(group):
    gkey = group["key"]
    chat_id = group["chat_id"]
    topic_id = group["topic_id"]
    gdata = load_group_data(gkey)
    today = get_today_str()
    emps = get_employees(gkey)
    day = gdata.get(today, {})
    is_marketing = "marketing" in group.get("name", "").lower()
    if not day.get("replies"):
        empty_text = "📋 Сегодня нет отчетов." if is_marketing else "📋 No standup reports today."
        try: await bot.send_message(chat_id=chat_id, text=empty_text, message_thread_id=topic_id)
        except Exception: pass
        return
    header = f"📋 <b>Стендап {today}</b>\n" if is_marketing else f"📋 <b>Standup {today}</b>\n"
    lines = [header]
    for uid, info in day["replies"].items():
        role = emps.get((info.get("username") or "").lower(), "Developer")
        lines.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")
    try: await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML", message_thread_id=topic_id)
    except Exception as e: logging.error(f"Summary error {gkey}: {e}")

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    g = get_group_by_chat_id(message.chat.id)
    if not g:
        await message.reply("This command is only available in the designated team chats.")
        return
    if g["topic_id"] and message.message_thread_id != g["topic_id"]:
        await message.reply("📌 Please use this in the standup topic.", parse_mode="HTML")
        return
    gkey = g["key"]
    gdata = load_group_data(gkey)
    today = get_today_str()
    day = gdata.get(today, {})
    if not day.get("replies"):
        await message.reply("No plans submitted for today yet.")
        return
    emps = get_employees(gkey)
    lines = [f"📋 <b>Standup {today}</b>\n"]
    for uid, info in day["replies"].items():
        role = emps.get((info.get("username") or "").lower(), "Developer")
        lines.append(f"👨‍💻 <b>{info['name']}</b> ({role}) ({info['time']}):\n{info['text']}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")

async def auto_pause_pinging_for_group(group):
    gkey = group["key"]
    gdata = load_group_data(gkey)
    gdata["pinging_paused"] = True
    save_group_data(gkey, gdata)
    logging.info(f"Auto-paused pinging for {gkey} at 12:00")

async def reset_pinging_for_group(group):
    gkey = group["key"]
    gdata = load_group_data(gkey)
    gdata["pinging_paused"] = False
    save_group_data(gkey, gdata)
    logging.info(f"Auto-resumed pinging for {gkey} at 09:00")

async def auto_pause_all():
    for g in get_all_groups():
        await auto_pause_pinging_for_group(g)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="⏰ 12:00 PM — pinging paused for all groups.\nUse /start to manage.",
                parse_mode="HTML")
        except Exception:
            pass

async def reset_all():
    for g in get_all_groups():
        await reset_pinging_for_group(g)

async def check_all():
    for g in get_all_groups():
        await check_missing_standups(g)

async def noon_report_all():
    for g in get_all_groups():
        await report_missing_standups_at_noon(g)

async def summary_all():
    for g in get_all_groups():
        await auto_send_summary(g)

async def standup_prompt_all():
    for g in get_all_groups():
        await send_standup_prompt(g)

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    """Listen to the bot being added/removed from groups to discover them."""
    new_state = update.new_chat_member.status
    chat = update.chat
    
    data = load_data()
    discovered = data.get("discovered_groups", {})
    
    if new_state in ["member", "administrator"]:
        # We got added!
        discovered[str(chat.id)] = chat.title or str(chat.id)
    else:
        # We got removed or kicked
        if str(chat.id) in discovered:
            del discovered[str(chat.id)]
            
    data["discovered_groups"] = discovered
    save_data(data)

@dp.callback_query(F.data == "add_group_menu")
async def cb_add_group_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    
    data = load_data()
    discovered = data.get("discovered_groups", {})
    
    if not discovered:
        await callback.message.reply(
            "🤷‍♂️ <b>No new groups found!</b>\n\n"
            "To add a group:\n"
            "1. Add this bot to the new Telegram group.\n"
            "2. Come back here and click <b>➕ Add Group</b> again.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
        
    # Check which discovered groups are already active
    active_chat_ids = [str(g["chat_id"]) for g in get_all_groups()]
    
    available_to_add = {}
    for cid, title in discovered.items():
        if cid not in active_chat_ids:
            available_to_add[cid] = title
            
    if not available_to_add:
        await callback.message.reply("All groups the bot is in are already active!")
        await callback.answer()
        return
        
    kb = []
    for cid, title in available_to_add.items():
        kb.append([InlineKeyboardButton(text=f"✅ Activate: {title}", callback_data=f"activate_grp_{cid}")])
        
    await callback.message.reply(
        "<b>Found the following unregistered groups!</b>\n"
        "Click one to start standup automation for it:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("activate_grp_"))
async def cb_activate_grp(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    cid = callback.data.replace("activate_grp_", "")
    
    data = load_data()
    discovered = data.get("discovered_groups", {})
    
    title = discovered.get(cid, f"Group {cid}")
    
    dyn_groups = data.get("dynamic_groups", [])
    # Generate a unique key
    new_key = f"dyn_{len(dyn_groups) + 1}_{cid.replace('-', '')}"
    
    new_group = {
        "key": new_key,
        "chat_id": cid,
        "topic_id": None, # Assuming no specific topic by default
        "name": title
    }
    
    dyn_groups.append(new_group)
    data["dynamic_groups"] = dyn_groups
    save_data(data)
    
    await callback.message.reply(f"🎉 <b>{title}</b> has been successfully activated and added to the bot!\n"
                                 f"Go to the main menu to configure its employees.", parse_mode="HTML")
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()

async def main():
    migrate_data_if_needed()

    scheduler.add_job(standup_prompt_all, "cron", day_of_week="mon-fri", hour=9, minute=0)
    scheduler.add_job(check_all, "cron", day_of_week="mon-fri", hour="9-17", minute="5,20,35,50")
    scheduler.add_job(auto_pause_all, "cron", day_of_week="mon-fri", hour=12, minute=0)
    scheduler.add_job(reset_all, "cron", day_of_week="mon-fri", hour=9, minute=0)
    scheduler.add_job(summary_all, "cron", day_of_week="mon-fri", hour=18, minute=0)
    scheduler.add_job(noon_report_all, "cron", day_of_week="mon-fri", hour=12, minute=0)
    scheduler.start()

    logging.info("Starting bot polling...")
    await bot.set_my_commands([
        BotCommand(command="start", description="Admin Control Panel (DM)"),
        BotCommand(command="add", description="Add employee: /add group1 @user Role"),
        BotCommand(command="remove", description="Remove employee: /remove group1 @user"),
        BotCommand(command="list", description="Employee list (DM)"),
        BotCommand(command="plan", description="Request standup (Group)"),
        BotCommand(command="summary", description="Standup summary (Group)")
    ])
    await bot.delete_webhook(drop_pending_updates=True)

    from aiohttp import web as aio_web

    async def handle_dashboard(request):
        return aio_web.FileResponse('./dashboard.html')

    async def handle_api_data(request):
        return aio_web.json_response(load_data())

    app = aio_web.Application()
    app.router.add_get('/', handle_dashboard)
    app.router.add_get('/api/data', handle_api_data)

    port = int(os.getenv('PORT', 8080))
    runner = aio_web.AppRunner(app)
    await runner.setup()
    site = aio_web.TCPSite(runner, '91.90.216.119', port)
    await site.start()
    logging.info(f"Dashboard running on port {port}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

