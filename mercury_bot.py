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
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
CHANNEL_ID         = int(os.environ["CHANNEL_ID"])          # all found URLs as .txt every minute
NEW_CHANNEL_ID     = int(os.environ["NEW_CHANNEL_ID"])      # new URL alerts only
CONTENT_CHANNEL_ID = int(os.environ["CONTENT_CHANNEL_ID"]) # extracted credentials from new pastes

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

# ─── STATE ───────────────────────────────────────────────────────────────────
posted_urls: set = set()

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def extract_credentials(raw: str) -> list[str]:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if "@" in line and ":" in line:
            parts = line.split(":", 1)
            if len(parts) == 2 and "@" in parts[0] and "." in parts[0]:
                lines.append(line)
    return lines

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
        await channel.send(file=discord.File(fp=io.BytesIO(content.encode()), filename=filename))
    except Exception as e:
        log.error(f"Failed to post file: {e}")


async def post_new_alerts(channel, pastes: list[dict]):
    for item in pastes:
        try:
            await channel.send(f"= DETECTED 1 NEW URL =\n{item['url']}")
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        try:
            # ── Step 1: scrape archive for hotmail paste URLs ──────────────
            await page.goto(ARCHIVE_URL, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            found = []
            for page_num in range(1, PAGES_TO_SCAN + 1):
                if page_num > 1:
                    navigated = False
                    buttons = await page.query_selector_all("button")
                    for btn in buttons:
                        text = await btn.text_content()
                        if text and text.strip().lower() in ["next", ">", "»", "→", "▶"]:
                            disabled = await btn.get_attribute("disabled")
                            aria_dis = await btn.get_attribute("aria-disabled")
                            if disabled is not None or aria_dis == "true":
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

            # Deduplicate within this run
            seen_this_run = set()
            pastes = []
            for item in found:
                if item["url"] not in seen_this_run:
                    seen_this_run.add(item["url"])
                    pastes.append(item)

            # ── Step 2: channel 1 — all found URLs as .txt ─────────────────
            await post_pastes(channel, pastes)

            # ── Step 3: figure out which are new & mark them seen NOW ───
            new_pastes = [p for p in pastes if p["url"] not in posted_urls]
            if not new_pastes:
                log.info("No new pastes this run")
                return

            # Mark seen immediately so even if steps below crash, we don't repeat
            for p in new_pastes:
                posted_urls.add(p["url"])

            log.info(f"{len(new_pastes)} new paste(s) detected")

            # ── Step 4: channel 2 — new URL alerts ────────────────────────
            try:
                new_channel = bot.get_channel(NEW_CHANNEL_ID) or await bot.fetch_channel(NEW_CHANNEL_ID)
                await post_new_alerts(new_channel, new_pastes)
            except Exception as e:
                log.error(f"Could not post to new channel: {e}")

            # ── Step 5: channel 3 — visit each new paste, extract creds ───
            try:
                content_channel = bot.get_channel(CONTENT_CHANNEL_ID) or await bot.fetch_channel(CONTENT_CHANNEL_ID)
                combined = []

                for item in new_pastes[:5]:
                    url = item["url"]
                    log.info(f"Extracting content from {url}")
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=15000)
                        await page.wait_for_timeout(1500)

                        # Try ace editor API
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

                        # Fallback: scroll ace lines
                        if not raw or not raw.strip():
                            await page.evaluate("""
                                () => {
                                    const s = document.querySelector('.ace_scroller');
                                    if (s) s.scrollTop = s.scrollHeight;
                                }
                            """)
                            await page.wait_for_timeout(800)
                            lines = await page.query_selector_all("div.ace_line")
                            raw = "\n".join([(await l.text_content() or "").strip() for l in lines])

                        # Fallback: pre tag
                        if not raw or not raw.strip():
                            pre = await page.query_selector("pre")
                            if pre:
                                raw = await pre.text_content()

                        if raw and raw.strip():
                            creds = extract_credentials(raw)
                            if creds:
                                combined.append(f"# {item['title']}\n# {url}\n" + "\n".join(creds))
                                log.info(f"Got {len(creds)} credential lines from {url}")
                            else:
                                log.info(f"No credential lines in {url}")
                        else:
                            log.info(f"No content extracted from {url}")

                    except Exception as e:
                        log.error(f"Failed to extract {url}: {e}")

                if combined:
                    output = "\n\n".join(combined)
                    filename = f"content_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
                    await content_channel.send(file=discord.File(fp=io.BytesIO(output.encode()), filename=filename))
                    log.info("Posted combined content file")
                else:
                    log.info("Nothing to post to content channel")

            except Exception as e:
                log.error(f"Could not post to content channel: {e}")

        except Exception as e:
            log.error(f"Monitor loop error: {e}")
        finally:
            await browser.close()


@monitor_loop.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ─── SLASH COMMAND ───────────────────────────────────────────────────────────
@tree.command(name="scrape", description="Manually trigger a scrape right now")
@app_commands.describe(pages="Number of archive pages to scan (default: 5)")
async def cmd_scrape(interaction: discord.Interaction, pages: int = PAGES_TO_SCAN):
    await interaction.response.send_message(f"🔴 Scanning {pages} page(s)...", ephemeral=True)
    channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    # Trigger a one-off run of the loop logic
    await monitor_loop()
    await interaction.followup.send("✅ Done.", ephemeral=True)


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
