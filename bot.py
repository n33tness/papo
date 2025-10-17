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
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

CURRENCY_EMOJI = "🍉"
CURRENCY_NAME = "golden noodles"

TARGET_USER_ID = 1028310674318839878
AUTHORIZED_GIVER_ID = 1422010902680567918
ADMIN_USER_ID = 939225086341296209

MULTIPLE_OF = 5
JACKPOT = 100
LEADERBOARD_LIMIT_DEFAULT = 10
GIVE_COOLDOWN_SECONDS = 8

BONK_EMOJI = "<:bonk:1427717741481033799>"
BONK_COOLDOWN_SECONDS = 3
BONK_STREAK_STEP = 10

SPOTIFY_REGEX = re.compile(
    r'(https?://(?:open\.spotify\.com|spotify\.link|spoti\.fi)/[^\s>]+)',
    re.IGNORECASE
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool | None = None

# ========= DATABASE SETUP =========
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
  id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  actor_id BIGINT NOT NULL,
  target_id BIGINT NOT NULL,
  delta INTEGER NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_SPOTIFY = """
CREATE TABLE IF NOT EXISTS smuckles_spotify_links (
  id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  url TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (guild_id, user_id, message_id, url)
);
"""

CREATE_REMINDERS = """
CREATE TABLE IF NOT EXISTS smuckles_reminders (
  id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  author_id BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  mentions TEXT,
  note TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_BONK_LOG = """
CREATE TABLE IF NOT EXISTS smuckles_bonk_log (
  id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  bonker_id BIGINT NOT NULL,
  target_id BIGINT NOT NULL,
  channel_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def db_init():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as con:
        await con.execute(CREATE_USERS)
        await con.execute(CREATE_LOG)
        await con.execute(CREATE_SPOTIFY)
        await con.execute(CREATE_REMINDERS)
        await con.execute(CREATE_BONK_LOG)

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

# ========= DATABASE ACTIONS =========
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
        f"• Type `bonk` in chat to bonk {target} (3s cooldown).\n"
        "• Streak memes at 10, 20, 30… bonks per day.\n"
        "• `/bonkstats [member]` — bonks today/week/all-time.\n"
        "• `/bonktop [limit] [window]` — bonk leaderboard (window: all/day/week).\n\n"

        "### 🎵 Spotify Memory\n"
        f"• Auto-saves Spotify links from {target}.\n"
        "• `/papolinks [limit]` — recent Spotify links.\n"
        "• `/paposcan channel:[#channel] [limit]` — (admin) backfill scan.\n\n"

        "### 🗒️ Reminder Bank\n"
        "• Mention the bot + say **remind ...** to save a reminder.\n"
        "• `/myreminders [limit]` — view yours.\n"
        "• `/clearmyreminders` — delete your reminders.\n"
        "• `/remindbank [limit]` — (admin) view recent reminders.\n"
        "• `/clearemindbank` — (admin) wipe all reminders.\n\n"

        "### 🧪 Utility\n"
        "• `/papoping` — latency test.\n"
    )

    await interaction.response.send_message(msg, ephemeral=True)

# ========= RUN =========
if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN env var.")
bot.run(TOKEN)
