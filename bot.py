import os
import json
import asyncio
import datetime
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands, tasks

import gspread

# ----------------------------
# ENV VARS
# ----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # your server, for slash command sync
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # only you can run /blast
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Comma-separated list of discord IDs to DM, e.g. "123,456,789"
TARGET_IDS_RAW = os.getenv("TARGET_IDS", "")
TARGET_IDS = [int(x.strip()) for x in TARGET_IDS_RAW.split(",") if x.strip()]

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

DB_PATH = "bot.db"


# ----------------------------
# DB helpers
# ----------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS dm_targets (
            user_id INTEGER PRIMARY KEY,
            first_dm_at TEXT,
            responded INTEGER DEFAULT 0,
            followup_sent INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def add_or_update_target(user_id: int, first_dm_at: datetime.datetime):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT OR IGNORE INTO dm_targets (user_id, first_dm_at, responded, followup_sent)
        VALUES (?, ?, 0, 0)
        """,
        (user_id, first_dm_at.isoformat()),
    )
    conn.commit()
    conn.close()


def mark_responded(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE dm_targets SET responded = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def mark_followup_sent(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE dm_targets SET followup_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_pending_followups():
    """Return list of user_ids who need follow-up: 24h passed, not responded, not followed up."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, first_dm_at FROM dm_targets WHERE responded = 0 AND followup_sent = 0")
    rows = c.fetchall()
    conn.close()

    now = datetime.datetime.utcnow()
    to_follow = []
    for user_id, first_dm_at in rows:
        sent_time = datetime.datetime.fromisoformat(first_dm_at)
        if now - sent_time >= datetime.timedelta(hours=24):
            to_follow.append(user_id)
    return to_follow


# ----------------------------
# Google Sheets setup
# ----------------------------
def get_sheet():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    gc = gspread.service_account_from_dict(sa_info)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.sheet1  # first sheet


def append_submission_to_sheet(user_id: int, q1: str, q2: str):
    sheet = get_sheet()
    sheet.append_row(
        [
            datetime.datetime.utcnow().isoformat(),
            str(user_id),
            q1,
            q2,
        ]
    )


# ----------------------------
# Discord UI: Button + Modal
# ----------------------------
class InfoForm(discord.ui.Modal, title="Fill out the info"):
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

        self.reason = discord.ui.TextInput(
            label="Why are you filling this out?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.contact = discord.ui.TextInput(
            label="Best contact (email/discord/etc.)",
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
        )

        self.add_item(self.reason)
        self.add_item(self.contact)

    async def on_submit(self, interaction: discord.Interaction):
        # Log to Google Sheet
        try:
            append_submission_to_sheet(self.user_id, str(self.reason), str(self.contact))
        except Exception as e:
            print("Error saving to Google Sheets:", e)

        # Mark responded
        mark_responded(self.user_id)

        await interaction.response.send_message(
            "Got it — thanks for filling that out ✅", ephemeral=True
        )


class FormView(discord.ui.View):
    def __init__(self, user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    @discord.ui.button(label="Open form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = InfoForm(user_id=self.user_id)
        await interaction.response.send_modal(modal)


# ----------------------------
# Bot events
# ----------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    try:
        # sync commands to your guild for fast updates
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
            print("Slash commands synced to guild.")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally.")
    except Exception as e:
        print("Error syncing commands:", e)

    followup_checker.start()


# ----------------------------
# Slash command to send the DMs
# ----------------------------
@bot.tree.command(name="blast", description="DM all target IDs the form")
@app_commands.checks.has_permissions(administrator=True)
async def blast(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You can't run this.", ephemeral=True)
        return

    await interaction.response.send_message(f"Sending DMs to {len(TARGET_IDS)} users...", ephemeral=True)

    for user_id in TARGET_IDS:
        user = await bot.fetch_user(user_id)
        if user is None:
            continue
        try:
            await user.send(
                embed=discord.Embed(
                    title="Quick form for you",
                    description="Hit the button below and fill it out — takes 30 seconds.",
                    color=0xA9C8DC,
                ),
                view=FormView(user_id=user_id),
            )
            add_or_update_target(user_id, datetime.datetime.utcnow())
            await asyncio.sleep(1)  # tiny delay
        except Exception as e:
            print(f"Error DMing {user_id}: {e}")

    await interaction.followup.send("DMs sent (check logs for any failures).", ephemeral=True)


# ----------------------------
# Background task for 24h follow-ups
# ----------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    ids_to_follow = get_pending_followups()
    if not ids_to_follow:
        return

    for user_id in ids_to_follow:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(
                "Hey! Just following up on that form I sent yesterday — can you fill it out when you get a sec? 👍",
                view=FormView(user_id=user_id),
            )
            mark_followup_sent(user_id)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Error sending follow-up to {user_id}: {e}")


# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(DISCORD_TOKEN)
