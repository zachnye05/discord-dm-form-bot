import os
import json
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
import gspread

# ------------------------------------------------
# ENV VARS
# ------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"

# ------------------------------------------------
# GUILD / ROLE GATE
# ------------------------------------------------
TARGET_GUILD_ID = 667532381376217089
EXCLUDED_ROLE_IDS = {
    744273392240164985,
    1327765815314878514,
}

# ------------------------------------------------
# EMBED LOOK
# ------------------------------------------------
EMBED_COLOR = 0x963BF3
BANNER_URL = (
    "https://cdn.discordapp.com/attachments/1436108078612484189/1436115777265598555/"
    "image.png?ex=690e6e8b&is=690d1d0b&hm=c119554967b072298d91b5f1fb1cfb75b0a815fcec29dcd8c4d32639248442b9&"
)

MESSAGE_CACHE = {}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # required for role checks
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------------------------------
# HELPERS
# ------------------------------------------------
async def log_to_channel(text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(text[:2000])
    except Exception:
        pass

def iso_now():
    return datetime.datetime.utcnow().isoformat()

def parse_iso(s):
    return datetime.datetime.fromisoformat(s)

# ------------------------------------------------
# GOOGLE SHEETS
# ------------------------------------------------
def get_client():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return gspread.service_account_from_dict(sa_info)

def get_ws(name):
    gc = get_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(name)

def load_targets():
    ws = get_ws(TARGETS_WS_NAME)
    return ws.get_all_records(), ws

def update_target_row(ws, row_index, updates):
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
    keys = ws.col_values(1)
    contents = ws.col_values(2)
    MESSAGE_CACHE = {k.strip(): c.replace("\\n", "\n") for k, c in zip(keys[1:], contents[1:]) if k.strip()}
    return MESSAGE_CACHE

def append_response(user_id, username, payload):
    try:
        ws = get_ws(RESPONSES_WS_NAME)
        ws.append_row([
            iso_now(),
            str(user_id),
            username,
            payload.get("first_name", ""),
            payload.get("email", ""),
            payload.get("phone", ""),
        ])
    except Exception:
        pass

# ------------------------------------------------
# DISCORD UI
# ------------------------------------------------
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

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        append_response(self.user_id, self.username, {
            "first_name": str(self.first_name),
            "email": str(self.email),
            "phone": str(self.phone),
        })

        try:
            targets, ws = load_targets()
            for idx, row in enumerate(targets, start=2):
                if str(row.get("user_id", "")) == str(self.user_id):
                    update_target_row(ws, idx, {
                        "status": "form_submitted",
                        "form_submitted": "✅",
                        "completed_at": iso_now(),
                    })
                    break
        except Exception:
            pass

        await log_to_channel(f"✅ Form submitted by `{self.username}` ({self.user_id})")

        await interaction.followup.send("✅ Thanks — your info was submitted.", ephemeral=True)
        try:
            await interaction.user.send(
                "Thanks for submitting your info.\n\n"
                "To claim your free week of Divine, click the link below👇\n\n"
                "<https://whop.com/checkout/plan_XiUlT3C057H67?d2c=true>"
            )
        except Exception:
            pass

class FormView(discord.ui.View):
    def __init__(self, user, timeout=None):
        super().__init__(timeout=timeout)
        self.user = user

    @discord.ui.button(label="Claim Your Free Week", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction, button):
        modal = InfoForm(user_id=self.user.id, username=str(self.user))
        await interaction.response.send_modal(modal)

# ------------------------------------------------
# ROLE CHECK
# ------------------------------------------------
async def has_excluded_role(user_id):
    guild = bot.get_guild(TARGET_GUILD_ID)
    if guild is None:
        return False
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.NotFound:
        return False

    member_roles = {r.id for r in member.roles}
    return any(role_id in member_roles for role_id in EXCLUDED_ROLE_IDS)

# ------------------------------------------------
# DM FUNCTIONS
# ------------------------------------------------
async def send_embed_dm(user, key, fallback):
    text = MESSAGE_CACHE.get(key, fallback).replace("{user}", user.mention)
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    embed.set_image(url=BANNER_URL)
    await user.send(embed=embed, view=FormView(user))

async def send_initial_dm(user):
    await send_embed_dm(user, "initial_dm", "Tap below to claim your free week.")
    await log_to_channel(f"📤 Sent initial DM to {user} ({user.id})")

async def send_24h_dm(user):
    await send_embed_dm(user, "followup_24h", "Following up — claim your free week.")
    await log_to_channel(f"🔁 Sent 24h follow-up to {user} ({user.id})")

async def send_72h_dm(user):
    await send_embed_dm(user, "followup_72h", "Last chance to claim your free week.")
    await log_to_channel(f"⏰ Sent 72h follow-up to {user} ({user.id})")

# ------------------------------------------------
# STARTUP & REBASE
# ------------------------------------------------
async def rebase_initials_for_followups():
    try:
        targets, ws = load_targets()
        now_iso = iso_now()
        rebased = 0
        for idx, row in enumerate(targets, start=2):
            if str(row.get("initial_sent", "")) == "✅" and not row.get("reminder_sent") and not row.get("second_reminder_sent"):
                update_target_row(ws, idx, {"sent_at": now_iso})
                rebased += 1
        if rebased:
            await log_to_channel(f"🔁 Rebased {rebased} targets for follow-ups.")
    except Exception as e:
        await log_to_channel(f"⚠️ Rebase failed: {e}")

# ------------------------------------------------
# EVENTS
# ------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    load_messages()
    await log_to_channel("🟣 Divine Messenger online.")
    await rebase_initials_for_followups()
    followup_checker.start()
    await bot.tree.sync()

# ------------------------------------------------
# COMMANDS
# ------------------------------------------------
@bot.tree.command(name="blast", description="Send DMs to users from Google Sheet.")
async def blast(interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        return

    await interaction.response.send_message("Sending DMs…", ephemeral=True)

    targets, ws = load_targets()
    count = 0

    for idx, row in enumerate(targets, start=2):
        user_id = str(row.get("user_id", "")).strip()
        if not user_id:
            continue
        user_id = int(user_id)

        if await has_excluded_role(user_id):
            update_target_row(ws, idx, {"status": "has_excluded_role"})
            continue

        try:
            user = await bot.fetch_user(user_id)
            await send_initial_dm(user)
            update_target_row(ws, idx, {"initial_sent": "✅", "sent_at": iso_now(), "status": "initial_sent"})
            count += 1
            await asyncio.sleep(15)
        except Exception as e:
            update_target_row(ws, idx, {"dm_error": f"❌ {e}"})
            await log_to_channel(f"⚠️ Failed to DM {user_id}: {e}")

    await log_to_channel(f"✅ /blast complete. Sent {count} DMs.")

# ------------------------------------------------
# FOLLOWUP LOOP
# ------------------------------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    targets, ws = load_targets()
    now = datetime.datetime.utcnow()

    for idx, row in enumerate(targets, start=2):
        user_id_str = row.get("user_id")
        if not user_id_str:
            continue
        user_id = int(user_id_str)

        if await has_excluded_role(user_id):
            update_target_row(ws, idx, {"status": "has_excluded_role"})
            continue

        if str(row.get("form_submitted", "")).strip() == "✅":
            continue

        sent_at = row.get("sent_at")
        if not sent_at:
            continue

        try:
            delta = now - parse_iso(sent_at)
        except Exception:
            continue

        if delta >= datetime.timedelta(hours=24) and not row.get("reminder_sent"):
            try:
                user = await bot.fetch_user(user_id)
                await send_24h_dm(user)
                update_target_row(ws, idx, {"reminder_sent": "✅", "status": "followup_24h_sent"})
            except Exception as e:
                await log_to_channel(f"⚠️ 24h DM error for {user_id}: {e}")

        elif delta >= datetime.timedelta(hours=72) and not row.get("second_reminder_sent"):
            try:
                user = await bot.fetch_user(user_id)
                await send_72h_dm(user)
                update_target_row(ws, idx, {"second_reminder_sent": "✅", "status": "followup_72h_sent"})
            except Exception as e:
                await log_to_channel(f"⚠️ 72h DM error for {user_id}: {e}")

# ------------------------------------------------
# RUN
# ------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
