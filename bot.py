from pathlib import Path
import textwrap, zipfile, os, json, re

base = Path("/mnt/data/discord_full_bot")
base.mkdir(parents=True, exist_ok=True)

bot_code = r'''
import os
import random
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Lipsește variabila DISCORD_TOKEN.")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_PATH = "bot.db"

# giveaway_message_id -> date
giveaways: dict[int, dict] = {}
last_giveaway_winner: dict[int, int] = {}

SHOP_ITEMS = {
    "telefon": 500,
    "laptop": 1500,
    "masina": 5000,
    "vip": 10000,
}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT,
                last_work TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, item)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS reputation (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS verification (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
        """)


def ensure_economy(guild_id: int, user_id: int) -> None:
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO economy (guild_id, user_id, coins) VALUES (?, ?, 0)",
            (guild_id, user_id),
        )


def get_coins(guild_id: int, user_id: int) -> int:
    ensure_economy(guild_id, user_id)
    with db() as con:
        row = con.execute(
            "SELECT coins FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    return int(row["coins"])


def set_coins_db(guild_id: int, user_id: int, amount: int) -> None:
    ensure_economy(guild_id, user_id)
    with db() as con:
        con.execute(
            "UPDATE economy SET coins=? WHERE guild_id=? AND user_id=?",
            (max(0, amount), guild_id, user_id),
        )


def add_coins_db(guild_id: int, user_id: int, amount: int) -> int:
    current = get_coins(guild_id, user_id)
    new_amount = max(0, current + amount)
    set_coins_db(guild_id, user_id, new_amount)
    return new_amount


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def can_moderate(actor: discord.Member, target: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    if target == guild.owner:
        return False, "Nu poți modera proprietarul serverului."
    if target == actor:
        return False, "Nu poți folosi comanda asupra ta."
    if guild.me is None or guild.me.top_role <= target.top_role:
        return False, "Rolul botului trebuie să fie deasupra rolului membrului."
    if actor != guild.owner and actor.top_role <= target.top_role:
        return False, "Rolul tău trebuie să fie deasupra rolului membrului."
    return True, ""


async def require_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ Doar administratorii pot folosi această comandă.",
            ephemeral=True,
        )
        return False
    return True


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verifică-te",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="persistent_verify_button",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        with db() as con:
            row = con.execute(
                "SELECT role_id FROM verification WHERE guild_id=?",
                (interaction.guild.id,),
            ).fetchone()
        if not row:
            await interaction.response.send_message("❌ Verificarea nu este configurată.", ephemeral=True)
            return
        role = interaction.guild.get_role(int(row["role_id"]))
        if role is None:
            await interaction.response.send_message("❌ Rolul de verificare nu mai există.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Verificare prin buton")
            await interaction.response.send_message(
                f"✅ Ai primit rolul {role.mention}.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Botul nu poate adăuga rolul. Mută rolul botului deasupra rolului de verificare.",
                ephemeral=True,
            )


@bot.event
async def on_ready():
    init_db()
    bot.add_view(VerifyView())
    try:
        synced = await bot.tree.sync()
        print(f"Sincronizate {len(synced)} comenzi slash.")
    except Exception as exc:
        print(f"Eroare la sincronizare: {exc}")
    print(f"{bot.user} este online!")


# ---------------- MODERARE ----------------

@bot.tree.command(name="ban", description="Banează un membru")
@app_commands.describe(membru="Membrul", motiv="Motivul")
async def ban(interaction: discord.Interaction, membru: discord.Member, motiv: str = "Fără motiv"):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return
    try:
        await membru.ban(reason=f"{motiv} | De {interaction.user}")
        await interaction.response.send_message(f"🔨 {membru.mention} a fost banat.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botul nu are permisiunea necesară.", ephemeral=True)


@bot.tree.command(name="kick", description="Dă afară un membru")
@app_commands.describe(membru="Membrul", motiv="Motivul")
async def kick(interaction: discord.Interaction, membru: discord.Member, motiv: str = "Fără motiv"):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return
    try:
        await membru.kick(reason=f"{motiv} | De {interaction.user}")
        await interaction.response.send_message(f"👢 {membru.mention} a fost dat afară.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botul nu are permisiunea necesară.", ephemeral=True)


@bot.tree.command(name="clear", description="Șterge mesaje din canal")
@app_commands.describe(numar="Număr de mesaje, maximum 100")
async def clear(interaction: discord.Interaction, numar: app_commands.Range[int, 1, 100]):
    if not await require_admin(interaction):
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ Folosește comanda într-un canal text.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=numar)
    await interaction.followup.send(f"🧹 Am șters **{len(deleted)}** mesaje.", ephemeral=True)


@bot.tree.command(name="timeout", description="Pune un membru în timeout")
@app_commands.describe(membru="Membrul", minute="Durata în minute", motiv="Motivul")
async def timeout_cmd(
    interaction: discord.Interaction,
    membru: discord.Member,
    minute: app_commands.Range[int, 1, 40320],
    motiv: str = "Fără motiv",
):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return
    await membru.timeout(timedelta(minutes=minute), reason=f"{motiv} | De {interaction.user}")
    await interaction.response.send_message(
        f"⏳ {membru.mention} a primit timeout pentru **{minute} minute**.\nMotiv: **{motiv}**"
    )


@bot.tree.command(name="untimeout", description="Elimină timeout-ul unui membru")
async def untimeout(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return
    await membru.timeout(None, reason=f"Scos de {interaction.user}")
    await interaction.response.send_message(f"✅ Timeout eliminat pentru {membru.mention}.")


@bot.tree.command(name="warn", description="Avertizează un membru")
@app_commands.describe(membru="Membrul", motiv="Motivul")
async def warn(interaction: discord.Interaction, membru: discord.Member, motiv: str):
    if not await require_admin(interaction):
        return
    with db() as con:
        con.execute(
            "INSERT INTO warnings (guild_id,user_id,moderator_id,reason,created_at) VALUES (?,?,?,?,?)",
            (interaction.guild.id, membru.id, interaction.user.id, motiv, datetime.now(timezone.utc).isoformat()),
        )
    try:
        await membru.send(f"⚠️ Ai primit un avertisment pe **{interaction.guild.name}**.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f"⚠️ {membru.mention} a primit avertisment.\nMotiv: **{motiv}**")


@bot.tree.command(name="warnings", description="Arată avertismentele unui membru")
async def warnings_cmd(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    with db() as con:
        rows = con.execute(
            "SELECT id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id",
            (interaction.guild.id, membru.id),
        ).fetchall()
    if not rows:
        await interaction.response.send_message(f"✅ {membru.mention} nu are avertismente.", ephemeral=True)
        return
    text = "\n".join(f"`#{r['id']}` {r['reason']}" for r in rows[:20])
    await interaction.response.send_message(f"⚠️ Avertismente pentru {membru.mention}:\n{text}", ephemeral=True)


@bot.tree.command(name="unwarn", description="Șterge un avertisment după ID")
@app_commands.describe(id_avertisment="ID-ul afișat de /warnings")
async def unwarn(interaction: discord.Interaction, id_avertisment: int):
    if not await require_admin(interaction):
        return
    with db() as con:
        row = con.execute(
            "SELECT id FROM warnings WHERE id=? AND guild_id=?",
            (id_avertisment, interaction.guild.id),
        ).fetchone()
        if row:
            con.execute("DELETE FROM warnings WHERE id=?", (id_avertisment,))
    if not row:
        await interaction.response.send_message("❌ Avertismentul nu există.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Avertismentul `#{id_avertisment}` a fost șters.")


# ---------------- GIVEAWAY ----------------

@bot.tree.command(name="gcreate", description="Creează un giveaway")
@app_commands.describe(
    premiu="Premiul",
    minute="Durata în minute",
    castigatori="Numărul de câștigători",
)
async def gcreate(
    interaction: discord.Interaction,
    premiu: str,
    minute: app_commands.Range[int, 1, 10080],
    castigatori: app_commands.Range[int, 1, 10] = 1,
):
    if not await require_admin(interaction):
        return
    end_at = datetime.now(timezone.utc) + timedelta(minutes=minute)
    embed = discord.Embed(
        title="🎉 GIVEAWAY",
        description=f"Premiu: **{premiu}**\nApasă 🎉 pentru participare!\nSe termină <t:{int(end_at.timestamp())}:R>",
    )
    embed.set_footer(text=f"{castigatori} câștigător(i) • Creat de {interaction.user}")
    await interaction.response.send_message("✅ Giveaway creat.", ephemeral=True)
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("🎉")
    giveaways[message.id] = {
        "channel_id": interaction.channel.id,
        "prize": premiu,
        "winners": castigatori,
        "end_at": end_at,
    }
    await asyncio.sleep(minute * 60)
    if message.id in giveaways:
        await finish_giveaway(interaction.guild, message.id)


async def finish_giveaway(guild: discord.Guild, message_id: int):
    data = giveaways.pop(message_id, None)
    if not data:
        return
    channel = guild.get_channel(data["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    users = []
    if reaction:
        async for user in reaction.users():
            if not user.bot:
                users.append(user)
    if not users:
        await channel.send(f"❌ Giveaway-ul pentru **{data['prize']}** nu are participanți.")
        return
    winners = random.sample(users, min(data["winners"], len(users)))
    last_giveaway_winner[message_id] = winners[0].id
    mentions = ", ".join(user.mention for user in winners)
    await channel.send(f"🏆 Câștigător(i): {mentions}\nPremiu: **{data['prize']}**")


@bot.tree.command(name="gend", description="Încheie un giveaway după ID-ul mesajului")
async def gend(interaction: discord.Interaction, id_mesaj: str):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        message_id = int(id_mesaj)
    except ValueError:
        await interaction.followup.send("❌ ID invalid.", ephemeral=True)
        return
    if message_id not in giveaways:
        await interaction.followup.send("❌ Giveaway-ul nu este activ sau botul a fost repornit.", ephemeral=True)
        return
    await finish_giveaway(interaction.guild, message_id)
    await interaction.followup.send("✅ Giveaway încheiat.", ephemeral=True)


@bot.tree.command(name="reroll", description="Alege alt câștigător pentru un giveaway")
async def reroll(interaction: discord.Interaction, id_mesaj: str):
    if not await require_admin(interaction):
        return
    try:
        message_id = int(id_mesaj)
        message = await interaction.channel.fetch_message(message_id)
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("❌ Mesaj invalid.", ephemeral=True)
        return
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    users = []
    if reaction:
        async for user in reaction.users():
            if not user.bot:
                users.append(user)
    if not users:
        await interaction.response.send_message("❌ Nu există participanți.", ephemeral=True)
        return
    winner = random.choice(users)
    await interaction.response.send_message(f"🔁 Noul câștigător este {winner.mention}!")


# ---------------- UTILITARE ----------------

@bot.tree.command(name="embed", description="Trimite un mesaj embed")
@app_commands.describe(titlu="Titlul", mesaj="Conținutul", culoare_hex="Exemplu: 5865F2")
async def embed_cmd(interaction: discord.Interaction, titlu: str, mesaj: str, culoare_hex: str = "5865F2"):
    if not await require_admin(interaction):
        return
    try:
        color = discord.Color(int(culoare_hex.lstrip("#"), 16))
    except ValueError:
        color = discord.Color.blurple()
    embed = discord.Embed(title=titlu, description=mesaj, color=color)
    embed.set_footer(text=f"Trimis de {interaction.user}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Arată toate comenzile")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Comenzile botului", color=discord.Color.blurple())
    embed.add_field(name="Moderare", value="/ban /kick /clear /timeout /untimeout /warn /warnings /unwarn", inline=False)
    embed.add_field(name="Giveaway", value="/gcreate /gend /reroll", inline=False)
    embed.add_field(name="Utilitare", value="/embed /help", inline=False)
    embed.add_field(name="Economie", value="/balance /daily /work /shop /buy /leaderboard /setcoins /addcoins /removecoins /resetcoins", inline=False)
    embed.add_field(name="Reputație", value="/rep add /rep remove /rep check /rep leaderboard", inline=False)
    embed.add_field(name="Distracție", value="/dog /cat /meme", inline=False)
    embed.add_field(name="Verificare", value="/setup-verificare", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- ECONOMIE ----------------

@bot.tree.command(name="balance", description="Arată balanța")
async def balance(interaction: discord.Interaction, membru: Optional[discord.Member] = None):
    target = membru or interaction.user
    coins = get_coins(interaction.guild.id, target.id)
    await interaction.response.send_message(f"💰 {target.mention} are **{coins} coins**.")


@bot.tree.command(name="daily", description="Primește recompensa zilnică")
async def daily(interaction: discord.Interaction):
    ensure_economy(interaction.guild.id, interaction.user.id)
    now = datetime.now(timezone.utc)
    with db() as con:
        row = con.execute(
            "SELECT last_daily FROM economy WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, interaction.user.id),
        ).fetchone()
        if row["last_daily"]:
            last = datetime.fromisoformat(row["last_daily"])
            remaining = timedelta(hours=24) - (now - last)
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"⏳ Revino peste **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return
        reward = random.randint(150, 300)
        con.execute(
            "UPDATE economy SET coins=coins+?, last_daily=? WHERE guild_id=? AND user_id=?",
            (reward, now.isoformat(), interaction.guild.id, interaction.user.id),
        )
    await interaction.response.send_message(f"🎁 Ai primit **{reward} coins**.")


@bot.tree.command(name="work", description="Muncește pentru coins")
async def work(interaction: discord.Interaction):
    ensure_economy(interaction.guild.id, interaction.user.id)
    now = datetime.now(timezone.utc)
    with db() as con:
        row = con.execute(
            "SELECT last_work FROM economy WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, interaction.user.id),
        ).fetchone()
        if row["last_work"]:
            last = datetime.fromisoformat(row["last_work"])
            remaining = timedelta(minutes=30) - (now - last)
            if remaining.total_seconds() > 0:
                minutes = max(1, int(remaining.total_seconds() // 60))
                await interaction.response.send_message(
                    f"⏳ Mai așteaptă **{minutes} minute**.",
                    ephemeral=True,
                )
                return
        reward = random.randint(40, 120)
        con.execute(
            "UPDATE economy SET coins=coins+?, last_work=? WHERE guild_id=? AND user_id=?",
            (reward, now.isoformat(), interaction.guild.id, interaction.user.id),
        )
    jobs = ["programator", "șofer", "bucătar", "constructor", "designer"]
    await interaction.response.send_message(f"🛠️ Ai lucrat ca **{random.choice(jobs)}** și ai primit **{reward} coins**.")


@bot.tree.command(name="shop", description="Arată magazinul")
async def shop(interaction: discord.Interaction):
    text = "\n".join(f"• **{name}** — {price} coins" for name, price in SHOP_ITEMS.items())
    await interaction.response.send_message(f"🛒 **Magazin**\n{text}")


@bot.tree.command(name="buy", description="Cumpără un obiect")
@app_commands.describe(obiect="Numele obiectului din /shop")
async def buy(interaction: discord.Interaction, obiect: str):
    item = obiect.lower()
    if item not in SHOP_ITEMS:
        await interaction.response.send_message("❌ Obiect inexistent. Folosește /shop.", ephemeral=True)
        return
    price = SHOP_ITEMS[item]
    coins = get_coins(interaction.guild.id, interaction.user.id)
    if coins < price:
        await interaction.response.send_message("❌ Nu ai destui coins.", ephemeral=True)
        return
    set_coins_db(interaction.guild.id, interaction.user.id, coins - price)
    with db() as con:
        con.execute(
            """INSERT INTO inventory (guild_id,user_id,item,amount) VALUES (?,?,?,1)
               ON CONFLICT(guild_id,user_id,item) DO UPDATE SET amount=amount+1""",
            (interaction.guild.id, interaction.user.id, item),
        )
    await interaction.response.send_message(f"✅ Ai cumpărat **{item}** pentru **{price} coins**.")


@bot.tree.command(name="leaderboard", description="Clasamentul economiei")
async def leaderboard(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT user_id,coins FROM economy WHERE guild_id=? ORDER BY coins DESC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Nu există date încă.")
        return
    lines = []
    for i, row in enumerate(rows, 1):
        member = interaction.guild.get_member(row["user_id"])
        name = member.mention if member else f"<@{row['user_id']}>"
        lines.append(f"**{i}.** {name} — {row['coins']} coins")
    await interaction.response.send_message("🏆 **Leaderboard economie**\n" + "\n".join(lines))


@bot.tree.command(name="setcoins", description="Setează coins unui membru")
async def setcoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 0, 100000000]):
    if not await require_admin(interaction):
        return
    set_coins_db(interaction.guild.id, membru.id, suma)
    await interaction.response.send_message(f"✅ {membru.mention} are acum **{suma} coins**.")


@bot.tree.command(name="addcoins", description="Adaugă coins unui membru")
async def addcoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 1, 100000000]):
    if not await require_admin(interaction):
        return
    total = add_coins_db(interaction.guild.id, membru.id, suma)
    await interaction.response.send_message(f"✅ Am adăugat **{suma} coins**. Total: **{total}**.")


@bot.tree.command(name="removecoins", description="Elimină coins unui membru")
async def removecoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 1, 100000000]):
    if not await require_admin(interaction):
        return
    total = add_coins_db(interaction.guild.id, membru.id, -suma)
    await interaction.response.send_message(f"✅ Am eliminat **{suma} coins**. Total: **{total}**.")


@bot.tree.command(name="resetcoins", description="Resetează coins unui membru")
async def resetcoins(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    set_coins_db(interaction.guild.id, membru.id, 0)
    await interaction.response.send_message(f"✅ Coins resetați pentru {membru.mention}.")


# ---------------- REPUTAȚIE ----------------

rep = app_commands.Group(name="rep", description="Comenzi de reputație")


@rep.command(name="add", description="Adaugă reputație")
async def rep_add(interaction: discord.Interaction, membru: discord.Member, puncte: app_commands.Range[int, 1, 100] = 1):
    if not await require_admin(interaction):
        return
    with db() as con:
        con.execute(
            """INSERT INTO reputation (guild_id,user_id,points) VALUES (?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET points=points+excluded.points""",
            (interaction.guild.id, membru.id, puncte),
        )
    await interaction.response.send_message(f"✅ Am adăugat **{puncte} rep** lui {membru.mention}.")


@rep.command(name="remove", description="Elimină reputație")
async def rep_remove(interaction: discord.Interaction, membru: discord.Member, puncte: app_commands.Range[int, 1, 100] = 1):
    if not await require_admin(interaction):
        return
    with db() as con:
        row = con.execute(
            "SELECT points FROM reputation WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, membru.id),
        ).fetchone()
        current = int(row["points"]) if row else 0
        new = max(0, current - puncte)
        con.execute(
            """INSERT INTO reputation (guild_id,user_id,points) VALUES (?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET points=excluded.points""",
            (interaction.guild.id, membru.id, new),
        )
    await interaction.response.send_message(f"✅ {membru.mention} are acum **{new} rep**.")


@rep.command(name="check", description="Verifică reputația")
async def rep_check(interaction: discord.Interaction, membru: Optional[discord.Member] = None):
    target = membru or interaction.user
    with db() as con:
        row = con.execute(
            "SELECT points FROM reputation WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, target.id),
        ).fetchone()
    points = int(row["points"]) if row else 0
    await interaction.response.send_message(f"⭐ {target.mention} are **{points} rep**.")


@rep.command(name="leaderboard", description="Clasamentul reputației")
async def rep_leaderboard(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT user_id,points FROM reputation WHERE guild_id=? ORDER BY points DESC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Nu există date încă.")
        return
    text = "\n".join(f"**{i}.** <@{r['user_id']}> — {r['points']} rep" for i, r in enumerate(rows, 1))
    await interaction.response.send_message("⭐ **Leaderboard reputație**\n" + text)


bot.tree.add_command(rep)


# ---------------- DISTRACȚIE ----------------

async def fetch_json(url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()


@bot.tree.command(name="dog", description="Trimite o imagine cu un câine")
async def dog(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = await fetch_json("https://dog.ceo/api/breeds/image/random")
        embed = discord.Embed(title="🐶 Câine")
        embed.set_image(url=data["message"])
        await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send("❌ Nu am putut încărca imaginea.")


@bot.tree.command(name="cat", description="Trimite o imagine cu o pisică")
async def cat(interaction: discord.Interaction):
    embed = discord.Embed(title="🐱 Pisică")
    embed.set_image(url=f"https://cataas.com/cat?width=700&height=500&ts={random.randint(1, 999999)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="meme", description="Trimite un meme aleatoriu")
async def meme(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = await fetch_json("https://meme-api.com/gimme")
        embed = discord.Embed(title=data.get("title", "Meme"))
        embed.set_image(url=data["url"])
        embed.set_footer(text=f"r/{data.get('subreddit', 'memes')}")
        await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send("❌ Nu am putut încărca meme-ul.")


# ---------------- VERIFICARE ----------------

@bot.tree.command(name="setup-verificare", description="Configurează verificarea cu buton")
@app_commands.describe(rol="Rolul primit după verificare", canal="Canalul mesajului")
async def setup_verificare(
    interaction: discord.Interaction,
    rol: discord.Role,
    canal: discord.TextChannel,
):
    if not await require_admin(interaction):
        return
    if interaction.guild.me is None or rol >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ Mută rolul botului deasupra rolului de verificare.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="✅ Verificare",
        description="Apasă butonul de mai jos pentru a primi acces pe server.",
        color=discord.Color.green(),
    )
    message = await canal.send(embed=embed, view=VerifyView())
    with db() as con:
        con.execute(
            """INSERT INTO verification (guild_id,role_id,channel_id,message_id)
               VALUES (?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET
               role_id=excluded.role_id,
               channel_id=excluded.channel_id,
               message_id=excluded.message_id""",
            (interaction.guild.id, rol.id, canal.id, message.id),
        )
    await interaction.response.send_message(
        f"✅ Verificarea a fost configurată în {canal.mention}.",
        ephemeral=True,
    )


bot.run(TOKEN)
'''

requirements = """discord.py>=2.4,<3
aiohttp>=3.9,<4
"""

readme = """# Discord Full Bot

## Comenzi
- Moderare: /ban, /kick, /clear, /timeout, /untimeout, /warn, /warnings, /unwarn
- Giveaway: /gcreate, /gend, /reroll
- Utilitare: /embed, /help
- Economie: /balance, /daily, /work, /shop, /buy, /leaderboard, /setcoins, /addcoins, /removecoins, /resetcoins
- Reputație: /rep add, /rep remove, /rep check, /rep leaderboard
- Distracție: /dog, /cat, /meme
- Verificare: /setup-verificare

## Railway
1. Pune `bot.py` și `requirements.txt` în repository.
2. În Railway adaugă variabila `DISCORD_TOKEN`.
3. În Discord Developer Portal activează:
   - Server Members Intent
   - Message Content Intent
4. Invită botul cu scope-urile:
   - bot
   - applications.commands
5. Acordă-i Administrator sau permisiunile necesare.
6. Rolul botului trebuie să fie deasupra rolurilor pe care le administrează.

## Observații
- Economia, reputația și avertismentele folosesc SQLite (`bot.db`).
- Pe Railway, pentru păstrarea bazei de date după redeploy, montează un Volume și setează DB_PATH către acel volum dacă dorești persistență completă.
- Giveaway-urile active sunt ținute în memorie și se pierd dacă botul este repornit.
"""

(base / "bot.py").write_text(bot_code.strip() + "\n", encoding="utf-8")
(base / "requirements.txt").write_text(requirements, encoding="utf-8")
(base / "README.md").write_text(readme, encoding="utf-8")

zip_path = Path("/mnt/data/discord_full_bot.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for p in base.iterdir():
        z.write(p, arcname=p.name)

print(zip_path)

