"""
Polymarket Lucknow Temperature Market — Discord Notifier Bot
=============================================================
✅ 1-minute polling
✅ Discord alerts on every error
✅ Auto-retry with exponential backoff
✅ Auto month/year handling (works forever)
✅ Heartbeat health checks every 6 hours
✅ Full logging to file + console
✅ Auto-pause after 10 consecutive errors
✅ Commands: !status !check !pause !resume !help

SETUP:
1. pip install discord.py aiohttp
2. Create bot at https://discord.com/developers/applications
3. Fill in BOT_TOKEN and CHANNEL_ID below
4. python polymarket_bot.py
"""

import discord
import aiohttp
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────
BOT_TOKEN              = "YOUR_BOT_TOKEN_HERE"
CHANNEL_ID             = 123456789012345678

POLL_INTERVAL_SECONDS  = 60        # Poll every 1 minute
MAX_RETRIES            = 3         # Retries per failed check
RETRY_BACKOFF_SECONDS  = 10        # Wait between retries
ERROR_COOLDOWN_MINUTES = 5         # Min gap between error Discord alerts
HEARTBEAT_HOURS        = 6         # Send alive ping every N hours
IST                    = ZoneInfo("Asia/Kolkata")
LOG_FILE               = "polymarket_bot.log"
# ─────────────────────────────────────────────────────

# ── Logging ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("polymarket-bot")

# ── Discord ───────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)

# ── State ─────────────────────────────────────────────
state = {
    "paused":             False,
    "last_error_alert":   None,
    "consecutive_errors": 0,
    "total_checks":       0,
    "successful_checks":  0,
    "last_check_time":    None,
    "last_check_result":  "—",
    "started_at":         None,
    "markets_found":      0,
}

GAMMA_API = "https://gamma-api.polymarket.com/markets"
MONTHS    = ["january","february","march","april","may","june",
             "july","august","september","october","november","december"]


# ═══════════════════════════════════════════════════════
#  CUSTOM EXCEPTIONS
# ═══════════════════════════════════════════════════════

class RateLimitError(Exception):
    def __init__(self, msg, retry_after=60):
        super().__init__(msg)
        self.retry_after = retry_after

class ServerError(Exception): pass
class APIError(Exception):    pass


# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════

def get_target_date() -> datetime:
    return datetime.now(IST) + timedelta(days=1)

def build_search_url(target: datetime) -> str:
    month = MONTHS[target.month - 1].capitalize()
    return f"{GAMMA_API}?question=Highest+temperature+in+Lucknow+on+{month}+{target.day}"

def build_market_url(market: dict, target: datetime) -> str:
    month = MONTHS[target.month - 1]
    slug  = market.get("slug") or \
            f"highest-temperature-in-lucknow-on-{month}-{target.day}-{target.year}"
    return f"https://polymarket.com/event/{slug}"

def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

def uptime_str() -> str:
    if not state["started_at"]:
        return "—"
    delta  = datetime.now(IST) - state["started_at"]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

async def get_channel():
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        log.error(f"Channel {CHANNEL_ID} not found.")
    return ch


# ═══════════════════════════════════════════════════════
#  ERROR ALERTING
# ═══════════════════════════════════════════════════════

async def send_error_alert(title: str, description: str, critical: bool = False):
    now = datetime.now(IST)

    # Cooldown — skip non-critical repeats
    if not critical and state["last_error_alert"]:
        elapsed = (now - state["last_error_alert"]).total_seconds() / 60
        if elapsed < ERROR_COOLDOWN_MINUTES:
            log.warning(f"Error alert suppressed (cooldown). {title}")
            return

    state["last_error_alert"] = now
    ch = await get_channel()
    if not ch:
        return

    embed = discord.Embed(
        title       = f"{'🚨' if critical else '⚠️'} {title}",
        description = description,
        color       = 0xff3b5c if critical else 0xff6b35
    )
    embed.add_field(name="Time",               value=now_ist_str(),                    inline=True)
    embed.add_field(name="Consecutive Errors", value=str(state["consecutive_errors"]),  inline=True)
    embed.add_field(name="Total Checks",       value=str(state["total_checks"]),        inline=True)
    embed.set_footer(text="Use !status to check bot health • !resume to unpause")
    embed.timestamp = datetime.utcnow()

    try:
        await ch.send(embed=embed)
    except Exception as e:
        log.error(f"Failed to send error alert: {e}")


# ═══════════════════════════════════════════════════════
#  MARKET CHECK
# ═══════════════════════════════════════════════════════

async def check_market_once(session: aiohttp.ClientSession, target: datetime):
    url = build_search_url(target)
    log.info(f"Polling: {url}")

    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RateLimitError(f"Rate limited. Retry after {retry_after}s", retry_after)
        if resp.status >= 500:
            raise ServerError(f"Polymarket server error HTTP {resp.status}")
        if resp.status != 200:
            raise APIError(f"Unexpected HTTP {resp.status}")

        data = await resp.json()

    markets = data if isinstance(data, list) else data.get("markets", [])
    month   = MONTHS[target.month - 1]

    for m in markets:
        q = (m.get("question") or "").lower()
        if "lucknow" in q and month in q and str(target.day) in q:
            log.info(f"Market found: {m.get('question')}")
            return m

    return None


async def check_market_with_retry(target: datetime):
    """Returns (market_or_None, error_string_or_None)"""
    last_error = None

    async with aiohttp.ClientSession() as session:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await check_market_once(session, target)
                return result, None

            except RateLimitError as e:
                last_error = str(e)
                log.warning(f"Rate limited — waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after)

            except (ServerError, APIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed: {e} — retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    log.error(f"All {MAX_RETRIES} attempts failed. Last: {e}")

            except Exception as e:
                last_error = f"{e}\n```{traceback.format_exc()[-400:]}```"
                log.error(f"Unexpected: {e}")
                break

    return None, last_error


# ═══════════════════════════════════════════════════════
#  EMBEDS
# ═══════════════════════════════════════════════════════

def make_found_embed(market: dict, target: datetime) -> discord.Embed:
    url   = build_market_url(market, target)
    month = MONTHS[target.month - 1].capitalize()
    embed = discord.Embed(
        title       = f"🌡️ {month} {target.day} Market is LIVE!",
        description = f"**{market.get('question', 'Lucknow Temperature Market')}**",
        color       = 0x00ff88,
        url         = url
    )
    embed.add_field(name="📈 Trade Now",  value=f"[Open on Polymarket]({url})", inline=False)
    embed.add_field(name="Volume",        value=f"${float(market.get('volume', 0)):,.0f}" if market.get("volume") else "—", inline=True)
    embed.add_field(name="Resolves",      value=target.strftime("%B %d, %Y"), inline=True)
    embed.add_field(name="Detected At",   value=now_ist_str(), inline=True)
    embed.set_footer(text="Polymarket Lucknow Bot")
    embed.timestamp = datetime.utcnow()
    return embed


def make_status_embed() -> discord.Embed:
    target = get_target_date()
    month  = MONTHS[target.month - 1].capitalize()

    if state["paused"]:
        health = "⏸️ Paused"
    elif state["consecutive_errors"] == 0:
        health = "🟢 Healthy"
    else:
        health = f"🟡 {state['consecutive_errors']} consecutive error(s)"

    success_rate = (
        f"{state['successful_checks']}/{state['total_checks']} "
        f"({round(state['successful_checks']/state['total_checks']*100)}%)"
        if state["total_checks"] > 0 else "—"
    )

    embed = discord.Embed(title="🤖 Bot Status", color=0x5865f2)
    embed.add_field(name="Health",           value=health,                                   inline=True)
    embed.add_field(name="Uptime",           value=uptime_str(),                             inline=True)
    embed.add_field(name="Poll Interval",    value=f"{POLL_INTERVAL_SECONDS}s",              inline=True)
    embed.add_field(name="Watching For",     value=f"{month} {target.day}, {target.year}",   inline=True)
    embed.add_field(name="Markets Found",    value=str(state["markets_found"]),               inline=True)
    embed.add_field(name="API Success Rate", value=success_rate,                             inline=True)
    embed.add_field(name="Last Check",       value=state["last_check_time"] or "—",          inline=True)
    embed.add_field(name="Last Result",      value=state["last_check_result"],                inline=True)
    embed.set_footer(text=f"Started: {state['started_at'].strftime('%d %b %Y, %I:%M %p IST') if state['started_at'] else '—'}")
    embed.timestamp = datetime.utcnow()
    return embed


# ═══════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════

async def watch_loop():
    await client.wait_until_ready()
    ch = await get_channel()
    if not ch:
        log.critical("Channel not found. Aborting.")
        return

    state["started_at"] = datetime.now(IST)
    target  = get_target_date()
    month   = MONTHS[target.month - 1].capitalize()
    last_hb = datetime.now(IST)

    await ch.send(embed=discord.Embed(
        title       = "🤖 Polymarket Watcher Online",
        description = f"Watching for **Lucknow {month} {target.day}** temperature market.\n"
                      f"Polling every **{POLL_INTERVAL_SECONDS} seconds**.\n\n"
                      f"Commands: `!status` `!check` `!pause` `!resume` `!help`",
        color       = 0x5865f2
    ))

    while not client.is_closed():
        try:
            # ── Heartbeat ──────────────────────────────
            if (datetime.now(IST) - last_hb).total_seconds() >= HEARTBEAT_HOURS * 3600:
                embed = discord.Embed(
                    title       = "💓 Heartbeat",
                    description = "Bot is alive and polling.",
                    color       = 0x00b4d8
                )
                target_hb = get_target_date()
                embed.add_field(name="Watching",     value=f"{MONTHS[target_hb.month-1].capitalize()} {target_hb.day}", inline=True)
                embed.add_field(name="Uptime",       value=uptime_str(),                inline=True)
                embed.add_field(name="Total Checks", value=str(state["total_checks"]),  inline=True)
                embed.timestamp = datetime.utcnow()
                await ch.send(embed=embed)
                last_hb = datetime.now(IST)

            # ── Paused? ────────────────────────────────
            if state["paused"]:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # ── Poll ───────────────────────────────────
            state["total_checks"]   += 1
            state["last_check_time"] = now_ist_str()
            target                   = get_target_date()

            market, error = await check_market_with_retry(target)

            if error:
                state["consecutive_errors"] += 1
                state["last_check_result"]   = "❌ Error"

                await send_error_alert(
                    title       = f"API Check Failed (#{state['consecutive_errors']})",
                    description = f"Failed to reach Polymarket.\n\n**Error:**\n{error}",
                    critical    = state["consecutive_errors"] >= 5
                )

                # Auto-pause after 10 consecutive failures
                if state["consecutive_errors"] >= 10:
                    state["paused"] = True
                    await send_error_alert(
                        title       = "🛑 Bot Auto-Paused",
                        description = "10 consecutive errors detected.\nBot **paused** to prevent spam.\n\nUse `!resume` to restart.",
                        critical    = True
                    )

            elif market:
                state["consecutive_errors"] = 0
                state["successful_checks"]  += 1
                state["markets_found"]      += 1
                state["last_check_result"]   = "✅ Market Found!"

                month = MONTHS[target.month - 1].capitalize()
                await ch.send(
                    content = f"@here 🚨 **Lucknow {month} {target.day} market just opened!**",
                    embed   = make_found_embed(market, target)
                )
                log.info("Notification sent!")
                # Wait before hunting the next day's market
                await asyncio.sleep(3600)

            else:
                state["consecutive_errors"] = 0
                state["successful_checks"]  += 1
                state["last_check_result"]   = "✅ Not live yet"

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except discord.HTTPException as e:
            log.error(f"Discord error: {e}")
            await asyncio.sleep(30)

        except Exception as e:
            log.critical(f"Unhandled crash: {e}\n{traceback.format_exc()}")
            try:
                await send_error_alert(
                    title       = "💥 Critical Bot Crash",
                    description = f"Unhandled exception:\n```{str(e)[:500]}```\nBot will retry in 60s.",
                    critical    = True
                )
            except Exception:
                pass
            await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id != CHANNEL_ID:
        return

    cmd = message.content.strip().lower()

    if cmd == "!status":
        await message.channel.send(embed=make_status_embed())

    elif cmd == "!check":
        await message.channel.send("🔍 Checking right now...")
        target = get_target_date()
        market, error = await check_market_with_retry(target)
        if error:
            await message.channel.send(embed=discord.Embed(
                title="❌ Check Failed", description=error, color=0xff3b5c
            ))
        elif market:
            await message.channel.send(embed=make_found_embed(market, target))
        else:
            month = MONTHS[target.month - 1].capitalize()
            await message.channel.send(f"❌ **{month} {target.day}** market isn't live yet.")

    elif cmd == "!pause":
        state["paused"] = True
        await message.channel.send("⏸️ Bot paused. Use `!resume` to restart.")

    elif cmd == "!resume":
        state["paused"]             = False
        state["consecutive_errors"] = 0
        await message.channel.send("▶️ Resumed! Polling every 60 seconds.")

    elif cmd == "!help":
        embed = discord.Embed(title="📖 Commands", color=0x5865f2)
        embed.add_field(name="`!status`", value="Bot health & stats",          inline=False)
        embed.add_field(name="`!check`",  value="Manually trigger a check",    inline=False)
        embed.add_field(name="`!pause`",  value="Pause polling",               inline=False)
        embed.add_field(name="`!resume`", value="Resume / clear error streak", inline=False)
        await message.channel.send(embed=embed)


# ═══════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")
    client.loop.create_task(watch_loop())

@client.event
async def on_disconnect():
    log.warning("Disconnected from Discord. Auto-reconnecting...")

@client.event
async def on_resumed():
    log.info("Reconnected to Discord.")


# ═══════════════════════════════════════════════════════
#  ENTRY
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n" + "="*50)
        print("  ❌  Set BOT_TOKEN and CHANNEL_ID in config!")
        print("="*50 + "\n")
    else:
        log.info("Starting Polymarket Lucknow Bot...")
        client.run(BOT_TOKEN, reconnect=True)
