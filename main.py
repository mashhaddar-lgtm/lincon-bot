import discord
from discord.ext import commands
import os

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"LinCon is online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        await message.channel.send(
            "Noted. I’ll use this for your LinkedIn content."
        )

    await bot.process_commands(message)

bot.run(os.getenv("DISCORD_TOKEN"))
