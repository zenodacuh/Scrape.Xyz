"""
MERCURY — Discord Bot
Deploy on Railway. Scrapes Pasteview every 1 minute, posts to 3 channels.
"""

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]
CHANNEL_ID     = int(os.environ["CHANNEL_ID"])       # posts all found URLs as .txt every minute
NEW_CHANNEL_ID = int(os.environ["NEW_CHANNEL_ID"])   # posts only new (never-seen) URLs as alerts
CONTENT_CHANNEL_ID = int(os.environ["CONTENT_CHANNEL_ID"])  # grabs first 5 paste contents, sends combined .txt

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

# ─── SEEN URLS (in-memory, tracks what NEW_CHANNEL has already received) ─────
posted_urls: set = set()


# ─── SCRAPER — archive list ───────────────────────────────────────────────────
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

    # Deduplicate within this run
    seen_urls = set()
    unique = []
    for item in found:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            unique.append(item)

    return unique


# ─── SCRAPER — paste content extractor ───────────────────────────────────────
def extract_credentials(raw: str) -> list[str]:
    """Pull only email:password lines from raw paste content."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        # Must contain @ and : and look like an email combo
        if "@" in line and ":" in line:
            parts = line.split(":", 1)
            if len(parts) == 2 and "@" in parts[0] and "." in parts[0]:
                lines.append(line)
    return lines


async def extract_paste_contents(pastes: list[dict], limit: int = 5) -> str:
    """Visit up to `limit` paste URLs, extract credential lines only."""
    combined = []
    targets = pastes[:limit]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        for item in targets:
            url = item["url"]
            log.info(f"Extracting content from {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(1500)

                # Try ace editor JS API first
                raw = await page.evaluate("""
                    () => {
                        if (window.ace) {
                            const editors = document.querySelectorAll('.ace_editor');
                            for (let ed of editors) {
                                try {
                                    const val = ace.edit(ed).getValue();
                                    if (val && val.trim()) return val;
                                } catch(e) {}
                            }
                        }
                        const edEl = document.querySelector('.ace_editor');
                        if (edEl && edEl.env && edEl.env.editor)
                            return edEl.env.editor.getValue();
                        return null;
                    }
                """)

                # Fallback: scroll and collect ace lines
                if not raw or not raw.strip():
                    await page.evaluate("""
                        () => {
                            const s = document.querySelector('.ace_scroller');
                            if (s) s.scrollTop = s.scrollHeight;
                        }
                    """)
                    await page.wait_for_timeout(800)
                    lines = await page.query_selector_all("div.ace_line")
                    raw = "\n".join([
                        (await l.text_content() or "").strip()
                        for l in lines
                    ])

                # Fallback: pre tag
                if not raw or not raw.strip():
                    pre = await page.query_selector("pre")
                    if pre:
                        raw = await pre.text_content()

                if raw and raw.strip():
                    creds = extract_credentials(raw)
                    if creds:
                        block = "\n".join(creds)
                        combined.append(f"# {item['title']}\n# {url}\n{block}")
                        log.info(f"Extracted {len(creds)} credential lines from {url}")
                    else:
                        log.info(f"No credential lines found in {url}")

            except Exception as e:
                log.error(f"Failed to extract {url}: {e}")

        await browser.close()

    return "\n\n".join(combined)


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
    for item in pastes:
        try:
            await channel.send(f"= DETECTED {1} NEW URL =\n{item['url']}")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Failed to post alert: {e}")


async def post_combined_content(channel, pastes: list[dict]):
    if not pastes:
        return
    try:
        combined = await extract_paste_contents(pastes, limit=5)
        if not combined.strip():
            log.info("No content extracted for content channel")
            return
        filename = f"content_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        file = discord.File(fp=io.BytesIO(combined.encode()), filename=filename)
        await channel.send(file=file)
        log.info("Posted combined content file")
    except Exception as e:
        log.error(f"Failed to post combined content: {e}")


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

    # Channel 1 — post all found URLs as .txt
    await post_pastes(channel, pastes)

    # Compute new pastes once — used by both channel 2 and channel 3
    new_pastes = [p for p in pastes if p["url"] not in posted_urls]

    # Channel 2 — post only new (never-seen) URLs as alerts
    try:
        new_channel = bot.get_channel(NEW_CHANNEL_ID) or await bot.fetch_channel(NEW_CHANNEL_ID)
        if new_pastes:
            await post_new_alerts(new_channel, new_pastes)
            log.info(f"Posted {len(new_pastes)} new link(s) to new channel")
    except Exception as e:
        log.error(f"Could not post to new channel: {e}")

    # Channel 3 — grab content of first 5 new pastes, combine, send as .txt
    try:
        content_channel = bot.get_channel(CONTENT_CHANNEL_ID) or await bot.fetch_channel(CONTENT_CHANNEL_ID)
        await post_combined_content(content_channel, new_pastes)
    except Exception as e:
        log.error(f"Could not post to content channel: {e}")

    # Mark all new pastes as seen AFTER both channels are done
    for p in new_pastes:
        posted_urls.add(p["url"])


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
