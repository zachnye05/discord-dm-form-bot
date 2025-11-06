import os
import json
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
import gspread

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"

EMBED_COLOR = 0x963BF3
MESSAGE_CACHE = {}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# -----------------------------
# LOGGING
# -----------------------------
async def log_to_channel(text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(text[:2000])
    except Exception:
        pass

# -----------------------------
# GOOGLE SHEETS
# -----------------------------
def get_client():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return gspread.service_account_from_dict(sa_info)

def get_ws(name: str):
    gc = get_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(name)

def iso_now():
    return datetime.datetime.utcnow().isoformat()

def parse_iso(s: str):
    return datetime.datetime.fromisoformat(s)

# Forgiving loader — reads A=key, B=content
def load_messages():
    global MESSAGE_CACHE
    ws = get_ws(MESSAGES_WS_NAME)
    keys = ws.col_values(1)
    contents = ws.col_values(2)
    cache = {}
    for i in range(1, len(keys)):
        k = (keys[i] or "").strip()
        if not k:
            continue
        c = contents[i] if i < len(contents) else ""
        c = (c or "").replace("\\n", "\n")
        cache[k] = c
    MESSAGE_CACHE = cache
    return cache

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

def append_response(user_id, username, payload):
    try:
        ws = get_ws(RESPONSES_WS_NAME)
        ws.append_row([
            iso_now(),
            str(user_id),
            username,
            payload.get("first_name", ""),
            payload.get("email", ""),
            payload.get("phone", "")
        ])
    except Exception as e:
        print("Error appending response:", e)

# -----------------------------
# DISCORD UI
# -----------------------------
class InfoForm(discord.ui.Modal, title="Claim your free week"):
    def __init__(self, user_id, username):
        super().__init__()
        self.user_id = user_id
        self.username = username
        self.first_name = discord.ui.TextInput(label="First name", required=True)
        self.email = discord.ui.TextInput(label="Email", required=True)
        self.phone = discord.ui.TextInput(label="Phone number", required=True)
        self.add_item(self.first_name)
        self.add_item(self.email)
        self.add_item(self.phone)

    async def on_submit(self, interaction: discord.Interaction):
        append_response(self.user_id, self.username, {
            "first_name": str(self.first_name),
            "email": str(self.email),
            "phone": str(self.phone),
        })
        await log_to_channel(f"✅ Form submitted by {self.username} ({self.user_id})")
        await interaction.response.send_message("✅ Thanks — your info was submitted.", ephemeral=True)
        await interaction.user.send(
            "Thanks for submitting your info.\n\n"
            "To claim your free week of Divine, click the link below👇\n\n"
            "https://whop.com/checkout/plan_XiUlT3C057H67?d2c=true"
        )

class FormView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user

    @discord.ui.button(label="Claim Your Free Week", style=discord.ButtonStyle.primary)
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InfoForm(self.user.id, str(self.user)))

# -----------------------------
# DM HELPERS
# -----------------------------
def get_message(key, default=""):
    return MESSAGE_CACHE.get(key, default)

async def send_embed_dm(user, key, fallback):
    text = get_message(key, fallback)
    text = text.replace("<@user>", user.mention)
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    await user.send(embed=embed, view=FormView(user))

async def send_initial_dm(user):
    await send_embed_dm(user, "initial_dm", "Hey! Tap below to claim your free week.")
    await log_to_channel(f"📤 Sent initial DM to {user} ({user.id})")

async def send_24h_dm(user):
    await send_embed_dm(user, "followup_24h", "Reminder — claim your free week soon.")
    await log_to_channel(f"🔁 Sent 24h follow-up to {user} ({user.id})")

async def send_72h_dm(user):
    await send_embed_dm(user, "followup_72h", "**IMPORTANT NOTICE** — last chance.")
    await log_to_channel(f"🔁 Sent 72h follow-up to {user} ({user.id})")

# -----------------------------
# EVENTS
# -----------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        loaded = load_messages()
        await log_to_channel(f"🗂 Loaded message keys: {list(loaded.keys())}")
    except Exception as e:
        await log_to_channel(f"⚠️ Could not load messages on startup: {repr(e)}")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Error syncing:", e)
    await log_to_channel("🟣 Bot online and ready.")
    followup_checker.start()

# -----------------------------
# COMMANDS
# -----------------------------
@bot.tree.command(name="blast", description="DM all users from the targets sheet.")
@app_commands.checks.has_permissions(administrator=True)
async def blast(interaction: discord.Interaction):
    if LOG_CHANNEL_ID and interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message("Please run this command in the log channel.", ephemeral=True)
        return
    await interaction.response.send_message("Sending DMs (15s delay per user)...", ephemeral=True)
    targets, ws = load_targets()
    count = 0
    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        status = str(row.get("status", "")).lower()
        if not user_id or status in ("sent", "completed"):
            continue
        try:
            user = await bot.fetch_user(int(user_id))
            await send_initial_dm(user)
            update_target_row(ws, idx, {"status": "sent", "sent_at": iso_now()})
            count += 1
        except Exception as e:
            update_target_row(ws, idx, {"dm_error": str(e)})
            await log_to_channel(f"⚠️ Failed to DM {user_id}: {e}")
        await asyncio.sleep(15)
    await log_to_channel(f"✅ /blast complete — {count} messages sent.")

@bot.tree.command(name="test", description="Send all 3 DMs to yourself (for testing).")
async def test_command(interaction: discord.Interaction):
    if LOG_CHANNEL_ID and interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message("Please run this command in the log channel.", ephemeral=True)
        return
    await interaction.response.send_message("Sending test messages...", ephemeral=True)
    user = interaction.user
    await send_initial_dm(user)
    await asyncio.sleep(1)
    await send_24h_dm(user)
    await asyncio.sleep(1)
    await send_72h_dm(user)
    await log_to_channel(f"🧪 Test DMs sent to {user} ({user.id})")

# -----------------------------
# FOLLOWUP LOOP
# -----------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    try:
        load_messages()
    except Exception as e:
        print("reload error", e)
    try:
        targets, ws = load_targets()
    except Exception as e:
        print("target load error", e)
        return
    now = datetime.datetime.utcnow()
    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        if not user_id:
            continue
        status = str(row.get("status", "")).lower()
        form_submitted = str(row.get("form_submitted", "")).upper()
        if status == "completed" or form_submitted == "TRUE":
            continue
        sent_at = row.get("sent_at", "")
        if not sent_at:
            continue
        try:
            sent_time = parse_iso(sent_at)
        except Exception:
            continue
        delta = now - sent_time
        if delta >= datetime.timedelta(hours=24) and not row.get("reminder_sent"):
            try:
                user = await bot.fetch_user(int(user_id))
                await send_24h_dm(user)
                update_target_row(ws, idx, {"reminder_sent": iso_now()})
                await asyncio.sleep(5)
            except Exception as e:
                await log_to_channel(f"⚠️ 24h follow-up failed for {user_id}: {e}")
        if delta >= datetime.timedelta(hours=72) and not row.get("second_reminder_sent"):
            try:
                user = await bot.fetch_user(int(user_id))
                await send_72h_dm(user)
                update_target_row(ws, idx, {"second_reminder_sent": iso_now()})
                await asyncio.sleep(5)
            except Exception as e:
                await log_to_channel(f"⚠️ 72h follow-up failed for {user_id}: {e}")

# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
