import discord
from discord.ext import commands
import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

print("Starting LinCon...")

# ---- GOOGLE CREDS LOAD ----
raw_creds = os.getenv("GOOGLE_CREDS")

if not raw_creds:
    print("GOOGLE_CREDS ENV VAR NOT FOUND")

try:
    creds_dict = json.loads(raw_creds)
    print("Google creds JSON loaded")
except Exception as e:
    print("FAILED TO LOAD GOOGLE CREDS:", e)
    raise e

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

try:
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    print("Google Sheets authorized")
except Exception as e:
    print("FAILED TO AUTHORIZE GOOGLE SHEETS:", e)
    raise e

try:
    sheet = client.open("LinCon_Brain").sheet1
    print("Google Sheet opened successfully")
except Exception as e:
    print("FAILED TO OPEN SHEET:", e)
    raise e

# ---- DISCORD EVENTS ----
@bot.event
async def on_ready():
    print(f"LinCon online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        print("DM received:", message.content)

        try:
            sheet.append_row([
                datetime.utcnow().isoformat(),
                "Discord DM",
                message.content,
                "raw"
            ])
            print("Row successfully added")
        except Exception as e:
            print("FAILED TO APPEND ROW:", e)

        await message.channel.send(
            "Saved. I’ll think with this later."
        )

    await bot.process_commands(message)

bot.run(os.getenv("DISCORD_TOKEN"))
