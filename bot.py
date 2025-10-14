import os
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ========= CONFIG =========
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://user:pass@host:port/db
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # set in Railway for instant per-guild sync

# 🍉 Currency symbol
CURRENCY = "🍉"

# 🔒 IDs (edit if they change)
TARGET_USER_ID = 1028310674318839878         # only this user can gain/lose smuckles
AUTHORIZED_GIVER_ID = 1422010902680567918    # primary authorized user for /give and /take
ADMIN_USER_ID = 939225086341296209           # admin override (also allowed to /give, /take, and bonk)

# Smuckles settings
VALID_AMOUNTS = (5, 10, 50)                  # allowed increments; 50 is jackpot
LEADERBOARD_LIMIT_DEFAULT = 10
GIVE_COOLDOWN_SECONDS = 8

# Bonk settings
BONK_EMOJI = "<:bonk:1427717741481033799>"
BONK_COOLDOWN_SECONDS = 3

# ========= DISCORD CLIENT =========
intents = discord.Intents.default()
# Needed for the bonk trigger (reading message content)
intents.message_content = True
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

async def db_init():
    global db_pool
    if not DATABASE_URL:
        raise SystemExit("❌ Missing DATABASE_URL environment variable.")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with db_pool.acquire() as con:
        await con.execute(CREATE_USERS)
        await con.execute(CREATE_LOG)

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

# ========= HELPERS =========
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

def is_authorized_actor(user_id: int) -> bool:
    return user_id in (AUTHORIZED_GIVER_ID, ADMIN_USER_ID)

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

# ========= READY =========
@bot.event
async def on_ready():
    await db_init()
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)   # instant registration in your server
        print(f"✅ Synced guild commands to {GUILD_ID} as {bot.user}")
    else:
        await bot.tree.sync()              # global (can be slower to appear)
        print(f"✅ Synced global commands as {bot.user}")

# ========= /papoping =========
@bot.tree.command(name="papoping", description="Check if the bot is alive and running")
async def papoping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🍉 Papo is online! Ping: `{latency}ms`")

# ========= /give =========
@bot.tree.command(description="Give smuckles to the designated member (5, 10, or 50)")
@app_commands.describe(member="Must be the designated member", amount="5, 10, or 50", reason="Optional reason")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if not is_authorized_actor(interaction.user.id):
        return await interaction.response.send_message("Only authorized users can give smuckles.", ephemeral=True)
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(f"Only <@{TARGET_USER_ID}> can receive {CURRENCY} smuckles.", ephemeral=True)
    if amount not in VALID_AMOUNTS:
        return await interaction.response.send_message("Amount must be **5**, **10**, or **50**.", ephemeral=True)
    if on_cooldown(interaction.user.id) and not is_admin(interaction.user.id):
        return await interaction.response.send_message("Slow down — try again in a few seconds.", ephemeral=True)

    await adjust_points(interaction.guild_id, member.id, amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    if amount == 50:
        text = f"🎰 JACKPOT! {member.mention} hit **{amount} {CURRENCY}**! Total: **{total} {CURRENCY}**."
    else:
        text = f"✅ {member.mention} received **{amount} {CURRENCY} smuckles**. New total: **{total} {CURRENCY}**."
        if reason:
            text += f" (_{reason}_)"
    await interaction.response.send_message(text)

# ========= /take =========
@bot.tree.command(description="Take smuckles from the designated member (5, 10, or 50)")
@app_commands.describe(member="Must be the designated member", amount="5, 10, or 50", reason="Optional reason")
async def take(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if not is_authorized_actor(interaction.user.id):
        return await interaction.response.send_message("Only authorized users can take smuckles.", ephemeral=True)
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(f"Only <@{TARGET_USER_ID}> can have smuckles taken away.", ephemeral=True)
    if amount not in VALID_AMOUNTS:
        return await interaction.response.send_message("Amount must be **5**, **10**, or **50**.", ephemeral=True)

    await adjust_points(interaction.guild_id, member.id, -amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, -amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    text = f"⚠️ {member.mention} lost **{amount} {CURRENCY}**. New total: **{total} {CURRENCY}**."
    if reason:
        text += f" (_{reason}_)"
    await interaction.response.send_message(text)

# ========= /sandia (leaderboard) =========
@bot.tree.command(name="sandia", description="Top members by smuckles (🍉 leaderboard)")
@app_commands.describe(limit="How many to show (default 10, max 30)")
async def sandia(interaction: discord.Interaction, limit: int = LEADERBOARD_LIMIT_DEFAULT):
    limit = max(1, min(30, limit))
    async with db_pool.acquire() as con:
        rows = await con.fetch(
            "SELECT user_id, points FROM smuckles_users WHERE guild_id=$1 ORDER BY points DESC LIMIT $2",
            interaction.guild_id, limit,
        )
    if not rows:
        return await interaction.response.send_message("No smuckles yet. Be the first to give some!")

    lines = []
    for i, r in enumerate(rows, start=1):
        uid = int(r[0]); pts = int(r[1])
        try:
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            name = member.display_name if member else f"<@{uid}>"
        except Exception:
            name = f"<@{uid}>"
        lines.append(f"{i}. **{name}** — {pts} {CURRENCY}")

    await interaction.response.send_message("🏆 **Sandia Leaderboard**\n" + "\n".join(lines))

# ========= BONK EMOTE TRIGGER =========
@bot.event
async def on_message(message: discord.Message):
    # ignore bot messages
    if message.author.bot:
        return

    content = message.content.lower()
    # allow both the main giver and the admin to bonk
    if message.author.id in (AUTHORIZED_GIVER_ID, ADMIN_USER_ID) and "bonk" in content:
        if not bonk_on_cooldown(message.author.id):
            target_mention = f"<@{TARGET_USER_ID}>"
            try:
                await message.channel.send(f"{BONK_EMOJI} BONK! {target_mention} got bonked!")
            except Exception:
                pass  # keep bot resilient

    # keep slash commands working alongside on_message
    await bot.process_commands(message)

# ========= RUN =========
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_TOKEN environment variable.")
bot.run(TOKEN)
