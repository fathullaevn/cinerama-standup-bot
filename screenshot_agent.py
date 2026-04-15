"""
Cinerama Screenshot Agent — runs on your PC permanently.
Polls the bot API for screenshot requests and captures Grafana/Superset dashboards.

Usage: python screenshot_agent.py
"""
import asyncio
import os
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_ID", "").split(",") if a.strip()]

# Grafana config
GRAFANA_URL = os.getenv("GRAFANA_URL", "https://bi.cinerama.uz/d/b4233477-01c3-4642-9c4e-1077f48bb7d1/tariffs?orgId=1&from=now%2FM&to=now%2FM&timezone=browser&var-tariff=$__all")
GRAFANA_USER = os.getenv("GRAFANA_USER", "nuriddin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "nuriddin")

# Superset config
SUPERSET_URL = os.getenv("SUPERSET_URL", "https://ba.cinerama.uz/superset/dashboard/55/?native_filters_key=oga88LqofF8")
SUPERSET_USER = os.getenv("SUPERSET_USER", "nuriddin")
SUPERSET_PASS = os.getenv("SUPERSET_PASS", "nuriddin")

BOT_API_URL = "https://cinerama-standup-bot-fa4c6c6e5bd5.herokuapp.com"
POLL_INTERVAL = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


async def screenshot_grafana():
    """Take Grafana dashboard screenshot."""
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
        await page.goto(GRAFANA_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)

        screenshot = await page.screenshot(full_page=True, type="png")
        logging.info(f"[Grafana] Captured: {len(screenshot)} bytes")
        await browser.close()
        return screenshot


async def screenshot_superset():
    """Take Superset dashboard screenshot."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080}, ignore_https_errors=True)
        page = await context.new_page()

        login_url = SUPERSET_URL.split('/superset/')[0] + '/login/'
        logging.info(f"[Superset] Logging in at {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        await page.fill('input#username', SUPERSET_USER)
        await page.fill('input#password', SUPERSET_PASS)
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_timeout(3000)

        logging.info("[Superset] Loading dashboard...")
        await page.goto(SUPERSET_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)

        screenshot = await page.screenshot(full_page=True, type="png")
        logging.info(f"[Superset] Captured: {len(screenshot)} bytes")
        await browser.close()
        return screenshot


async def send_photo(screenshot_bytes, caption):
    """Send screenshot to all admins."""
    import aiohttp
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    for admin_id in ADMIN_IDS:
        form = aiohttp.FormData()
        form.add_field("chat_id", admin_id)
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field("photo", screenshot_bytes, filename="dashboard.png", content_type="image/png")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form) as resp:
                if resp.status == 200:
                    logging.info(f"✅ Sent to admin {admin_id}")
                else:
                    text = await resp.text()
                    logging.error(f"❌ Failed for {admin_id}: {text}")


async def check_for_request():
    """Check if a screenshot was requested."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BOT_API_URL}/api/screenshot_check", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("requested", False)
    except Exception:
        pass
    return False


async def clear_request():
    """Clear the screenshot request flag."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(f"{BOT_API_URL}/api/screenshot_clear", timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


async def process_request(target):
    """Process a screenshot request."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    if target in ("grafana", "both"):
        try:
            img = await screenshot_grafana()
            if img:
                await send_photo(img, f"📊 <b>Grafana — Tariffs</b>\n{now}")
        except Exception as e:
            logging.error(f"Grafana failed: {e}")

    if target in ("superset", "both"):
        try:
            img = await screenshot_superset()
            if img:
                await send_photo(img, f"📊 <b>Superset — Daily Results</b>\n{now}")
        except Exception as e:
            logging.error(f"Superset failed: {e}")


async def main():
    logging.info("🖥️  Cinerama Screenshot Agent started!")
    logging.info(f"Polling {BOT_API_URL} every {POLL_INTERVAL}s...")
    logging.info("Press Ctrl+C to stop.\n")

    while True:
        try:
            target = await check_for_request()
            if target and target is not False:
                logging.info(f"📸 Request: {target}")
                await clear_request()
                await process_request(target)
                logging.info("✅ Done! Waiting for next request...\n")
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"Error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
