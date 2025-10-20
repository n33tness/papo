import os
import re
import random
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ========= CONFIG =========
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://user:pass@host:port/db
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # set in Railway for instant per-guild sync

# 🍜 Currency / theme
CURRENCY_EMOJI = "🍉"               # your watermelon vibe
CURRENCY_NAME  = "golden noodles"   # renamed from "smuckles"

# 🔒 IDs (edit if they change)
TARGET_USER_ID = 1028310674318839878         # the only user who can gain/lose golden noodles + Spotify tracking
AUTHORIZED_GIVER_ID = 1422010902680567918    # primary authorized user for /give and /take
ADMIN_USER_ID = 939225086341296209           # admin override (also allowed to /give, /take, scans, remindbank)

# Golden noodles settings
MULTIPLE_OF = 5          # any positive multiple of 5 is allowed
JACKPOT = 100            # special jackpot amount
LEADERBOARD_LIMIT_DEFAULT = 10
GIVE_COOLDOWN_SECONDS = 8

# Bonk settings
BONK_EMOJI = "<:bonk:1427717741481033799>"
BONK_COOLDOWN_SECONDS = 3  # per-user cooldown
BONK_STREAK_STEP = 10      # trigger memes at 10, 20, 30…
BONK_PENALTY_STEP = 20     # every 20 bonks -> -5 noodles
BONK_PENALTY_AMOUNT = 5

# Spotify detection (text + shorteners)
SPOTIFY_REGEX = re.compile(
    r'(https?://(?:open\.spotify\.com|spotify\.link|spoti\.fi)/[^\s>]+)',
    re.IGNORECASE
)

# ========= DISCORD CLIENT =========
intents = discord.Intents.default()
intents.message_content = True  # Needed for bonk + Spotify capture + backfill + reminder capture
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool | None = None

# ========= DATABASE =========
CREATE_USERS = """
CREATE TABLE IF NOT EXISTS smuckles_users (
  guild_id BIGINT NOT NULL,
  user_id  BIGINT NOT NULL,
  points   BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);
"""

CREATE_LOG = """
CREATE TABLE IF NOT EXISTS smuckles_log (
  id         BIGSERIAL PRIMARY KEY,
  guild_id   BIGINT NOT NULL,
  actor_id   BIGINT NOT NULL,
  target_id  BIGINT NOT NULL,
  delta      INTEGER NOT NULL,
  reason     TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_SPOTIFY = """
CREATE TABLE IF NOT EXISTS smuckles_spotify_links (
  id         BIGSERIAL PRIMARY KEY,
  guild_id   BIGINT NOT NULL,
  user_id    BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  url        TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (guild_id, user_id, message_id, url)
);
"""

CREATE_REMINDERS = """
CREATE TABLE IF NOT EXISTS smuckles_reminders (
  id         BIGSERIAL PRIMARY KEY,
  guild_id   BIGINT NOT NULL,
  author_id  BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  mentions   TEXT,
  note       TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_BONK_LOG = """
CREATE TABLE IF NOT EXISTS smuckles_bonk_log (
  id         BIGSERIAL PRIMARY KEY,
  guild_id   BIGINT NOT NULL,
  bonker_id  BIGINT NOT NULL,
  target_id  BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def db_init():
    global db_pool
    if not DATABASE_URL:
        raise SystemExit("❌ Missing DATABASE_URL environment variable.")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with db_pool.acquire() as con:
        await con.execute(CREATE_USERS)
        await con.execute(CREATE_LOG)
        await con.execute(CREATE_SPOTIFY)
        await con.execute(CREATE_REMINDERS)
        await con.execute(CREATE_BONK_LOG)

# ======== Golden Noodles ========
async def adjust_points(guild_id: int, target_id: int, delta: int):
    async with db_pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "INSERT INTO smuckles_users (guild_id, user_id, points) VALUES ($1,$2,0) ON CONFLICT DO NOTHING",
                guild_id, target_id,
            )
            await con.execute(
                "UPDATE smuckles_users SET points = points + $1 WHERE guild_id=$2 AND user_id=$3",
                delta, guild_id, target_id,
            )

async def get_points(guild_id: int, user_id: int) -> int:
    async with db_pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT points FROM smuckles_users WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        return int(row[0]) if row else 0

async def log_txn(guild_id: int, actor_id: int, target_id: int, delta: int, reason: str | None):
    async with db_pool.acquire() as con:
        await con.execute(
            "INSERT INTO smuckles_log (guild_id, actor_id, target_id, delta, reason) VALUES ($1,$2,$3,$4,$5)",
            guild_id, actor_id, target_id, delta, reason,
        )

# ======== Spotify ========
async def save_spotify_links(guild_id: int, user_id: int, channel_id: int, message_id: int, urls: list[str]):
    if not urls:
        return
    async with db_pool.acquire() as con:
        for u in urls:
            try:
                await con.execute(
                    "INSERT INTO smuckles_spotify_links (guild_id, user_id, channel_id, message_id, url) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                    guild_id, user_id, channel_id, message_id, u
                )
            except Exception:
                pass

# ======== Reminders ========
async def save_reminder(guild_id: int, author_id: int, channel_id: int, message_id: int, mentions_text: str, note: str):
    async with db_pool.acquire() as con:
        await con.execute(
            "INSERT INTO smuckles_reminders (guild_id, author_id, channel_id, message_id, mentions, note) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            guild_id, author_id, channel_id, message_id, mentions_text, note
        )

async def delete_my_reminders(guild_id: int, author_id: int) -> int:
    async with db_pool.acquire() as con:
        return await con.fetchval(
            "WITH del AS (DELETE FROM smuckles_reminders WHERE guild_id=$1 AND author_id=$2 RETURNING 1) SELECT COUNT(*) FROM del",
            guild_id, author_id
        )

async def clear_remind_bank(guild_id: int) -> int:
    async with db_pool.acquire() as con:
        return await con.fetchval(
            "WITH del AS (DELETE FROM smuckles_reminders WHERE guild_id=$1 RETURNING 1) SELECT COUNT(*) FROM del",
            guild_id
        )

# ======== Bonks ========
async def log_bonk(guild_id: int, bonker_id: int, channel_id: int, message_id: int):
    async with db_pool.acquire() as con:
        await con.execute(
            "INSERT INTO smuckles_bonk_log (guild_id, bonker_id, target_id, channel_id, message_id) "
            "VALUES ($1,$2,$3,$4,$5)",
            guild_id, bonker_id, TARGET_USER_ID, channel_id, message_id
        )

async def today_bonk_count(guild_id: int) -> int:
    async with db_pool.acquire() as con:
        return await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_bonk_log "
            "WHERE guild_id=$1 AND target_id=$2 AND created_at::date = CURRENT_DATE",
            guild_id, TARGET_USER_ID
        )

async def bonk_counts_for_user(guild_id: int, bonker_id: int) -> tuple[int, int, int]:
    """Return (today, week, all_time) for a given bonker vs the TARGET."""
    async with db_pool.acquire() as con:
        today = await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_bonk_log "
            "WHERE guild_id=$1 AND target_id=$2 AND bonker_id=$3 AND created_at::date = CURRENT_DATE",
            guild_id, TARGET_USER_ID, bonker_id
        )
        week = await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_bonk_log "
            "WHERE guild_id=$1 AND target_id=$2 AND bonker_id=$3 AND created_at >= (NOW() - INTERVAL '7 days')",
            guild_id, TARGET_USER_ID, bonker_id
        )
        all_time = await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_bonk_log "
            "WHERE guild_id=$1 AND target_id=$2 AND bonker_id=$3",
            guild_id, TARGET_USER_ID, bonker_id
        )
    return int(today or 0), int(week or 0), int(all_time or 0)

async def bonk_leaderboard(guild_id: int, window: str, limit: int):
    """Return list[(bonker_id, count)] ordered desc for 'all'|'day'|'week'."""
    where = "guild_id=$1 AND target_id=$2"
    if window == "day":
        where += " AND created_at::date = CURRENT_DATE"
    elif window == "week":
        where += " AND created_at >= (NOW() - INTERVAL '7 days')"
    sql = f"""
        SELECT bonker_id, COUNT(*) AS c
        FROM smuckles_bonk_log
        WHERE {where}
        GROUP BY bonker_id
        ORDER BY c DESC
        LIMIT $3
    """
    async with db_pool.acquire() as con:
        rows = await con.fetch(sql, guild_id, TARGET_USER_ID, limit)
    return [(int(r["bonker_id"]), int(r["c"])) for r in rows]

async def remove_bonks_for_user(guild_id: int, bonker_id: int, window: str, count: int) -> int:
    """
    Delete the most recent N bonks by bonker_id against TARGET within window.
    window: 'all' | 'day' | 'week'
    Returns number of rows deleted.
    """
    if count <= 0:
        return 0
    where = "guild_id=$1 AND target_id=$2 AND bonker_id=$3"
    if window == "day":
        where += " AND created_at::date = CURRENT_DATE"
    elif window == "week":
        where += " AND created_at >= (NOW() - INTERVAL '7 days')"

    # Use a CTE to select newest N ids, then delete them and count how many were removed
    sql = f"""
    WITH to_del AS (
        SELECT id FROM smuckles_bonk_log
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT $4
    ),
    del AS (
        DELETE FROM smuckles_bonk_log
        WHERE id IN (SELECT id FROM to_del)
        RETURNING 1
    )
    SELECT COUNT(*) FROM del;
    """
    async with db_pool.acquire() as con:
        n = await con.fetchval(sql, guild_id, TARGET_USER_ID, bonker_id, count)
    return int(n or 0)

# ========= HELPERS =========
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

def is_authorized_actor(user_id: int) -> bool:
    return user_id in (AUTHORIZED_GIVER_ID, ADMIN_USER_ID)

def is_valid_multiple(amount: int) -> bool:
    return amount > 0 and amount % MULTIPLE_OF == 0

last_give_ts: dict[int, float] = {}
def on_cooldown(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    last = last_give_ts.get(user_id, 0)
    if now - last < GIVE_COOLDOWN_SECONDS:
        return True
    last_give_ts[user_id] = now
    return False

last_bonk_ts: dict[int, float] = {}
def bonk_on_cooldown(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    last = last_bonk_ts.get(user_id, 0)
    if now - last < BONK_COOLDOWN_SECONDS:
        return True
    last_bonk_ts[user_id] = now
    return False

def extract_spotify_from_message(msg: discord.Message) -> list[str]:
    """Find spotify links in message content AND embeds."""
    urls = []
    urls += SPOTIFY_REGEX.findall(msg.content or "")
    for e in msg.embeds:
        if e.url:
            urls += SPOTIFY_REGEX.findall(e.url)
        if e.description:
            urls += SPOTIFY_REGEX.findall(e.description)
        if e.title:
            urls += SPOTIFY_REGEX.findall(e.title)
        for f in (e.fields or []):
            if f.name:
                urls += SPOTIFY_REGEX.findall(f.name)
            if f.value:
                urls += SPOTIFY_REGEX.findall(f.value)
    # dedupe preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def extract_reminder_note(raw_content: str) -> str | None:
    if not raw_content:
        return None
    low = raw_content.lower()
    idx = low.find("remind")
    if idx == -1:
        return None
    note = raw_content[idx + len("remind"):].strip()
    note = re.sub(r"^(me|us|to|@?\w+)?\s*", "", note, flags=re.IGNORECASE)
    return note if note else None

# ========= READY =========
@bot.event
async def on_ready():
    await db_init()
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced guild commands to {GUILD_ID} as {bot.user}")
    else:
        await bot.tree.sync()
        print(f"✅ Synced global commands as {bot.user}")

# ========= /papoping =========
@bot.tree.command(name="papoping", description="Check if the bot is alive and running")
async def papoping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"{CURRENCY_EMOJI} Papo is online! Ping: `{latency}ms`")

# ========= /papohelp =========
@bot.tree.command(name="papohelp", description="Show everything Papo can do")
async def papohelp(interaction: discord.Interaction):
    target = f"<@{TARGET_USER_ID}>"
    giver  = f"<@{AUTHORIZED_GIVER_ID}>"
    admin  = f"<@{ADMIN_USER_ID}>"

    msg = (
        "## 🤖 Papo Command Guide\n"
        f"**Target:** {target}\n"
        f"**Giver:** {giver}\n"
        f"**Admin:** {admin}\n\n"

        "### 🍜 Golden Noodles\n"
        f"• `/give @member amount reason` — (giver/admin) multiples of 5, or 100 for JACKPOT. Only {target} can receive.\n"
        f"• `/take @member amount reason` — (giver/admin) multiples of 5, or 100. Only affects {target}.\n"
        "• `/sandia [limit]` — leaderboard of golden noodles.\n\n"

        "### 🔨 Bonks\n"
        f"• Type `bonk` in chat to bonk {target} (3s personal cooldown).\n"
        "• Streak memes at 10, 20, 30… bonks per day.\n"
        "• `/bonkstats [member]` — bonks today/week/all-time.\n"
        "• `/bonktop [limit] [window]` — bonk leaderboard (window: all/day/week).\n"
        "• `/bonkremove member:<user> count:<n> window:<all|day|week>` — (admin) remove recent bonks from a user.\n\n"

        "### 🎵 Spotify Memory\n"
        f"• Auto-saves Spotify links from {target}.\n"
        "• `/papolinks [limit]` — recent Spotify links.\n"
        "• `/paposcan channel:[#channel] [limit]` — (admin) backfill scan.\n\n"

        "### 🗒️ Reminder Bank\n"
        "• Mention the bot + say **remind ...** to save a reminder.\n"
        "• `/myreminders [limit]` — your reminders.\n"
        "• `/clearmyreminders` — delete your reminders.\n"
        "• `/remindbank [limit]` — (admin) view recent reminders.\n"
        "• `/clearemindbank` — (admin) wipe all reminders.\n\n"

        "### 🧪 Utility\n"
        "• `/papoping` — latency test.\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ========= /give =========
@bot.tree.command(description=f"Give {CURRENCY_NAME} to the designated member (multiples of {MULTIPLE_OF}, jackpot {JACKPOT})")
@app_commands.describe(member="Must be the designated member", amount=f"Multiple of {MULTIPLE_OF} (e.g., 5,10,15,...) or {JACKPOT}", reason="Optional reason")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if not is_authorized_actor(interaction.user.id):
        return await interaction.response.send_message(f"Only authorized users can give {CURRENCY_NAME}.", ephemeral=True)
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(f"Only <@{TARGET_USER_ID}> can receive {CURRENCY_EMOJI} {CURRENCY_NAME}.", ephemeral=True)
    if not (is_valid_multiple(amount) or amount == JACKPOT):
        return await interaction.response.send_message(
            f"Amount must be a positive multiple of {MULTIPLE_OF} (e.g., 5,10,15,...) or exactly {JACKPOT}.",
            ephemeral=True
        )
    if on_cooldown(interaction.user.id) and not is_admin(interaction.user.id):
        return await interaction.response.send_message("Slow down — try again in a few seconds.", ephemeral=True)

    await adjust_points(interaction.guild_id, member.id, amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    if amount == JACKPOT:
        text = f"🎰 JACKPOT! {member.mention} hit **{amount} {CURRENCY_EMOJI}** {CURRENCY_NAME}! Total: **{total} {CURRENCY_EMOJI}**."
    else:
        text = f"✅ {member.mention} received **{amount} {CURRENCY_EMOJI} {CURRENCY_NAME}**. New total: **{total} {CURRENCY_EMOJI}**."
        if reason:
            text += f" (_{reason}_)"
    await interaction.response.send_message(text)

# ========= /take =========
@bot.tree.command(description=f"Take {CURRENCY_NAME} from the designated member (multiples of {MULTIPLE_OF}, or {JACKPOT})")
@app_commands.describe(member="Must be the designated member", amount=f"Multiple of {MULTIPLE_OF} (e.g., 5,10,15,...) or {JACKPOT}", reason="Optional reason")
async def take(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if not is_authorized_actor(interaction.user.id):
        return await interaction.response.send_message(f"Only authorized users can take {CURRENCY_NAME}.", ephemeral=True)
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(f"Only <@{TARGET_USER_ID}> can have {CURRENCY_NAME} taken away.", ephemeral=True)
    if not (is_valid_multiple(amount) or amount == JACKPOT):
        return await interaction.response.send_message(
            f"Amount must be a positive multiple of {MULTIPLE_OF} (e.g., 5,10,15,...) or exactly {JACKPOT}.",
            ephemeral=True
        )

    await adjust_points(interaction.guild_id, member.id, -amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, -amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    text = f"⚠️ {member.mention} lost **{amount} {CURRENCY_EMOJI}**. New total: **{total} {CURRENCY_EMOJI}**."
    if reason:
        text += f" (_{reason}_)"
    await interaction.response.send_message(text)

# ========= /sandia (leaderboard) =========
@bot.tree.command(name="sandia", description=f"Top members by {CURRENCY_NAME} ({CURRENCY_EMOJI} leaderboard)")
@app_commands.describe(limit="How many to show (default 10, max 30)")
async def sandia(interaction: discord.Interaction, limit: int = LEADERBOARD_LIMIT_DEFAULT):
    limit = max(1, min(30, limit))
    async with db_pool.acquire() as con:
        rows = await con.fetch(
            "SELECT user_id, points FROM smuckles_users WHERE guild_id=$1 ORDER BY points DESC LIMIT $2",
            interaction.guild_id, limit,
        )
    if not rows:
        return await interaction.response.send_message(f"No {CURRENCY_NAME} yet. Be the first to give some!")

    lines = []
    for i, r in enumerate(rows, start=1):
        uid = int(r[0]); pts = int(r[1])
        try:
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            name = member.display_name if member else f"<@{uid}>"
        except Exception:
            name = f"<@{uid}>"
        lines.append(f"{i}. **{name}** — {pts} {CURRENCY_EMOJI}")
    await interaction.response.send_message("🏆 **Sandia Leaderboard**\n" + "\n".join(lines))

# ========= /papolinks (Spotify link history) =========
@bot.tree.command(name="papolinks", description="Recent Spotify links posted by the target user")
@app_commands.describe(limit="How many to show (default 10, max 50)")
async def papolinks(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(50, limit))
    async with db_pool.acquire() as con:
        rows = await con.fetch(
            "SELECT url, created_at FROM smuckles_spotify_links "
            "WHERE guild_id=$1 AND user_id=$2 "
            "ORDER BY created_at DESC LIMIT $3",
            interaction.guild_id, TARGET_USER_ID, limit
        )
        total_count = await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_spotify_links WHERE guild_id=$1 AND user_id=$2",
            interaction.guild_id, TARGET_USER_ID
        )

    if not rows:
        return await interaction.response.send_message("No Spotify links found yet.", ephemeral=False)

    lines = [f"🎵 **Spotify history for <@{TARGET_USER_ID}>** (showing {len(rows)}/{total_count}):"]
    for r in rows:
        url = r["url"]
        lines.append(f"• {url}")
    msg = "\n".join(lines)
    if len(msg) > 1800 and len(rows) > 20:
        lines = lines[:21]
        lines.append("… (trimmed)")
        msg = "\n".join(lines)
    await interaction.response.send_message(msg)

# ========= /paposcan (admin-only backfill of Spotify links) =========
@bot.tree.command(name="paposcan", description="Admin: scan a channel's history to backfill the target's Spotify links")
@app_commands.describe(
    channel="Text channel to scan",
    limit="Max messages to scan (50–5000, default 1000, newest first)"
)
async def paposcan(interaction: discord.Interaction, channel: discord.TextChannel, limit: int = 1000):
    if interaction.user.id != ADMIN_USER_ID:
        return await interaction.response.send_message("Only the bot admin can run this.", ephemeral=True)

    limit = max(50, min(5000, limit))
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.read_messages and perms.read_message_history):
        return await interaction.response.send_message("I need **Read Messages** and **Read Message History** in that channel.", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=True)

    scanned = 0
    matched_msgs = 0
    saved_urls = 0

    try:
        async for msg in channel.history(limit=limit, oldest_first=False):
            scanned += 1
            if msg.author and msg.author.id == TARGET_USER_ID:
                urls = extract_spotify_from_message(msg)
                if urls:
                    matched_msgs += 1
                    await save_spotify_links(
                        guild_id=interaction.guild_id,
                        user_id=TARGET_USER_ID,
                        channel_id=channel.id,
                        message_id=msg.id,
                        urls=urls
                    )
                    saved_urls += len(urls)

            if scanned % 200 == 0:
                await asyncio.sleep(0.3)

    except discord.Forbidden:
        return await interaction.followup.send("I don't have permission to read that channel’s history.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"HTTP error while scanning: {e}", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Unexpected error: {e}", ephemeral=True)

    await interaction.followup.send(
        f"✅ Scan complete for {channel.mention}\n"
        f"- Messages scanned: **{scanned}** (newest → oldest)\n"
        f"- Messages with Spotify links from <@{TARGET_USER_ID}>: **{matched_msgs}**\n"
        f"- URLs saved (deduped): **{saved_urls}**",
        ephemeral=True
    )

# ========= Reminder bank commands =========
@bot.tree.command(name="remindbank", description="Admin: list recent stored reminder requests")
@app_commands.describe(limit="How many to show (default 10, max 50)")
async def remindbank(interaction: discord.Interaction, limit: int = 10):
    if interaction.user.id != ADMIN_USER_ID:
        return await interaction.response.send_message("Only the bot admin can view the full reminder bank.", ephemeral=True)
    limit = max(1, min(50, limit))
    async with db_pool.acquire() as con:
        rows = await con.fetch(
            "SELECT author_id, mentions, note, created_at FROM smuckles_reminders "
            "WHERE guild_id=$1 ORDER BY created_at DESC LIMIT $2",
            interaction.guild_id, limit
        )
        total = await con.fetchval(
            "SELECT COUNT(*) FROM smuckles_reminders WHERE guild_id=$1",
            interaction.guild_id
        )
    if not rows:
        return await interaction.response.send_message("No reminders saved yet.", ephemeral=True)
    lines = [f"📝 **Reminder Bank** (showing {len(rows)}/{total}):"]
    for r in rows:
        who = f"<@{int(r['author_id'])}>"
        mentions = f" → to: {r['mentions']}" if r['mentions'] else ""
        lines.append(f"• {who}{mentions}: {r['note']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="myreminders", description="See your own stored reminder requests")
@app_commands.describe(limit="How many to show (default 10, max 50)")
async def myreminders(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(50, limit))
    async with db_pool.acquire() as con:
        rows = await con.fetch(
            "SELECT mentions, note, created_at FROM smuckles_reminders "
            "WHERE guild_id=$1 AND author_id=$2 ORDER BY created_at DESC LIMIT $3",
            interaction.guild_id, interaction.user.id, limit
        )
    if not rows:
        return await interaction.response.send_message("You have no saved reminders yet.", ephemeral=True)
    lines = [f"🗒️ **Your reminders** (last {len(rows)}):"]
    for r in rows:
        mentions = f" → to: {r['mentions']}" if r['mentions'] else ""
        lines.append(f"• {mentions} {r['note']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="clearmyreminders", description="Delete all of YOUR saved reminders")
async def clearmyreminders(interaction: discord.Interaction):
    deleted = await delete_my_reminders(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(f"🧹 Deleted **{deleted}** of your reminders.", ephemeral=True)

@bot.tree.command(name="clearemindbank", description="Admin: wipe the entire reminder bank for this server")
async def clearemindbank(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_USER_ID:
        return await interaction.response.send_message("Only the bot admin can wipe the reminder bank.", ephemeral=True)
    deleted = await clear_remind_bank(interaction.guild_id)
    await interaction.response.send_message(f"🧨 Cleared the reminder bank. Removed **{deleted}** entries.", ephemeral=True)

# ========= Bonk stats / leaderboard =========
@bot.tree.command(name="bonkstats", description="Show how many times someone has bonked the target (today/week/all-time)")
@app_commands.describe(member="Member to check (defaults to you)")
async def bonkstats(interaction: discord.Interaction, member: discord.Member | None = None):
    target_member = member or interaction.user
    today, week, all_time = await bonk_counts_for_user(interaction.guild_id, target_member.id)
    await interaction.response.send_message(
        f"🔨 **Bonk stats for {target_member.mention} → <@{TARGET_USER_ID}>**\n"
        f"• Today: **{today}**\n"
        f"• Last 7 days: **{week}**\n"
        f"• All-time: **{all_time}**"
    )

@bot.tree.command(name="bonktop", description="Leaderboard of who bonks the target the most")
@app_commands.describe(limit="How many to show (1–30, default 10)", window="all | day | week")
async def bonktop(interaction: discord.Interaction, limit: int = 10, window: str = "all"):
    limit = max(1, min(30, limit))
    window = window.lower().strip()
    if window not in ("all", "day", "week"):
        return await interaction.response.send_message("`window` must be one of: **all**, **day**, **week**.", ephemeral=True)
    rows = await bonk_leaderboard(interaction.guild_id, window, limit)
    if not rows:
        return await interaction.response.send_message("No bonks logged yet.")
    title = {"all": "All-Time", "day": "Today", "week": "This Week"}[window]
    lines = [f"🏅 **Bonk Top — {title}** (→ <@{TARGET_USER_ID}>)"]
    for i, (uid, c) in enumerate(rows, start=1):
        lines.append(f"{i}. <@{uid}> — **{c}**")
    await interaction.response.send_message("\n".join(lines))

# ========= Admin: remove bonks for a user =========
@bot.tree.command(name="bonkremove", description="Admin: remove recent bonks a member made against the target")
@app_commands.describe(member="Member whose bonks to remove", count="How many recent bonks to remove", window="all | day | week")
async def bonkremove(interaction: discord.Interaction, member: discord.Member, count: int, window: str = "all"):
    if interaction.user.id != ADMIN_USER_ID:
        return await interaction.response.send_message("Only the bot admin can remove bonks.", ephemeral=True)
    window = window.lower().strip()
    if window not in ("all", "day", "week"):
        return await interaction.response.send_message("`window` must be one of: **all**, **day**, **week**.", ephemeral=True)
    if count <= 0 or count > 1000:
        return await interaction.response.send_message("`count` must be between 1 and 1000.", ephemeral=True)

    removed = await remove_bonks_for_user(interaction.guild_id, member.id, window, count)
    if removed == 0:
        return await interaction.response.send_message(f"No matching bonks found to remove for {member.mention} in `{window}` window.", ephemeral=True)
    await interaction.response.send_message(
        f"🧽 Removed **{removed}** bonk{'s' if removed != 1 else ''} made by {member.mention} against <@{TARGET_USER_ID}> in `{window}` window.",
        ephemeral=True
    )

# ========= BONK + SPOTIFY LIVE + REMINDER CAPTURE =========
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content or ""
    low = content.lower()

    # --- BONK for everyone (server channels only) ---
    if message.guild and "bonk" in low:
        if not bonk_on_cooldown(message.author.id):
            target_mention = f"<@{TARGET_USER_ID}>"
            try:
                await message.channel.send(f"{BONK_EMOJI} BONK! {target_mention} got bonked!")
            except Exception:
                pass

            # Log bonk + check streaks and penalties
            try:
                await log_bonk(
                    guild_id=message.guild.id,
                    bonker_id=message.author.id,
                    channel_id=message.channel.id,
                    message_id=message.id
                )
                count_today = await today_bonk_count(message.guild.id)

                # 🎭 meme milestones at 10, 20, 30...
                if count_today % BONK_STREAK_STEP == 0:
                    memes = [
                        "💀 Papo has been bonked into another dimension.",
                        "🧠 Memory corrupted by excessive bonks.",
                        "🚑 Paramedics called. Bonk overdose detected.",
                        "<a:endisnear:1416071184629497856> Papo's demise is inevitable.",
                        "🔨 World record bonk streak achieved!",
                    ]
                    await message.channel.send(random.choice(memes))

                # 💸 every 20 bonks deduct 5 noodles (and log it)
                if count_today % BONK_PENALTY_STEP == 0:
                    await adjust_points(message.guild.id, TARGET_USER_ID, -BONK_PENALTY_AMOUNT)
                    actor_id = bot.user.id if bot.user else ADMIN_USER_ID
                    await log_txn(
                        guild_id=message.guild.id,
                        actor_id=actor_id,
                        target_id=TARGET_USER_ID,
                        delta=-BONK_PENALTY_AMOUNT,
                        reason=f"{BONK_PENALTY_STEP}-bonk penalty"
                    )
                    total = await get_points(message.guild.id, TARGET_USER_ID)
                    await message.channel.send(
                        f"💀 {target_mention} has been bonked **{count_today}** times today and loses "
                        f"**{BONK_PENALTY_AMOUNT} {CURRENCY_EMOJI} {CURRENCY_NAME}**!\n"
                        f"New total: **{total} {CURRENCY_EMOJI}**."
                    )

            except Exception as e:
                # Keep bot resilient; just log to console
                print("Bonk handling error:", e)

    # --- SPOTIFY (live capture) — captures content + embeds from the target user
    if message.author.id == TARGET_USER_ID:
        urls = []
        urls += SPOTIFY_REGEX.findall(message.content or "")
        for e in message.embeds:
            if e.url: urls += SPOTIFY_REGEX.findall(e.url)
            if e.description: urls += SPOTIFY_REGEX.findall(e.description)
            if e.title: urls += SPOTIFY_REGEX.findall(e.title)
            for f in (e.fields or []):
                if f.name: urls += SPOTIFY_REGEX.findall(f.name)
                if f.value: urls += SPOTIFY_REGEX.findall(f.value)
        # dedupe
        if urls:
            seen, unique = set(), []
            for u in urls:
                if u not in seen:
                    seen.add(u); unique.append(u)
            try:
                await save_spotify_links(
                    guild_id=message.guild.id if message.guild else 0,
                    user_id=message.author.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    urls=unique
                )
            except Exception:
                pass

    # --- REMINDER BANK: if the bot is mentioned + the word "remind" appears
    try:
        bot_mentioned = any(user.id == bot.user.id for user in message.mentions) if bot.user else False
    except Exception:
        bot_mentioned = False

    if message.guild and bot_mentioned and "remind" in low:
        note = extract_reminder_note(content)
        if note:
            mentions_ids = [str(u.id) for u in message.mentions if bot.user and u.id != bot.user.id]
            mentions_text = ", ".join(f"<@{uid}>" for uid in mentions_ids) if mentions_ids else ""
            try:
                await save_reminder(
                    guild_id=message.guild.id,
                    author_id=message.author.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    mentions_text=mentions_text,
                    note=note[:500]
                )
                await message.channel.send(f"✅ Saved reminder to bank: _{note[:120]}{'…' if len(note)>120 else ''}_")
            except Exception:
                pass

    await bot.process_commands(message)

# ========= RUN =========
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_TOKEN environment variable.")
bot.run(TOKEN)
