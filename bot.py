
import discord
from discord.ext import commands
import random

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"{bot.user} este online!")

@bot.command()
async def giveaway(ctx, *, premiu):
    count = 0
    link_server = "https://discord.gg/KhUA2rjXKX"  # link-ul serverului tău
    membri_trimis = []

    # Trimite DM la toți membrii care nu sunt bot
    for membru in ctx.guild.members:
        try:
            if not membru.bot:
                await membru.send(f"🎉 Giveaway! Ai șansa să câștigi: **{premiu}**!\nIntră aici pentru mai multe info: {link_server}")
                count += 1
                membri_trimis.append(membru)
        except:
            pass

    await ctx.send(f"Giveaway trimis în privat la {count} membri!")

    # Alege câștigător aleatoriu
    if membri_trimis:
        castigator = random.choice(membri_trimis)
        await ctx.send(f"🏆 Câștigătorul giveaway-ului **{premiu}** este: {castigator.mention} 🎉")

import os

bot.run(os.getenv("DISCORD_TOKEN"))
