"""
Local screenshot agent — runs on your machine (which has access to bi.cinerama.uz).
Takes Grafana screenshots and sends them to Telegram admins via the bot.

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

logging.basicConfig(level=logging.INFO)


async def take_screenshot():
    """Take a screenshot of Grafana dashboard using Playwright."""
    from playwright.async_api import async_playwright

    logging.info(f"Taking screenshot of {GRAFANA_URL}")

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
        logging.info("Navigating to dashboard...")
        await page.goto(GRAFANA_URL, wait_until="domcontentloaded", timeout=60000)

        # Wait for all charts to render
        logging.info("Waiting for charts to render...")
        await page.wait_for_timeout(8000)

        # Take screenshot
        screenshot = await page.screenshot(full_page=True, type="png")
        logging.info(f"Screenshot taken: {len(screenshot)} bytes")

        await browser.close()
        return screenshot


async def send_to_admins(screenshot_bytes):
    """Send screenshot to all admins via the bot."""
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


async def main():
    screenshot = await take_screenshot()
    if screenshot:
        await send_to_admins(screenshot)
        logging.info("Done!")
    else:
        logging.error("Failed to take screenshot")


if __name__ == "__main__":
    asyncio.run(main())
