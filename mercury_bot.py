"""
MERCURY — Discord Bot
Deploy on Railway. Scrapes Pasteview every 1 minute, posts .txt to Discord.
"""

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]
CHANNEL_ID     = int(os.environ["CHANNEL_ID"])      # posts everything every minute
NEW_CHANNEL_ID = int(os.environ["NEW_CHANNEL_ID"])  # posts only new (never-seen) links

CHECK_INTERVAL = 1
PAGES_TO_SCAN  = 5
ARCHIVE_URL    = "https://pasteview.com/paste-archive"

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mercury")


# ─── SEEN URLS (in-memory, tracks what NEW_CHANNEL has already received) ───────
posted_urls: set = set()

# ─── SCRAPER ─────────────────────────────────────────────────────────────────
async def scrape_pasteview(num_pages: int = PAGES_TO_SCAN) -> list[dict]:
    found = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        try:
            await page.goto(ARCHIVE_URL, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            for page_num in range(1, num_pages + 1):
                if page_num > 1:
                    navigated = False
                    buttons = await page.query_selector_all("button")
                    for btn in buttons:
                        text = await btn.text_content()
                        if text and text.strip().lower() in ["next", ">", "»", "→", "▶"]:
                            disabled = await btn.get_attribute("disabled")
                            aria_dis = await btn.get_attribute("aria-disabled")
                            if disabled is not None or aria_dis == "true":
                                log.info(f"Last page at {page_num - 1}")
                                break
                            await btn.click()
                            await page.wait_for_timeout(2500)
                            navigated = True
                            break
                    if not navigated:
                        break

                matches = await page.evaluate("""
                    () => {
                        const results = [];
                        for (const a of document.querySelectorAll('a')) {
                            const text = a.innerText || a.textContent || '';
                            if (text.toLowerCase().includes('hotmail')) {
                                const href = a.href;
                                if (href
                                    && !href.includes('/paste-archive')
                                    && !href.includes('/new')
                                    && !href.endsWith('/')
                                    && href !== window.location.href) {
                                    results.push({
                                        title: text.trim().replace(/\\s+/g, ' '),
                                        url: href
                                    });
                                }
                            }
                        }
                        return results;
                    }
                """)

                log.info(f"Page {page_num}: {len(matches)} match(es)")
                found.extend(matches)

        except Exception as e:
            log.error(f"Scraper error: {e}")
        finally:
            await browser.close()

    seen_urls = set()
    unique = []
    for item in found:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            unique.append(item)

    return unique


# ─── BOT ─────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


async def post_pastes(channel, pastes: list[dict]):
    if not pastes:
        return
    try:
        content = "\n".join(item["url"] for item in pastes)
        filename = f"hotmail_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        file = discord.File(fp=io.BytesIO(content.encode()), filename=filename)
        await channel.send(file=file)
    except Exception as e:
        log.error(f"Failed to post file: {e}")


async def post_new_alerts(channel, pastes: list[dict]):
    """Post individual alert messages for new channel."""
    for item in pastes:
        try:
            await channel.send(f"= DETECTED {1} NEW URL =\n{item['url']}")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Failed to post alert: {e}")


# ─── BACKGROUND TASK ─────────────────────────────────────────────────────────
@tasks.loop(minutes=CHECK_INTERVAL)
async def monitor_loop():
    try:
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    except Exception as e:
        log.error(f"Could not get channel: {e}")
        return

    log.info("Running scheduled check...")
    pastes = await scrape_pasteview(PAGES_TO_SCAN)

    # Channel 1 — post everything every run
    await post_pastes(channel, pastes)

    # Channel 2 — post only links never seen before
    try:
        new_channel = bot.get_channel(NEW_CHANNEL_ID) or await bot.fetch_channel(NEW_CHANNEL_ID)
        new_pastes = [p for p in pastes if p["url"] not in posted_urls]
        if new_pastes:
            await post_new_alerts(new_channel, new_pastes)
            for p in new_pastes:
                posted_urls.add(p["url"])
            log.info(f"Posted {len(new_pastes)} new link(s) to new channel")
    except Exception as e:
        log.error(f"Could not post to new channel: {e}")


@monitor_loop.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ─── SLASH COMMAND ───────────────────────────────────────────────────────────
@tree.command(name="scrape", description="Manually trigger a scrape right now")
@app_commands.describe(pages="Number of archive pages to scan (default: 5)")
async def cmd_scrape(interaction: discord.Interaction, pages: int = PAGES_TO_SCAN):
    await interaction.response.send_message(f"🔴 Scanning {pages} page(s)...", ephemeral=True)
    pastes = await scrape_pasteview(pages)
    channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    await post_pastes(channel, pastes)
    await interaction.followup.send(f"✅ Done — {len(pastes)} posted.", ephemeral=True)


# ─── EVENTS ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")
    monitor_loop.start()
    log.info(f"Monitor started — checking every {CHECK_INTERVAL} minute(s)")


# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
