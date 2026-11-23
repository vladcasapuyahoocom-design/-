import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
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
log = logging.getLogger(__name__)
DB_PATH = "bot.db"

# giveaway_message_id -> date
giveaways: dict[int, dict] = {}
last_giveaway_winner: dict[int, int] = {}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS welcome_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS suggestion_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )
        """)


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


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Închide ticketul",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="persistent_close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.topic:
            await interaction.response.send_message("❌ Acesta nu este un canal de ticket.", ephemeral=True)
            return
        if not interaction.channel.topic.startswith("ticket_owner:"):
            await interaction.response.send_message("❌ Acesta nu este un canal de ticket.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Ticketul se va închide în 5 secunde.")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket închis de {interaction.user}")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Creează un ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
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
            await interaction.response.send_message("❌ Sistemul de ticket nu este configurat.", ephemeral=True)
            return

        topic = f"ticket_owner:{interaction.user.id}"
        existing = discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel) and channel.topic == topic,
            interaction.guild.channels,
        )
        if existing:
            await interaction.response.send_message(
                f"❌ Ai deja un ticket deschis: {existing.mention}", ephemeral=True
            )
            return

        support_role = interaction.guild.get_role(int(config["support_role_id"]))
        category = interaction.guild.get_channel(int(config["category_id"])) if config["category_id"] else None
        if support_role is None:
            await interaction.response.send_message("❌ Rolul de suport nu mai există.", ephemeral=True)
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
                "❌ Botul nu are permisiunea Manage Channels.", ephemeral=True
            )
            return

        await channel.send(
            f"{interaction.user.mention} {support_role.mention}\n"
            "Bine ai venit! Explică problema cât mai clar și așteaptă răspunsul echipei.",
            view=CloseTicketView(),
        )
        await interaction.response.send_message(
            f"✅ Ticketul tău a fost creat: {channel.mention}", ephemeral=True
        )


# ---------------- APLICAȚII STAFF ----------------

APPLY_CONFIG_FILE = "apply_config.json"
APPLY_COLOR = 0x1A1A1A
ANSWER_TIMEOUT = 900        # secunde per răspuns (15 minute)
MAX_ANSWER = 900            # caractere maxime per răspuns
APPLY_COOLDOWN = 6 * 3600   # pauză între două aplicații ale aceluiași utilizator
CANCEL_WORDS = {"anulează", "anuleaza", "anulare", "cancel"}

QUESTIONS = [
    "Care este numele tău de Discord?",
    "Câți ani ai?",
    "Din ce țară ești și ce fus orar ai?",
    "De cât timp faci parte din comunitatea noastră?",
    "Cât timp poți acorda serverului zilnic?",
    "Ai mai avut experiență ca staff pe alte servere Discord? Dacă da, unde și ce grad ai avut?",
    "De ce dorești să faci parte din echipa staff 𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄?",
    "Ce calități consideri că te recomandă pentru acest rol?",
    "Care crezi că este responsabilitatea principală a unui moderator?",
    "Cum ai proceda dacă doi membri se ceartă și încep să folosească un limbaj vulgar?",
    "Ce ai face dacă un membru încalcă regulile, dar este prietenul tău?",
    "Cum ai reacționa dacă un membru al staff-ului cu grad mai mare ar încălca regulamentul?",
    "Ce ai face dacă ai sancționat un membru, iar acesta consideră că sancțiunea este nedreaptă?",
    "Cum ai proceda dacă observi că un coleg din staff abuzează de comenzile de moderare?",
    "Ai ceva de adăugat sau dorești să ne transmiți un mesaj final?",
]


def load_apply_config() -> dict:
    if os.path.exists(APPLY_CONFIG_FILE):
        try:
            with open(APPLY_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_apply_config(data: dict) -> None:
    with open(APPLY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def get_owner_id(guild_id: int) -> Optional[int]:
    cfg = load_apply_config().get(str(guild_id), {})
    if cfg.get("owner"):
        return cfg["owner"]
    guild = bot.get_guild(guild_id)
    return guild.owner_id if guild else None


def build_pages(answers: list[str]) -> list[list[tuple[str, str]]]:
    """Împarte răspunsurile în pagini, ca să nu depășească limita de 6000 de caractere a unui embed."""
    pages, fields, size = [], [], 0
    for index, (question, answer) in enumerate(zip(QUESTIONS, answers), 1):
        name = f"{index}. {question}"[:256]
        value = answer[:1024]
        if fields and size + len(name) + len(value) > 4500:
            pages.append(fields)
            fields, size = [], 0
        fields.append((name, value))
        size += len(name) + len(value)
    if fields:
        pages.append(fields)
    return pages


class ApplyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Aplică pentru staff",
        style=discord.ButtonStyle.primary,
        custom_id="fd_apply:start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "StaffApply" = interaction.client.get_cog("StaffApply")
        await cog.start_application(interaction)


class ReviewView(discord.ui.View):
    """Butoanele pe care le vede owner-ul sub aplicație."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _decide(self, interaction: discord.Interaction, accepted: bool):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        footer = embed.footer.text if embed and embed.footer else ""
        match = re.search(r"ID:(\d+) \| G:(\d+)", footer or "")
        if not match:
            return await interaction.response.send_message(
                "Nu am putut citi datele aplicației.", ephemeral=True
            )

        user_id, guild_id = int(match.group(1)), int(match.group(2))
        owner_id = get_owner_id(guild_id)
        if interaction.user.id != owner_id:
            return await interaction.response.send_message(
                "Doar owner-ul poate decide asupra aplicațiilor.", ephemeral=True
            )

        notified = True
        try:
            applicant = await interaction.client.fetch_user(user_id)
            if accepted:
                text = (
                    "Felicitări! Aplicația ta pentru echipa staff 𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄 a fost **acceptată**. "
                    "Vei fi contactat în curând de echipă."
                )
                color = 0x2ECC71
            else:
                text = (
                    "Aplicația ta pentru echipa staff 𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄 a fost analizată și, momentan, "
                    "**nu a fost acceptată**. Poți aplica din nou pe viitor."
                )
                color = 0xE74C3C
            await applicant.send(embed=discord.Embed(title="Aplicație staff", description=text, color=color))
        except (discord.Forbidden, discord.HTTPException):
            notified = False

        embed.color = 0x2ECC71 if accepted else 0xE74C3C
        embed.add_field(
            name="Decizie",
            value=(
                ("Acceptată" if accepted else "Respinsă")
                + f" de {interaction.user.mention}"
                + ("" if notified else "\nCandidatul nu a putut fi notificat (mesaje private închise).")
            ),
            inline=False,
        )
        view = ReviewView()
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Acceptă", style=discord.ButtonStyle.success, custom_id="fd_apply:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, True)

    @discord.ui.button(label="Respinge", style=discord.ButtonStyle.danger, custom_id="fd_apply:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, False)


class StaffApply(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: set[int] = set()
        self.last_submit: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self):
        self.bot.add_view(ApplyPanel())
        self.bot.add_view(ReviewView())

    async def start_application(self, interaction: discord.Interaction):
        user = interaction.user
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Apasă butonul de pe server.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if user.id in self.active:
            return await interaction.followup.send(
                "Ai deja o aplicație în desfășurare. Verifică mesajele private.", ephemeral=True
            )

        remaining = APPLY_COOLDOWN - (time.time() - self.last_submit.get(user.id, 0))
        if remaining > 0:
            return await interaction.followup.send(
                f"Ai trimis deja o aplicație recent. Poți aplica din nou peste {int(remaining // 60) + 1} minute.",
                ephemeral=True,
            )

        intro = discord.Embed(
            title="Aplicație staff 𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄",
            description=(
                "Vei primi pe rând 15 întrebări. Răspunde la fiecare cu un mesaj aici, în privat.\n\n"
                f"• Un răspuns poate avea maxim **{MAX_ANSWER}** de caractere.\n"
                f"• Ai **{ANSWER_TIMEOUT // 60} minute** pentru fiecare răspuns.\n"
                "• Scrie `anulează` oricând pentru a opri aplicația.\n\n"
                "Răspunde sincer și detaliat. Mult succes!"
            ),
            color=APPLY_COLOR,
        )

        try:
            dm = await user.create_dm()
            await dm.send(embed=intro)
        except discord.Forbidden:
            return await interaction.followup.send(
                "Nu îți pot trimite mesaje private. Activează „Mesaje directe” de la membrii "
                "serverului în setările de confidențialitate, apoi apasă din nou pe buton.",
                ephemeral=True,
            )

        self.active.add(user.id)
        await interaction.followup.send(
            "Ți-am trimis întrebările în privat. Continuă conversația acolo.", ephemeral=True
        )

        task = asyncio.create_task(self.run_application(user, guild, dm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run_application(self, member: discord.Member, guild: discord.Guild, dm: discord.DMChannel):
        answers: list[str] = []

        def check(m: discord.Message) -> bool:
            return m.author.id == member.id and m.channel.id == dm.id

        try:
            for index, question in enumerate(QUESTIONS, 1):
                embed = discord.Embed(
                    title=f"Întrebarea {index}/{len(QUESTIONS)}",
                    description=question,
                    color=APPLY_COLOR,
                )
                embed.set_footer(text="Scrie „anulează” pentru a opri aplicația.")
                await dm.send(embed=embed)

                while True:
                    try:
                        msg = await self.bot.wait_for("message", check=check, timeout=ANSWER_TIMEOUT)
                    except asyncio.TimeoutError:
                        await dm.send(
                            "Sesiunea a expirat din cauza inactivității. "
                            "Apasă din nou pe butonul de aplicare de pe server pentru a relua."
                        )
                        return

                    content = msg.content.strip()
                    if content.lower() in CANCEL_WORDS:
                        await dm.send("Aplicația a fost anulată.")
                        return
                    if not content:
                        await dm.send("Trimite răspunsul sub formă de text.")
                        continue
                    if len(content) > MAX_ANSWER:
                        await dm.send(
                            f"Răspunsul tău are {len(content)} de caractere, iar limita este {MAX_ANSWER}. "
                            "Scurtează-l și trimite-l din nou."
                        )
                        continue

                    answers.append(content)
                    break

            delivered = await self.deliver(guild, member, answers)
            if delivered:
                self.last_submit[member.id] = time.time()
                await dm.send(
                    embed=discord.Embed(
                        title="Aplicație trimisă",
                        description=(
                            "Mulțumim! Aplicația ta a fost trimisă către owner. "
                            "Vei primi un mesaj privat când se ia o decizie."
                        ),
                        color=APPLY_COLOR,
                    )
                )
            else:
                await dm.send(
                    "Nu am putut trimite aplicația către owner. Te rugăm să contactezi un membru al echipei."
                )
        except discord.Forbidden:
            log.warning("Utilizatorul %s a închis mesajele private în timpul aplicației", member.id)
        except Exception:
            log.exception("Eroare în timpul aplicației staff")
        finally:
            self.active.discard(member.id)

    async def deliver(self, guild: discord.Guild, member: discord.Member, answers: list[str]) -> bool:
        cfg = load_apply_config().get(str(guild.id), {})
        owner_id = cfg.get("owner") or guild.owner_id

        header = discord.Embed(
            title="Aplicație staff nouă",
            description=f"{member.mention} a completat aplicația pentru echipa staff.",
            color=APPLY_COLOR,
        )
        header.add_field(name="Utilizator", value=f"{member} (`{member.id}`)", inline=False)
        header.add_field(name="Cont creat", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        if member.joined_at:
            header.add_field(
                name="Pe server din", value=discord.utils.format_dt(member.joined_at, "R"), inline=True
            )
        header.add_field(name="Trimisă", value=discord.utils.format_dt(discord.utils.utcnow(), "F"), inline=True)
        header.set_thumbnail(url=member.display_avatar.url)
        header.set_footer(text=f"ID:{member.id} | G:{guild.id}")

        pages = build_pages(answers)
        page_embeds = []
        for number, fields in enumerate(pages, 1):
            page = discord.Embed(title=f"Răspunsuri ({number}/{len(pages)})", color=APPLY_COLOR)
            for name, value in fields:
                page.add_field(name=name, value=value, inline=False)
            page_embeds.append(page)

        async def send_all(destination):
            await destination.send(embed=header, view=ReviewView())
            for page in page_embeds:
                await destination.send(embed=page)

        try:
            owner = await self.bot.fetch_user(owner_id)
            await send_all(owner)
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Nu am putut trimite aplicația în privat către owner")

        channel_id = cfg.get("channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await send_all(channel)
                return True
            except discord.HTTPException:
                log.exception("Nu am putut trimite aplicația nici în canalul de rezervă")

        return False

    @app_commands.command(name="apply-config", description="Setează cine primește aplicațiile de staff")
    @app_commands.describe(
        owner="Persoana care primește aplicațiile în privat",
        canal="Canal de rezervă, dacă mesajele private ale owner-ului sunt închise (opțional)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def apply_config(
        self,
        interaction: discord.Interaction,
        owner: discord.User,
        canal: Optional[discord.TextChannel] = None,
    ):
        data = load_apply_config()
        cfg = data.setdefault(str(interaction.guild.id), {})
        cfg["owner"] = owner.id
        if canal:
            cfg["channel"] = canal.id
        save_apply_config(data)

        text = f"Aplicațiile vor ajunge în privat la {owner.mention}."
        if canal:
            text += f"\nCanal de rezervă: {canal.mention}"
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="apply-panel", description="Postează panoul de aplicații pentru staff")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def apply_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄  /  APLICAȚIE STAFF",
            description=(
                "Vrei să faci parte din echipa noastră?\n"
                "Apasă pe buton, iar botul îți va trimite întrebările în privat."
            ),
            color=APPLY_COLOR,
        )
        embed.add_field(
            name="Cum funcționează",
            value="Răspunzi la 15 întrebări în mesaje private, iar răspunsurile ajung direct la owner.",
            inline=False,
        )
        embed.add_field(
            name="Înainte să aplici",
            value="Activează mesajele private de la membrii serverului, altfel botul nu îți poate scrie.",
            inline=False,
        )
        embed.set_footer(text="𝐃𝐀𝐑𝐊𝐒𝐈𝐃𝐄  •  RECRUTARE STAFF")

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(embed=embed, view=ApplyPanel())
        except discord.Forbidden:
            return await interaction.followup.send(
                "Nu am putut posta panoul. Dă-mi permisiunile **View Channel**, "
                "**Send Messages** și **Embed Links** în acest canal.",
                ephemeral=True,
            )
        await interaction.followup.send("Panoul de aplicații a fost postat.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Ai nevoie de permisiunea `Manage Server` pentru această comandă."
        else:
            log.error("Eroare în comanda de aplicații", exc_info=original)
            msg = f"Eroare: `{type(original).__name__}: {original}`"

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_ready():
    init_db()
    bot.add_view(VerifyView())
    bot.add_view(TicketView())
    bot.add_view(CloseTicketView())
    if bot.get_cog("StaffApply") is None:
        await bot.add_cog(StaffApply(bot))
    try:
        synced = await bot.tree.sync()
        print(f"Sincronizate {len(synced)} comenzi slash.")
    except Exception as exc:
        print(f"Eroare la sincronizare: {exc}")
    print(f"{bot.user} este online!")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Nu ai permisiunea necesară pentru această comandă."
    else:
        print(f"Eroare comandă: {error}")
        msg = "❌ A apărut o eroare."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------- BUN VENIT ----------------

@bot.event
async def on_member_join(member: discord.Member):
    with db() as con:
        row = con.execute(
            "SELECT channel_id FROM welcome_config WHERE guild_id=?",
            (member.guild.id,),
        ).fetchone()
    if not row:
        return
    channel = member.guild.get_channel(int(row["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return

    created_at = discord.utils.format_dt(member.created_at, style="F")
    member_count = member.guild.member_count

    embed = discord.Embed(
        title="Bun venit pe DARKSIDE !",
        description=(
            f"Salut, {member.mention}!"
            f"Ești al **#{member_count}**-lea membru pe **DARKSIDE**.\n\n"
            "📜 Citește regulamentul și distrează-te alături de comunitate!\n"
            "💬 Dacă ai nevoie de ajutor, echipa de staff este mereu aici pentru tine."
        ),
        color=discord.Color.from_rgb(120, 200, 255),
    )
    embed.add_field(name="👥 Membri", value=f"#{member_count}", inline=True)
    embed.add_field(name="📅 Cont creat", value=created_at, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="DARKSIDE  • Welcome System")

    try:
        await channel.send(content=member.mention, embed=embed)
    except discord.Forbidden:
        pass


@bot.tree.command(name="setup-welcome", description="Configurează canalul de bun venit")
@app_commands.describe(canal="Canalul unde vor apărea mesajele de bun venit")
async def setup_welcome(interaction: discord.Interaction, canal: discord.TextChannel):
    if not await require_admin(interaction):
        return
    with db() as con:
        con.execute(
            """INSERT INTO welcome_config (guild_id, channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id""",
            (interaction.guild.id, canal.id),
        )

    preview = discord.Embed(
        title="❄️ Bun venit pe DARKSIDE !",
        description=(
            "Salut, @membru! 🧊\n"
            "Ești al **#N**-lea membru pe **DARKSIDE**.\n\n"
            "📜 Citește regulamentul și distrează-te alături de comunitate!\n"
            "💬 Dacă ai nevoie de ajutor, echipa de staff este mereu aici pentru tine."
        ),
        color=discord.Color.from_rgb(120, 200, 255),
    )
    preview.add_field(name="👥 Membri", value="#N", inline=True)
    preview.add_field(name="📅 Cont creat", value="dd/mm/yyyy", inline=True)
    preview.set_footer(text="DARKSIDE  • Welcome System")
    await canal.send(embed=preview)

    await interaction.response.send_message(
        f"✅ Canalul de bun venit a fost setat la {canal.mention}. Am trimis și un preview acolo.",
        ephemeral=True,
    )


# ---------------- MODERARE ----------------

@bot.tree.command(name="setup-ticket", description="Configurează și trimite panoul de ticket")
@app_commands.describe(
    rol_suport="Rolul care poate vedea și răspunde la tickete",
    categorie="Categoria în care vor fi create ticketele",
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
        title="🎫 Suport & Asistență",
        description=(
            "Ai nevoie de ajutor sau vrei să discuți cu echipa noastră?\n"
            "Apasă butonul de mai jos pentru a crea un ticket.\n\n"
            "Te rugăm să explici problema cât mai clar și să aștepți răspunsul "
            "unui membru staff. Abuzarea sistemului de ticket poate fi sancționată."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=TicketView())


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
    embed.add_field(name="Reputație", value="/rep add /rep remove /rep check /rep leaderboard", inline=False)
    embed.add_field(name="Distracție", value="/dog /cat /meme", inline=False)
    embed.add_field(name="Numărătoare", value="/setcount /removecount /countset /countstats /countleaderboard", inline=False)
    embed.add_field(name="Verificare", value="/setup-verificare", inline=False)
    embed.add_field(name="Bun venit", value="/setup-welcome", inline=False)
    embed.add_field(name="Sugestii", value="/suggest /setup-suggestii", inline=False)
    embed.add_field(name="Aplicații staff", value="/apply-config /apply-panel", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


# ---------------- NUMĂRĂTOARE ----------------

COUNT_CONFIG_FILE = "counting_config.json"

# Aceeași persoană poate număra de două ori la rând? (False = nu)
PERMITE_ACELASI_USER = False
# Șterge mesajele din canal care nu sunt numere? (False = se poate vorbi liber)
STERGE_MESAJE_NON_NUMERE = True

NUMBER_RE = re.compile(r"^\d+$")


def load_count_config() -> dict:
    if not os.path.exists(COUNT_CONFIG_FILE):
        return {}
    try:
        with open(COUNT_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_count_config() -> None:
    with open(COUNT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(count_config, f, indent=4)


count_config: dict = load_count_config()
count_locks: dict[int, asyncio.Lock] = {}


def count_lock(guild_id: int) -> asyncio.Lock:
    return count_locks.setdefault(guild_id, asyncio.Lock())


def count_get(guild_id: int) -> Optional[dict]:
    return count_config.get(str(guild_id))


def count_new_entry(channel_id: int) -> dict:
    return {
        "channel": channel_id,
        "current": 0,
        "last_user": None,
        "last_message": None,
        "highscore": 0,
        "total_corecte": 0,
        "total_gresite": 0,
        "users": {},  # user_id -> numere corecte
    }


@bot.tree.command(name="setcount", description="Setează canalul unde se numără")
@app_commands.describe(canal="Canalul de numărat")
@app_commands.checks.has_permissions(manage_guild=True)
async def setcount(interaction: discord.Interaction, canal: discord.TextChannel):
    existent = count_get(interaction.guild_id)
    if existent:
        existent["channel"] = canal.id
    else:
        count_config[str(interaction.guild_id)] = count_new_entry(canal.id)
    save_count_config()
    await interaction.response.send_message(
        f"✅ Canalul de numărat este acum {canal.mention}. Începeți de la **1**!",
        ephemeral=True,
    )
    try:
        await canal.send("🔢 **Numărătoarea a început!** Primul număr este **1**.")
    except discord.HTTPException:
        pass


@bot.tree.command(name="removecount", description="Oprește sistemul de numărat")
@app_commands.checks.has_permissions(manage_guild=True)
async def removecount(interaction: discord.Interaction):
    count_config.pop(str(interaction.guild_id), None)
    save_count_config()
    await interaction.response.send_message("🗑️ Sistemul de numărat a fost oprit.", ephemeral=True)


@bot.tree.command(name="countset", description="Setează manual numărul curent")
@app_commands.describe(numar="Numărul de la care se continuă (ultimul număr valid)")
@app_commands.checks.has_permissions(manage_guild=True)
async def countset(interaction: discord.Interaction, numar: app_commands.Range[int, 0]):
    data = count_get(interaction.guild_id)
    if not data:
        return await interaction.response.send_message(
            "❌ Nu e setat niciun canal. Folosește `/setcount`.", ephemeral=True
        )
    data["current"] = numar
    data["last_user"] = None
    data["last_message"] = None
    save_count_config()
    await interaction.response.send_message(
        f"✅ Numărul curent este **{numar}**. Următorul: **{numar + 1}**.", ephemeral=True
    )


@bot.tree.command(name="countstats", description="Statistici despre numărătoare")
async def countstats(interaction: discord.Interaction):
    data = count_get(interaction.guild_id)
    if not data:
        return await interaction.response.send_message(
            "❌ Sistemul de numărat nu e activ aici.", ephemeral=True
        )
    embed = discord.Embed(title="🔢 Statistici numărătoare", color=discord.Color.blurple())
    embed.add_field(name="Canal", value=f"<#{data['channel']}>", inline=True)
    embed.add_field(name="Număr curent", value=str(data["current"]), inline=True)
    embed.add_field(name="Următorul", value=str(data["current"] + 1), inline=True)
    embed.add_field(name="🏆 Record", value=str(data["highscore"]), inline=True)
    embed.add_field(name="✅ Corecte", value=str(data["total_corecte"]), inline=True)
    embed.add_field(name="❌ Greșeli", value=str(data["total_gresite"]), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="countleaderboard", description="Top 10 numărători")
async def countleaderboard(interaction: discord.Interaction):
    data = count_get(interaction.guild_id)
    if not data or not data["users"]:
        return await interaction.response.send_message(
            "Încă nu a numărat nimeni.", ephemeral=True
        )
    top = sorted(data["users"].items(), key=lambda x: x[1], reverse=True)[:10]
    medalii = ["🥇", "🥈", "🥉"]
    linii = []
    for i, (uid, nr) in enumerate(top):
        prefix = medalii[i] if i < 3 else f"**{i + 1}.**"
        linii.append(f"{prefix} <@{uid}> — **{nr}** numere")
    embed = discord.Embed(
        title="🏅 Top numărători", description="\n".join(linii), color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


@bot.listen("on_message")
async def counting_on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    data = count_get(message.guild.id)
    if not data or message.channel.id != data["channel"]:
        return

    content = message.content.strip()
    if not NUMBER_RE.match(content):
        if STERGE_MESAJE_NON_NUMERE:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        return  # mesaj normal, ignorat

    async with count_lock(message.guild.id):
        numar = int(content)
        asteptat = data["current"] + 1
        uid = message.author.id

        # aceeași persoană de două ori la rând
        if not PERMITE_ACELASI_USER and data["last_user"] == uid:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await message.channel.send(
                f"⚠️ {message.author.mention}, nu poți număra de două ori la rând!",
                delete_after=5,
            )
            return

        # număr greșit
        if numar != asteptat:
            data["total_gresite"] += 1
            ruinat_la = data["current"]
            data["current"] = 0
            data["last_user"] = None
            data["last_message"] = None
            save_count_config()
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
            await message.channel.send(
                f"❌ {message.author.mention} a stricat numărătoarea la **{ruinat_la}**! "
                f"Era rândul lui **{asteptat}**.\n"
                f"Recordul serverului: **{data['highscore']}**. Reîncepem de la **1**."
            )
            return

        # număr corect
        data["current"] = numar
        data["last_user"] = uid
        data["last_message"] = message.id
        data["total_corecte"] += 1
        data["users"][str(uid)] = data["users"].get(str(uid), 0) + 1
        record_nou = numar > data["highscore"]
        if record_nou:
            data["highscore"] = numar
        save_count_config()

        try:
            await message.add_reaction("🏆" if record_nou and numar > 1 else "✅")
            if numar % 100 == 0:
                await message.add_reaction("💯")
                await message.channel.send(f"🎉 Felicitări! Ați ajuns la **{numar}**!")
        except discord.HTTPException:
            pass


# Anti-trișat: mesaj de număr șters / editat
@bot.listen("on_message_delete")
async def counting_on_message_delete(message: discord.Message):
    if message.guild is None:
        return
    data = count_get(message.guild.id)
    if not data or message.channel.id != data["channel"]:
        return
    if data["last_message"] == message.id:
        await message.channel.send(
            f"🗑️ {message.author.mention} a șters ultimul număr (**{data['current']}**). "
            f"Următorul număr este **{data['current'] + 1}**."
        )


@bot.listen("on_message_edit")
async def counting_on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild is None:
        return
    data = count_get(before.guild.id)
    if not data or before.channel.id != data["channel"]:
        return
    if data["last_message"] == before.id and before.content != after.content:
        await after.channel.send(
            f"✏️ {after.author.mention} a editat ultimul număr! "
            f"Numărul corect era **{data['current']}**, următorul este **{data['current'] + 1}**."
        )


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


# ---------------- SUGESTII ----------------

@bot.tree.command(name="setup-suggestii", description="Configurează canalul de sugestii")
@app_commands.describe(canal="Canalul unde vor apărea sugestiile")
async def setup_suggestii(interaction: discord.Interaction, canal: discord.TextChannel):
    if not await require_admin(interaction):
        return
    with db() as con:
        con.execute(
            """INSERT INTO suggestion_config (guild_id, channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id""",
            (interaction.guild.id, canal.id),
        )
    await interaction.response.send_message(
        f"✅ Canalul de sugestii a fost setat la {canal.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="suggest", description="Trimite o sugestie pentru server")
@app_commands.describe(sugestie="Textul sugestiei tale")
async def suggest(interaction: discord.Interaction, sugestie: str):
    if interaction.guild is None:
        return
    with db() as con:
        row = con.execute(
            "SELECT channel_id FROM suggestion_config WHERE guild_id=?",
            (interaction.guild.id,),
        ).fetchone()
    if not row:
        await interaction.response.send_message(
            "❌ Sistemul de sugestii nu este configurat. Un admin trebuie să folosească /setup-suggestii.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(int(row["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Canalul de sugestii nu mai există.", ephemeral=True)
        return

    embed = discord.Embed(
        title="💡 Sugestie nouă",
        description=sugestie,
        color=discord.Color.blurple(),
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"ID membru: {interaction.user.id}")

    try:
        message = await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Botul nu poate trimite mesaje în canalul de sugestii.", ephemeral=True
        )
        return

    await message.add_reaction("👍")
    await message.add_reaction("👎")

    await interaction.response.send_message(
        f"✅ Sugestia ta a fost trimisă în {channel.mention}!",
        ephemeral=True,
    )


bot.run(TOKEN)
