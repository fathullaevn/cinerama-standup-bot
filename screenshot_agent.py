"""
Cinerama Screenshot Agent — runs on your PC permanently.
Polls the bot API for screenshot requests and captures Grafana dashboards.

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
GRAFANA_URL = os.getenv("GRAFANA_URL", "https://bi.cinerama.uz/d/b4233477-01c3-4642-9c4e-1077f48bb7d1/tariffs?orgId=1&from=now%2FM&to=now%2FM&timezone=browser&var-tariff=$__all")
GRAFANA_USER = os.getenv("GRAFANA_USER", "nuriddin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "nuriddin")
BOT_API_URL = "https://cinerama-standup-bot-fa4c6c6e5bd5.herokuapp.com"

POLL_INTERVAL = 5  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


async def take_screenshot():
    """Take a screenshot of Grafana dashboard using Playwright."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True
        )
        page = await context.new_page()

        # Login to Grafana
        login_url = GRAFANA_URL.split('/d/')[0] + '/login'
        logging.info(f"Logging in at {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        await page.fill('input[name="user"]', GRAFANA_USER)
        await page.fill('input[name="password"]', GRAFANA_PASS)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(3000)

        # Navigate to dashboard
        logging.info("Loading dashboard...")
        await page.goto(GRAFANA_URL, wait_until="domcontentloaded", timeout=60000)

        # Wait for charts to render
        await page.wait_for_timeout(8000)

        screenshot = await page.screenshot(full_page=True, type="png")
        logging.info(f"Screenshot captured: {len(screenshot)} bytes")

        await browser.close()
        return screenshot


async def send_to_admins(screenshot_bytes):
    """Send screenshot to all admins via Telegram Bot API."""
    import aiohttp

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    caption = f"📊 <b>Grafana Dashboard</b>\n{datetime.now().strftime('%Y-%m-%d %H:%M')}"

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
                    logging.error(f"❌ Failed for admin {admin_id}: {text}")


async def check_for_request():
    """Check if a screenshot was requested via the bot."""
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


async def main():
    logging.info("🖥️  Cinerama Screenshot Agent started!")
    logging.info(f"Polling {BOT_API_URL} every {POLL_INTERVAL}s for screenshot requests...")
    logging.info("Press Ctrl+C to stop.\n")

    while True:
        try:
            if await check_for_request():
                logging.info("📸 Screenshot requested! Capturing...")
                await clear_request()

                screenshot = await take_screenshot()
                if screenshot:
                    await send_to_admins(screenshot)
                    logging.info("✅ Done! Waiting for next request...\n")
                else:
                    logging.error("❌ Failed to capture screenshot\n")
        except KeyboardInterrupt:
            logging.info("Agent stopped.")
            break
        except Exception as e:
            logging.error(f"Error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
