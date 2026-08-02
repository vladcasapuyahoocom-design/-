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
    raise RuntimeError("LipseÈ™te variabila DISCORD_TOKEN.")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_PATH = "bot.db"

# giveaway_message_id -> date
giveaways: dict[int, dict] = {}
last_giveaway_winner: dict[int, int] = {}

SHOP_ITEMS = {
    "vip": 10000,
    "mvp": 25000,
    "elite": 50000,
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS ticket_config (
                guild_id INTEGER PRIMARY KEY,
                support_role_id INTEGER NOT NULL,
                category_id INTEGER
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
        return False, "Nu poÈ›i modera proprietarul serverului."
    if target == actor:
        return False, "Nu poÈ›i folosi comanda asupra ta."
    if guild.me is None or guild.me.top_role <= target.top_role:
        return False, "Rolul botului trebuie sÄƒ fie deasupra rolului membrului."
    if actor != guild.owner and actor.top_role <= target.top_role:
        return False, "Rolul tÄƒu trebuie sÄƒ fie deasupra rolului membrului."
    return True, ""


async def require_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message(
            "âŒ Doar administratorii pot folosi aceastÄƒ comandÄƒ.",
            ephemeral=True,
        )
        return False
    return True


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="VerificÄƒ-te",
        style=discord.ButtonStyle.success,
        emoji="âœ…",
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
            await interaction.response.send_message("âŒ Verificarea nu este configuratÄƒ.", ephemeral=True)
            return
        role = interaction.guild.get_role(int(row["role_id"]))
        if role is None:
            await interaction.response.send_message("âŒ Rolul de verificare nu mai existÄƒ.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Verificare prin buton")
            await interaction.response.send_message(
                f"âœ… Ai primit rolul {role.mention}.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "âŒ Botul nu poate adÄƒuga rolul. MutÄƒ rolul botului deasupra rolului de verificare.",
                ephemeral=True,
            )


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ÃŽnchide ticketul",
        style=discord.ButtonStyle.danger,
        emoji="ðŸ”’",
        custom_id="persistent_close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.topic:
            await interaction.response.send_message("âŒ Acesta nu este un canal de ticket.", ephemeral=True)
            return
        if not interaction.channel.topic.startswith("ticket_owner:"):
            await interaction.response.send_message("âŒ Acesta nu este un canal de ticket.", ephemeral=True)
            return
        await interaction.response.send_message("ðŸ”’ Ticketul se va Ã®nchide Ã®n 5 secunde.")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket Ã®nchis de {interaction.user}")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="CreeazÄƒ un ticket",
        style=discord.ButtonStyle.primary,
        emoji="ðŸŽ«",
        custom_id="persistent_create_ticket",
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return

        with db() as con:
            config = con.execute(
                "SELECT support_role_id, category_id FROM ticket_config WHERE guild_id=?",
                (interaction.guild.id,),
            ).fetchone()

        if not config:
            await interaction.response.send_message("âŒ Sistemul de ticket nu este configurat.", ephemeral=True)
            return

        topic = f"ticket_owner:{interaction.user.id}"
        existing = discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel) and channel.topic == topic,
            interaction.guild.channels,
        )
        if existing:
            await interaction.response.send_message(
                f"âŒ Ai deja un ticket deschis: {existing.mention}", ephemeral=True
            )
            return

        support_role = interaction.guild.get_role(int(config["support_role_id"]))
        category = interaction.guild.get_channel(int(config["category_id"])) if config["category_id"] else None
        if support_role is None:
            await interaction.response.send_message("âŒ Rolul de suport nu mai existÄƒ.", ephemeral=True)
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            support_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if interaction.guild.me:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            )

        try:
            channel = await interaction.guild.create_text_channel(
                name=f"ticket-{interaction.user.name}"[:100],
                category=category if isinstance(category, discord.CategoryChannel) else None,
                topic=topic,
                overwrites=overwrites,
                reason=f"Ticket creat de {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "âŒ Botul nu are permisiunea Manage Channels.", ephemeral=True
            )
            return

        await channel.send(
            f"{interaction.user.mention} {support_role.mention}\n"
            "Bine ai venit! ExplicÄƒ problema cÃ¢t mai clar È™i aÈ™teaptÄƒ rÄƒspunsul echipei.",
            view=CloseTicketView(),
        )
        await interaction.response.send_message(
            f"âœ… Ticketul tÄƒu a fost creat: {channel.mention}", ephemeral=True
        )


@bot.event
async def on_ready():
    init_db()
    bot.add_view(VerifyView())
    bot.add_view(TicketView())
    bot.add_view(CloseTicketView())
    try:
        synced = await bot.tree.sync()
        print(f"Sincronizate {len(synced)} comenzi slash.")
    except Exception as exc:
        print(f"Eroare la sincronizare: {exc}")
    print(f"{bot.user} este online!")


# ---------------- MODERARE ----------------

@bot.tree.command(name="setup-ticket", description="ConfigureazÄƒ È™i trimite panoul de ticket")
@app_commands.describe(
    rol_suport="Rolul care poate vedea È™i rÄƒspunde la tickete",
    categorie="Categoria Ã®n care vor fi create ticketele",
)
async def setup_ticket(
    interaction: discord.Interaction,
    rol_suport: discord.Role,
    categorie: Optional[discord.CategoryChannel] = None,
):
    if not await require_admin(interaction):
        return
    if interaction.guild is None:
        return

    with db() as con:
        con.execute(
            """INSERT INTO ticket_config (guild_id, support_role_id, category_id)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
               support_role_id=excluded.support_role_id,
               category_id=excluded.category_id""",
            (interaction.guild.id, rol_suport.id, categorie.id if categorie else None),
        )

    embed = discord.Embed(
        title="ðŸŽ« Suport & AsistenÈ›Äƒ",
        description=(
            "Ai nevoie de ajutor sau vrei sÄƒ discuÈ›i cu echipa noastrÄƒ?\n"
            "ApasÄƒ butonul de mai jos pentru a crea un ticket.\n\n"
            "Te rugÄƒm sÄƒ explici problema cÃ¢t mai clar È™i sÄƒ aÈ™tepÈ›i rÄƒspunsul "
            "unui membru staff. Abuzarea sistemului de ticket poate fi sancÈ›ionatÄƒ."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=TicketView())

@bot.tree.command(name="ban", description="BaneazÄƒ un membru")
@app_commands.describe(membru="Membrul", motiv="Motivul")
async def ban(interaction: discord.Interaction, membru: discord.Member, motiv: str = "FÄƒrÄƒ motiv"):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"âŒ {msg}", ephemeral=True)
        return
    try:
        await membru.ban(reason=f"{motiv} | De {interaction.user}")
        await interaction.response.send_message(f"ðŸ”¨ {membru.mention} a fost banat.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        await interaction.response.send_message("âŒ Botul nu are permisiunea necesarÄƒ.", ephemeral=True)


@bot.tree.command(name="kick", description="DÄƒ afarÄƒ un membru")
@app_commands.describe(membru="Membrul", motiv="Motivul")
async def kick(interaction: discord.Interaction, membru: discord.Member, motiv: str = "FÄƒrÄƒ motiv"):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"âŒ {msg}", ephemeral=True)
        return
    try:
        await membru.kick(reason=f"{motiv} | De {interaction.user}")
        await interaction.response.send_message(f"ðŸ‘¢ {membru.mention} a fost dat afarÄƒ.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        await interaction.response.send_message("âŒ Botul nu are permisiunea necesarÄƒ.", ephemeral=True)


@bot.tree.command(name="clear", description="È˜terge mesaje din canal")
@app_commands.describe(numar="NumÄƒr de mesaje, maximum 100")
async def clear(interaction: discord.Interaction, numar: app_commands.Range[int, 1, 100]):
    if not await require_admin(interaction):
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("âŒ FoloseÈ™te comanda Ã®ntr-un canal text.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=numar)
    await interaction.followup.send(f"ðŸ§¹ Am È™ters **{len(deleted)}** mesaje.", ephemeral=True)


@bot.tree.command(name="timeout", description="Pune un membru Ã®n timeout")
@app_commands.describe(membru="Membrul", minute="Durata Ã®n minute", motiv="Motivul")
async def timeout_cmd(
    interaction: discord.Interaction,
    membru: discord.Member,
    minute: app_commands.Range[int, 1, 40320],
    motiv: str = "FÄƒrÄƒ motiv",
):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"âŒ {msg}", ephemeral=True)
        return
    await membru.timeout(timedelta(minutes=minute), reason=f"{motiv} | De {interaction.user}")
    await interaction.response.send_message(
        f"â³ {membru.mention} a primit timeout pentru **{minute} minute**.\nMotiv: **{motiv}**"
    )


@bot.tree.command(name="untimeout", description="EliminÄƒ timeout-ul unui membru")
async def untimeout(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    ok, msg = can_moderate(interaction.user, membru, interaction.guild)
    if not ok:
        await interaction.response.send_message(f"âŒ {msg}", ephemeral=True)
        return
    await membru.timeout(None, reason=f"Scos de {interaction.user}")
    await interaction.response.send_message(f"âœ… Timeout eliminat pentru {membru.mention}.")


@bot.tree.command(name="warn", description="AvertizeazÄƒ un membru")
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
        await membru.send(f"âš ï¸ Ai primit un avertisment pe **{interaction.guild.name}**.\nMotiv: **{motiv}**")
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f"âš ï¸ {membru.mention} a primit avertisment.\nMotiv: **{motiv}**")


@bot.tree.command(name="warnings", description="AratÄƒ avertismentele unui membru")
async def warnings_cmd(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    with db() as con:
        rows = con.execute(
            "SELECT id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id",
            (interaction.guild.id, membru.id),
        ).fetchall()
    if not rows:
        await interaction.response.send_message(f"âœ… {membru.mention} nu are avertismente.", ephemeral=True)
        return
    text = "\n".join(f"`#{r['id']}` {r['reason']}" for r in rows[:20])
    await interaction.response.send_message(f"âš ï¸ Avertismente pentru {membru.mention}:\n{text}", ephemeral=True)


@bot.tree.command(name="unwarn", description="È˜terge un avertisment dupÄƒ ID")
@app_commands.describe(id_avertisment="ID-ul afiÈ™at de /warnings")
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
        await interaction.response.send_message("âŒ Avertismentul nu existÄƒ.", ephemeral=True)
        return
    await interaction.response.send_message(f"âœ… Avertismentul `#{id_avertisment}` a fost È™ters.")


# ---------------- GIVEAWAY ----------------

@bot.tree.command(name="gcreate", description="CreeazÄƒ un giveaway")
@app_commands.describe(
    premiu="Premiul",
    minute="Durata Ã®n minute",
    castigatori="NumÄƒrul de cÃ¢È™tigÄƒtori",
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
        title="ðŸŽ‰ GIVEAWAY",
        description=f"Premiu: **{premiu}**\nApasÄƒ ðŸŽ‰ pentru participare!\nSe terminÄƒ <t:{int(end_at.timestamp())}:R>",
    )
    embed.set_footer(text=f"{castigatori} cÃ¢È™tigÄƒtor(i) â€¢ Creat de {interaction.user}")
    await interaction.response.send_message("âœ… Giveaway creat.", ephemeral=True)
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("ðŸŽ‰")
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
    reaction = discord.utils.get(message.reactions, emoji="ðŸŽ‰")
    users = []
    if reaction:
        async for user in reaction.users():
            if not user.bot:
                users.append(user)
    if not users:
        await channel.send(f"âŒ Giveaway-ul pentru **{data['prize']}** nu are participanÈ›i.")
        return
    winners = random.sample(users, min(data["winners"], len(users)))
    last_giveaway_winner[message_id] = winners[0].id
    mentions = ", ".join(user.mention for user in winners)
    await channel.send(f"ðŸ† CÃ¢È™tigÄƒtor(i): {mentions}\nPremiu: **{data['prize']}**")


@bot.tree.command(name="gend", description="ÃŽncheie un giveaway dupÄƒ ID-ul mesajului")
async def gend(interaction: discord.Interaction, id_mesaj: str):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        message_id = int(id_mesaj)
    except ValueError:
        await interaction.followup.send("âŒ ID invalid.", ephemeral=True)
        return
    if message_id not in giveaways:
        await interaction.followup.send("âŒ Giveaway-ul nu este activ sau botul a fost repornit.", ephemeral=True)
        return
    await finish_giveaway(interaction.guild, message_id)
    await interaction.followup.send("âœ… Giveaway Ã®ncheiat.", ephemeral=True)


@bot.tree.command(name="reroll", description="Alege alt cÃ¢È™tigÄƒtor pentru un giveaway")
async def reroll(interaction: discord.Interaction, id_mesaj: str):
    if not await require_admin(interaction):
        return
    try:
        message_id = int(id_mesaj)
        message = await interaction.channel.fetch_message(message_id)
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("âŒ Mesaj invalid.", ephemeral=True)
        return
    reaction = discord.utils.get(message.reactions, emoji="ðŸŽ‰")
    users = []
    if reaction:
        async for user in reaction.users():
            if not user.bot:
                users.append(user)
    if not users:
        await interaction.response.send_message("âŒ Nu existÄƒ participanÈ›i.", ephemeral=True)
        return
    winner = random.choice(users)
    await interaction.response.send_message(f"ðŸ” Noul cÃ¢È™tigÄƒtor este {winner.mention}!")


# ---------------- UTILITARE ----------------

@bot.tree.command(name="embed", description="Trimite un mesaj embed")
@app_commands.describe(titlu="Titlul", mesaj="ConÈ›inutul", culoare_hex="Exemplu: 5865F2")
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


@bot.tree.command(name="help", description="AratÄƒ toate comenzile")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="ðŸ“š Comenzile botului", color=discord.Color.blurple())
    embed.add_field(name="Moderare", value="/ban /kick /clear /timeout /untimeout /warn /warnings /unwarn", inline=False)
    embed.add_field(name="Giveaway", value="/gcreate /gend /reroll", inline=False)
    embed.add_field(name="Utilitare", value="/embed /help", inline=False)
    embed.add_field(name="Economie", value="/balance /daily /work /shop /buy /leaderboard /setcoins /addcoins /removecoins /resetcoins", inline=False)
    embed.add_field(name="ReputaÈ›ie", value="/rep add /rep remove /rep check /rep leaderboard", inline=False)
    embed.add_field(name="DistracÈ›ie", value="/dog /cat /meme", inline=False)
    embed.add_field(name="Verificare", value="/setup-verificare", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- ECONOMIE ----------------

@bot.tree.command(name="balance", description="AratÄƒ balanÈ›a")
async def balance(interaction: discord.Interaction, membru: Optional[discord.Member] = None):
    target = membru or interaction.user
    coins = get_coins(interaction.guild.id, target.id)
    await interaction.response.send_message(f"ðŸ’° {target.mention} are **{coins} coins**.")


@bot.tree.command(name="daily", description="PrimeÈ™te recompensa zilnicÄƒ")
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
                    f"â³ Revino peste **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return
        reward = random.randint(500, 1500)
        con.execute(
            "UPDATE economy SET coins=coins+?, last_daily=? WHERE guild_id=? AND user_id=?",
            (reward, now.isoformat(), interaction.guild.id, interaction.user.id),
        )
    await interaction.response.send_message(f"ðŸŽ Ai primit **{reward} coins**.")


@bot.tree.command(name="work", description="MunceÈ™te pentru coins")
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
                    f"â³ Mai aÈ™teaptÄƒ **{minutes} minute**.",
                    ephemeral=True,
                )
                return
        reward = random.randint(40, 120)
        con.execute(
            "UPDATE economy SET coins=coins+?, last_work=? WHERE guild_id=? AND user_id=?",
            (reward, now.isoformat(), interaction.guild.id, interaction.user.id),
        )
    jobs = ["programator", "È™ofer", "bucÄƒtar", "constructor", "designer"]
    await interaction.response.send_message(f"ðŸ› ï¸ Ai lucrat ca **{random.choice(jobs)}** È™i ai primit **{reward} coins**.")


@bot.tree.command(name="shop", description="AratÄƒ magazinul")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(title="ðŸ›’ Magazin", color=discord.Color.blue())
    embed.add_field(name="ðŸ‘‘ VIP", value="**10.000 coins**", inline=False)
    embed.add_field(name="ðŸ’Ž MVP", value="**25.000 coins**", inline=False)
    embed.add_field(name="ðŸ”¥ ELITE", value="**50.000 coins**", inline=False)
    embed.set_footer(text="FoloseÈ™te /buy <vip | mvp | elite>")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="buy", description="CumpÄƒrÄƒ un obiect")
@app_commands.describe(obiect="Numele obiectului din /shop")
async def buy(interaction: discord.Interaction, obiect: str):
    item = obiect.lower()
    if item not in SHOP_ITEMS:
        await interaction.response.send_message("âŒ Obiect inexistent. FoloseÈ™te /shop.", ephemeral=True)
        return
    price = SHOP_ITEMS[item]
    coins = get_coins(interaction.guild.id, interaction.user.id)
    if coins < price:
        await interaction.response.send_message("âŒ Nu ai destui coins.", ephemeral=True)
        return
    set_coins_db(interaction.guild.id, interaction.user.id, coins - price)
    with db() as con:
        con.execute(
            """INSERT INTO inventory (guild_id,user_id,item,amount) VALUES (?,?,?,1)
               ON CONFLICT(guild_id,user_id,item) DO UPDATE SET amount=amount+1""",
            (interaction.guild.id, interaction.user.id, item),
        )
    await interaction.response.send_message(f"âœ… Ai cumpÄƒrat **{item}** pentru **{price} coins**.")


@bot.tree.command(name="leaderboard", description="Clasamentul economiei")
async def leaderboard(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT user_id,coins FROM economy WHERE guild_id=? ORDER BY coins DESC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Nu existÄƒ date Ã®ncÄƒ.")
        return
    lines = []
    for i, row in enumerate(rows, 1):
        member = interaction.guild.get_member(row["user_id"])
        name = member.mention if member else f"<@{row['user_id']}>"
        lines.append(f"**{i}.** {name} â€” {row['coins']} coins")
    await interaction.response.send_message("ðŸ† **Leaderboard economie**\n" + "\n".join(lines))


@bot.tree.command(name="setcoins", description="SeteazÄƒ coins unui membru")
async def setcoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 0, 100000000]):
    if not await require_admin(interaction):
        return
    set_coins_db(interaction.guild.id, membru.id, suma)
    await interaction.response.send_message(f"âœ… {membru.mention} are acum **{suma} coins**.")


@bot.tree.command(name="addcoins", description="AdaugÄƒ coins unui membru")
async def addcoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 1, 100000000]):
    if not await require_admin(interaction):
        return
    total = add_coins_db(interaction.guild.id, membru.id, suma)
    await interaction.response.send_message(f"âœ… Am adÄƒugat **{suma} coins**. Total: **{total}**.")


@bot.tree.command(name="removecoins", description="EliminÄƒ coins unui membru")
async def removecoins(interaction: discord.Interaction, membru: discord.Member, suma: app_commands.Range[int, 1, 100000000]):
    if not await require_admin(interaction):
        return
    total = add_coins_db(interaction.guild.id, membru.id, -suma)
    await interaction.response.send_message(f"âœ… Am eliminat **{suma} coins**. Total: **{total}**.")


@bot.tree.command(name="resetcoins", description="ReseteazÄƒ coins unui membru")
async def resetcoins(interaction: discord.Interaction, membru: discord.Member):
    if not await require_admin(interaction):
        return
    set_coins_db(interaction.guild.id, membru.id, 0)
    await interaction.response.send_message(f"âœ… Coins resetaÈ›i pentru {membru.mention}.")


# ---------------- REPUTAÈšIE ----------------

rep = app_commands.Group(name="rep", description="Comenzi de reputaÈ›ie")


@rep.command(name="add", description="AdaugÄƒ reputaÈ›ie")
async def rep_add(interaction: discord.Interaction, membru: discord.Member, puncte: app_commands.Range[int, 1, 100] = 1):
    if not await require_admin(interaction):
        return
    with db() as con:
        con.execute(
            """INSERT INTO reputation (guild_id,user_id,points) VALUES (?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET points=points+excluded.points""",
            (interaction.guild.id, membru.id, puncte),
        )
    await interaction.response.send_message(f"âœ… Am adÄƒugat **{puncte} rep** lui {membru.mention}.")


@rep.command(name="remove", description="EliminÄƒ reputaÈ›ie")
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
    await interaction.response.send_message(f"âœ… {membru.mention} are acum **{new} rep**.")


@rep.command(name="check", description="VerificÄƒ reputaÈ›ia")
async def rep_check(interaction: discord.Interaction, membru: Optional[discord.Member] = None):
    target = membru or interaction.user
    with db() as con:
        row = con.execute(
            "SELECT points FROM reputation WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, target.id),
        ).fetchone()
    points = int(row["points"]) if row else 0
    await interaction.response.send_message(f"â­ {target.mention} are **{points} rep**.")


@rep.command(name="leaderboard", description="Clasamentul reputaÈ›iei")
async def rep_leaderboard(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT user_id,points FROM reputation WHERE guild_id=? ORDER BY points DESC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Nu existÄƒ date Ã®ncÄƒ.")
        return
    text = "\n".join(f"**{i}.** <@{r['user_id']}> â€” {r['points']} rep" for i, r in enumerate(rows, 1))
    await interaction.response.send_message("â­ **Leaderboard reputaÈ›ie**\n" + text)


bot.tree.add_command(rep)


# ---------------- DISTRACÈšIE ----------------

async def fetch_json(url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()


@bot.tree.command(name="dog", description="Trimite o imagine cu un cÃ¢ine")
async def dog(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = await fetch_json("https://dog.ceo/api/breeds/image/random")
        embed = discord.Embed(title="ðŸ¶ CÃ¢ine")
        embed.set_image(url=data["message"])
        await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send("âŒ Nu am putut Ã®ncÄƒrca imaginea.")


@bot.tree.command(name="cat", description="Trimite o imagine cu o pisicÄƒ")
async def cat(interaction: discord.Interaction):
    embed = discord.Embed(title="ðŸ± PisicÄƒ")
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
        await interaction.followup.send("âŒ Nu am putut Ã®ncÄƒrca meme-ul.")


# ---------------- VERIFICARE ----------------

@bot.tree.command(name="setup-verificare", description="ConfigureazÄƒ verificarea cu buton")
@app_commands.describe(rol="Rolul primit dupÄƒ verificare", canal="Canalul mesajului")
async def setup_verificare(
    interaction: discord.Interaction,
    rol: discord.Role,
    canal: discord.TextChannel,
):
    if not await require_admin(interaction):
        return
    if interaction.guild.me is None or rol >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "âŒ MutÄƒ rolul botului deasupra rolului de verificare.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="âœ… Verificare",
        description="ApasÄƒ butonul de mai jos pentru a primi acces pe server.",
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
        f"âœ… Verificarea a fost configuratÄƒ Ã®n {canal.mention}.",
        ephemeral=True,
    )


bot.run(TOKEN)
