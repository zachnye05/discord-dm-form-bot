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
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))  # NEW

# sheet/tab names
TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"  # optional tab to store responses

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# cache for external messages
MESSAGE_CACHE = {}

# embed color (hex) -> int
EMBED_COLOR = 0x963BF3  # #963bf3

# message to send after form is filled
CLAIM_MESSAGE = (
    "Thanks for submitting your info.\n\n"
    "To claim your free week of Divine, click the link below👇\n\n"
    "https://whop.com/checkout/plan_XiUlT3C057H67?d2c=true"
)


# ------------------------------------------------
# LOGGING HELPER
# ------------------------------------------------
async def log_to_channel(text: str):
    """Send a log message to the Discord channel if LOG_CHANNEL_ID is set and the channel is reachable."""
    if not LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        # try fetching
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(text[:2000])  # discord msg limit
    except Exception:
        pass


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
    """Load messages from 'messages' sheet into cache."""
    global MESSAGE_CACHE
    ws = get_ws(MESSAGES_WS_NAME)
    rows = ws.get_all_records()
    cache = {}
    for r in rows:
        k = r.get("key")
        c = r.get("content", "")
        if k:
            # let people type "\n" in Sheets and have it become a real newline
            c = c.replace("\\n", "\n")
            cache[k] = c
    MESSAGE_CACHE = cache
    return cache


def get_message(key: str, default: str = "") -> str:
    return MESSAGE_CACHE.get(key, default)


def append_response(user_id: int, username: str, payload: dict):
    """Append form submission to responses sheet (if it exists)."""
    try:
        ws = get_ws(RESPONSES_WS_NAME)
    except Exception:
        return  # sheet optional
    ws.append_row([
        iso_now(),
        str(user_id),
        username,
        payload.get("first_name", ""),
        payload.get("email", ""),
        payload.get("phone", ""),
    ])


# ------------------------------------------------
# DISCORD UI: MODAL + BUTTON
# ------------------------------------------------
class InfoForm(discord.ui.Modal, title="Claim your free week"):
    def __init__(self, user_id: int, username: str):
        super().__init__()
        self.user_id = user_id
        self.username = username

        self.first_name = discord.ui.TextInput(
            label="First name",
            style=discord.TextStyle.short,
            required=True,
        )
        self.email = discord.ui.TextInput(
            label="Email",
            style=discord.TextStyle.short,
            required=True,
        )
        self.phone = discord.ui.TextInput(
            label="Phone number",
            style=discord.TextStyle.short,
            required=True,
        )

        self.add_item(self.first_name)
        self.add_item(self.email)
        self.add_item(self.phone)

    async def on_submit(self, interaction: discord.Interaction):
        # log to responses sheet
        append_response(
            self.user_id,
            self.username,
            {
                "first_name": str(self.first_name),
                "email": str(self.email),
                "phone": str(self.phone),
            },
        )

        # update targets sheet: completed + form_submitted
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

        # log it
        await log_to_channel(
            f"✅ Form submitted by `{self.username}` (ID: {self.user_id})"
        )

        # first reply in the modal
        await interaction.response.send_message(
            "✅ Thanks — your info was submitted.",
            ephemeral=True
        )

        # then send the claim message right away
        try:
            await interaction.user.send(CLAIM_MESSAGE)
        except Exception as e:
            print("Error sending claim message to user:", e)


class FormView(discord.ui.View):
    """
    This view goes on ALL 3 DMs.
    We pass the actual discord.User so the modal can capture name + id.
    """
    def __init__(self, user: discord.User, timeout=None):
        super().__init__(timeout=timeout)
        self.user = user

    @discord.ui.button(
        label="Claim Your Free Week",
        style=discord.ButtonStyle.primary  # Discord purple/blurple
    )
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = InfoForm(user_id=self.user.id, username=str(self.user))
        await interaction.response.send_modal(modal)


# ------------------------------------------------
# DM SENDING HELPERS (embed + purple button)
# ------------------------------------------------
async def send_embed_dm(user: discord.User, message_key: str, fallback: str):
    """Send an embed DM with our purple color and the claim button."""
    text = get_message(message_key, fallback)
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    await user.send(embed=embed, view=FormView(user))


async def send_initial_dm(user: discord.User):
    await send_embed_dm(
        user,
        "initial_dm",
        "Hey! Tap the button below to claim your free week."
    )
    await log_to_channel(f"📤 Sent initial DM to `{user}` (ID: {user.id})")


async def send_24h_dm(user: discord.User):
    await send_embed_dm(
        user,
        "followup_24h",
        "Just following up on that free week — tap below to claim."
    )
    await log_to_channel(f"🔁 Sent 24h follow-up to `{user}` (ID: {user.id})")


async def send_72h_dm(user: discord.User):
    await send_embed_dm(
        user,
        "followup_72h",
        "Final reminder to claim your free week — tap below."
    )
    await log_to_channel(f"🔁 Sent 72h follow-up to `{user}` (ID: {user.id})")


# ------------------------------------------------
# BOT EVENTS
# ------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # load messages once at startup
    try:
        load_messages()
        print("✅ Loaded messages from sheet.")
    except Exception as e:
        print("⚠️ Could not load messages on startup:", e)

    # sync slash commands
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("⚠️ Error syncing commands:", e)

    await log_to_channel("🟣 Divine DM bot is online.")
    followup_checker.start()


# ------------------------------------------------
# /blast COMMAND
# ------------------------------------------------
@bot.tree.command(name="blast", description="DM all targets from the sheet")
@app_commands.checks.has_permissions(administrator=True)
async def blast(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You can't run this.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Sending DMs from sheet… (15s delay per DM to stay safe)",
        ephemeral=True
    )

    targets, ws = load_targets()
    sent_count = 0

    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        form_submitted = str(row.get("form_submitted", "")).strip().upper()

        # skip if blank or already done
        if not user_id:
            continue
        if status in ("sent", "completed") or form_submitted == "TRUE":
            continue

        try:
            user = await bot.fetch_user(int(user_id))
            await send_initial_dm(user)
            update_target_row(ws, idx, {
                "status": "sent",
                "sent_at": iso_now(),
                "dm_error": "",
            })
            sent_count += 1
        except Exception as e:
            print(f"Error DMing {user_id}: {e}")
            update_target_row(ws, idx, {
                "dm_error": str(e)
            })
            await log_to_channel(f"⚠️ Failed to DM ID {user_id}: `{e}`")

        # ✅ very safe spacing for initial sends
        await asyncio.sleep(15)

    await interaction.followup.send(f"Done. Sent {sent_count} DMs.", ephemeral=True)
    await log_to_channel(f"✅ /blast complete. Sent {sent_count} DMs.")


# ------------------------------------------------
# FOLLOW-UP LOOP (24h + 72h)
# ------------------------------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    # refresh messages in case you edited the sheet
    try:
        load_messages()
    except Exception as e:
        print("⚠️ Could not reload messages:", e)

    try:
        targets, ws = load_targets()
    except Exception as e:
        print("⚠️ Error loading targets in loop:", e)
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

        # stop followups if user already submitted or is completed
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

        # 24h followup
        if delta >= datetime.timedelta(hours=24) and not reminder_sent:
            try:
                user = await bot.fetch_user(int(user_id))
                await send_24h_dm(user)
                update_target_row(ws, idx, {
                    "reminder_sent": iso_now()
                })
                await asyncio.sleep(5)  # lighter delay for followups
            except Exception as e:
                print(f"Error sending 24h followup to {user_id}: {e}")
                await log_to_channel(f"⚠️ Failed 24h follow-up to {user_id}: `{e}`")

        # 72h followup
        if delta >= datetime.timedelta(hours=72) and not second_reminder_sent:
            try:
                user = await bot.fetch_user(int(user_id))
                await send_72h_dm(user)
                update_target_row(ws, idx, {
                    "second_reminder_sent": iso_now()
                })
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error sending 72h followup to {user_id}: {e}")
                await log_to_channel(f"⚠️ Failed 72h follow-up to {user_id}: `{e}`")


# ------------------------------------------------
# RUN BOT
# ------------------------------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(DISCORD_TOKEN)
