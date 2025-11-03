import os
import json
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import gspread

# ------------------------------------------------
# ENVIRONMENT VARIABLES
# ------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"  # optional logging tab

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

MESSAGE_CACHE = {}  # refreshed every loop


# ------------------------------------------------
# GOOGLE SHEETS HELPERS
# ------------------------------------------------
def get_client():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    gc = gspread.service_account_from_dict(sa_info)
    return gc


def get_ws(name: str):
    gc = get_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(name)


def iso_now():
    return datetime.datetime.utcnow().isoformat()


def parse_iso(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)


def load_targets():
    ws = get_ws(TARGETS_WS_NAME)
    records = ws.get_all_records()
    return records, ws


def update_target_row(ws, row_index: int, updates: dict):
    headers = ws.row_values(1)
    batch = []
    for key, value in updates.items():
        if key in headers:
            col = headers.index(key) + 1
            a1 = gspread.utils.rowcol_to_a1(row_index, col)
            batch.append({"range": a1, "values": [[value]]})
    if batch:
        ws.batch_update(batch)


def load_messages():
    global MESSAGE_CACHE
    ws = get_ws(MESSAGES_WS_NAME)
    rows = ws.get_all_records()
    cache = {}
    for r in rows:
        k = r.get("key")
        c = r.get("content", "")
        if k:
            cache[k] = c
    MESSAGE_CACHE = cache
    return cache


def get_message(key: str, default: str = "") -> str:
    return MESSAGE_CACHE.get(key, default)


def append_response(user_id: int, payload: dict):
    # responses sheet is optional
    try:
        ws = get_ws(RESPONSES_WS_NAME)
    except Exception:
        return
    ws.append_row([
        iso_now(),
        str(user_id),
        payload.get("reason", ""),
        payload.get("contact", "")
    ])


# ------------------------------------------------
# DISCORD UI: FORM MODAL
# ------------------------------------------------
class InfoForm(discord.ui.Modal, title="Fill out the info"):
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

        self.reason = discord.ui.TextInput(
            label="Why are you filling this out?",
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.contact = discord.ui.TextInput(
            label="Best contact (email/discord/etc.)",
            style=discord.TextStyle.short,
            required=True,
        )

        self.add_item(self.reason)
        self.add_item(self.contact)

    async def on_submit(self, interaction: discord.Interaction):
        # log to responses sheet
        append_response(self.user_id, {
            "reason": str(self.reason),
            "contact": str(self.contact),
        })

        # mark user as completed
        try:
            targets, ws = load_targets()
            for idx, row in enumerate(targets, start=2):
                if str(row.get("user_id", "")).strip() == str(self.user_id).strip():
                    update_target_row(ws, idx, {
                        "status": "completed",
                        "completed_at": iso_now(),
                        "form_submitted": "TRUE",
                    })
                    break
        except Exception as e:
            print("Error updating completed row:", e)

        await interaction.response.send_message("Got it — thanks for filling that out ✅", ephemeral=True)


class FormView(discord.ui.View):
    def __init__(self, user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    @discord.ui.button(label="Open form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = InfoForm(user_id=self.user_id)
        await interaction.response.send_modal(modal)


# ------------------------------------------------
# DM SENDING HELPERS
# ------------------------------------------------
async def send_initial_dm(user: discord.User, user_id: int):
    msg = get_message("initial_dm", "Hey! Please fill out this quick form.")
    await user.send(msg, view=FormView(user_id=user_id))


async def send_24h_dm(user: discord.User, user_id: int):
    msg = get_message("followup_24h", "Just following up on that form I sent yesterday 👍")
    await user.send(msg, view=FormView(user_id=user_id))


async def send_72h_dm(user: discord.User, user_id: int):
    msg = get_message("followup_72h", "Last nudge on that form — would love to get it from you 🙏")
    await user.send(msg, view=FormView(user_id=user_id))


# ------------------------------------------------
# BOT EVENTS
# ------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # Load messages initially
    try:
        load_messages()
        print("✅ Loaded messages from sheet.")
    except Exception as e:
        print("⚠️ Could not load messages:", e)

    # Sync commands
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("⚠️ Error syncing commands:", e)

    followup_checker.start()


# ------------------------------------------------
# /BLAST COMMAND
# ------------------------------------------------
@bot.tree.command(name="blast", description="DM all targets from the sheet")
@app_commands.checks.has_permissions(administrator=True)
async def blast(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You can't run this.", ephemeral=True)
        return

    await interaction.response.send_message("Sending DMs from sheet… (15 s delay per DM for safety)", ephemeral=True)

    targets, ws = load_targets()
    count = 0
    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        form_submitted = str(row.get("form_submitted", "")).strip().upper()

        if not user_id:
            continue
        if status in ("sent", "completed") or form_submitted == "TRUE":
            continue

        try:
            user = await bot.fetch_user(int(user_id))
            await send_initial_dm(user, int(user_id))
            update_target_row(ws, idx, {
                "status": "sent",
                "sent_at": iso_now(),
                "dm_error": "",
            })
            count += 1
        except Exception as e:
            print(f"Error DMing {user_id}: {e}")
            update_target_row(ws, idx, {"dm_error": str(e)})

        # ✅ Slow delay between each initial DM
        await asyncio.sleep(15)

    await interaction.followup.send(f"Done. Sent {count} DMs.", ephemeral=True)


# ------------------------------------------------
# FOLLOW-UP LOOP (24 H + 72 H)
# ------------------------------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    # Refresh messages each run
    try:
        load_messages()
    except Exception as e:
        print("⚠️ Could not reload messages:", e)

    try:
        targets, ws = load_targets()
    except Exception as e:
        print("⚠️ Error loading targets:", e)
        return

    now = datetime.datetime.utcnow()

    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        sent_at = row.get("sent_at", "")
        reminder_sent = row.get("reminder_sent", "")
        second_reminder_sent = row.get("second_reminder_sent", "")
        completed_at = row.get("completed_at", "")
        form_submitted = str(row.get("form_submitted", "")).strip().upper()

        # ✅ Cancel followups for completed / submitted users
        if not user_id:
            continue
        if status == "completed" or completed_at or form_submitted == "TRUE":
            continue
        if not sent_at:
            continue

        try:
            sent_time = parse_iso(sent_at)
        except Exception:
            continue

        delta = now - sent_time

        # 24 h follow-up
        if delta >= datetime.timedelta(hours=24) and not reminder_sent:
            try:
                user = await bot.fetch_user(int(user_id))
                await send_24h_dm(user, int(user_id))
                update_target_row(ws, idx, {"reminder_sent": iso_now()})
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error sending 24h follow-up to {user_id}: {e}")

        # 72 h follow-up
        if delta >= datetime.timedelta(hours=72) and not second_reminder_sent:
            try:
                user = await bot.fetch_user(int(user_id))
                await send_72h_dm(user, int(user_id))
                update_target_row(ws, idx, {"second_reminder_sent": iso_now()})
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error sending 72h follow-up to {user_id}: {e}")


# ------------------------------------------------
# RUN BOT
# ------------------------------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(DISCORD_TOKEN)
