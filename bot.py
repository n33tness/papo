import os
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ========= CONFIG =========
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://user:pass@host:port/db
MOD_ROLE_IDS = [int(x) for x in os.getenv("MOD_ROLE_IDS", "").split(",") if x.strip().isdigit()]
GIVE_COOLDOWN_SECONDS = int(os.getenv("GIVE_COOLDOWN_SECONDS", "8"))
MAX_DELTA_PER_TXN = int(os.getenv("MAX_DELTA_PER_TXN", "250"))
LEADERBOARD_LIMIT_DEFAULT = int(os.getenv("LEADERBOARD_LIMIT_DEFAULT", "10"))

# 🍉 Currency symbol
CURRENCY = "🍉"

# 🔒 Target user restrictions
TARGET_USER_ID = 1028310674318839878  # Only this user can gain/lose smuckles

# ========= DISCORD CLIENT =========
intents = discord.Intents.default()
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

def has_mod_role(member: discord.Member) -> bool:
    if not MOD_ROLE_IDS:
        return True  # everyone allowed if not set
    member_role_ids = {r.id for r in getattr(member, "roles", [])}
    return any(rid in member_role_ids for rid in MOD_ROLE_IDS)

# ========= COOLDOWN =========
last_give_ts: dict[int, float] = {}

def on_cooldown(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    last = last_give_ts.get(user_id, 0)
    if now - last < GIVE_COOLDOWN_SECONDS:
        return True
    last_give_ts[user_id] = now
    return False

# ========= READY EVENT =========
@bot.event
async def on_ready():
    await db_init()
    await bot.tree.sync()
    print(f"✅ Bot connected and synced as {bot.user}")

# ========= TEST COMMAND =========
@bot.tree.command(name="ping", description="Check if the bot is online and responsive")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)  # ms
    await interaction.response.send_message(f"🏓 Pong! Latency: {latency}ms")

# ========= GIVE COMMAND =========
@bot.tree.command(description="Give smuckles to the designated member")
@app_commands.describe(member="Must be the designated member", amount="How many (positive integer)", reason="Optional reason")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(
            f"Only <@{TARGET_USER_ID}> can receive {CURRENCY} smuckles.", ephemeral=True)
    if interaction.user.id == TARGET_USER_ID:
        return await interaction.response.send_message("You cannot modify smuckles.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
    if amount > MAX_DELTA_PER_TXN:
        return await interaction.response.send_message(f"Max per transaction is {MAX_DELTA_PER_TXN}.", ephemeral=True)
    if on_cooldown(interaction.user.id):
        return await interaction.response.send_message("Slow down — try again in a few seconds.", ephemeral=True)

    await adjust_points(interaction.guild_id, member.id, amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    text = f"✅ {member.mention} received **{amount} {CURRENCY} smuckles**"
    if reason:
        text += f" for _{reason}_"
    text += f". New total: **{total} {CURRENCY}**."
    await interaction.response.send_message(text)

# ========= TAKE COMMAND =========
@bot.tree.command(description="Take smuckles from the designated member")
@app_commands.describe(member="Must be the designated member", amount="How many (positive integer)", reason="Optional reason")
async def take(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    if not has_mod_role(interaction.user):
        return await interaction.response.send_message("Only moderators can use /take.", ephemeral=True)
    if member.id != TARGET_USER_ID:
        return await interaction.response.send_message(
            f"Only <@{TARGET_USER_ID}> can have {CURRENCY} smuckles taken away.", ephemeral=True)
    if interaction.user.id == TARGET_USER_ID:
        return await interaction.response.send_message("You cannot modify smuckles.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
    if amount > MAX_DELTA_PER_TXN:
        return await interaction.response.send_message(f"Max per transaction is {MAX_DELTA_PER_TXN}.", ephemeral=True)

    await adjust_points(interaction.guild_id, member.id, -amount)
    await log_txn(interaction.guild_id, interaction.user.id, member.id, -amount, reason)
    total = await get_points(interaction.guild_id, member.id)

    text = f"⚠️ {member.mention} lost **{amount} {CURRENCY}**"
    if reason:
        text += f" for _{reason}_"
    text += f". New total: **{total} {CURRENCY}**."
    await interaction.response.send_message(text)

# ========= SMUCKLES COMMAND =========
@bot.tree.command(name="smuckles", description="Check your (or another member’s) smuckles")
@app_commands.describe(member="Whose balance to check (optional)")
async def smuckles(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    total = await get_points(interaction.guild_id, target.id)
    await interaction.response.send_message(f"🎯 {target.mention} has **{total} {CURRENCY}** smuckles.")

# ========= LEADERBOARD =========
@bot.tree.command(description="Top members by smuckles")
@app_commands.describe(limit="How many to show (default 10, max 30)")
async def leaderboard(interaction: discord.Interaction, limit: int = LEADERBOARD_LIMIT_DEFAULT):
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

    await interaction.response.send_message("🏆 **Smuckles Leaderboard**\n" + "\n".join(lines))

# ========= RUN =========
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_TOKEN environment variable.")
bot.run(TOKEN)
