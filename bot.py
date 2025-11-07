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

# sheet names
TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"

# guild / role gate
TARGET_GUILD_ID = 667532381376217089         # the server to check
EXCLUDE_ROLE_ID = 744273392240164985         # users with this role should NOT be DMed

# embed look
EMBED_COLOR = 0x963BF3
BANNER_URL = (
    "https://cdn.discordapp.com/attachments/1436108078612484189/1436115777265598555/"
    "image.png?ex=690e6e8b&is=690d1d0b&hm=c119554967b072298d91b5f1fb1cfb75b0a815fcec29dcd8c4d32639248442b9&"
)

MESSAGE_CACHE = {}

# we need members intent to check roles
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------------------------------
# HELPER FUNCS
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

def iso_now() -> str:
    return datetime.datetime.utcnow().isoformat()

def parse_iso(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)

# ------------------------------------------------
# GOOGLE SHEETS
# ------------------------------------------------
def get_client():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return gspread.service_account_from_dict(sa_info)

def get_ws(name: str):
    gc = get_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(name)

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

def append_response(user_id: int, username: str, payload: dict):
    try:
        ws = get_ws(RESPONSES_WS_NAME)
    except Exception:
        return
    ws.append_row([
        iso_now(),
        str(user_id),
        username,
        payload.get("first_name", ""),
        payload.get("email", ""),
        payload.get("phone", ""),
    ])

# ------------------------------------------------
# DISCORD UI (MODAL + BUTTON)
# ------------------------------------------------
class InfoForm(discord.ui.Modal, title="Claim your free week"):
    def __init__(self, user_id: int, username: str):
        super().__init__()
        self.user_id = user_id
        self.username = username

        self.first_name = discord.ui.TextInput(
            label="First name",
            required=True,
            placeholder="Casey"
        )
        self.email = discord.ui.TextInput(
            label="Email",
            required=True,
            placeholder="casey@divineresell.com"
        )
        self.phone = discord.ui.TextInput(
            label="Phone number",
            required=True,
            placeholder="(987) 654-3210"
        )

        self.add_item(self.first_name)
        self.add_item(self.email)
        self.add_item(self.phone)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        append_response(
            self.user_id,
            self.username,
            {
                "first_name": str(self.first_name),
                "email": str(self.email),
                "phone": str(self.phone),
            },
        )

        # mark on targets
        try:
            targets, ws = load_targets()
            for idx, row in enumerate(targets, start=2):
                if str(row.get("user_id", "")).strip() == str(self.user_id).strip():
                    update_target_row(ws, idx, {
                        "status": "form_submitted",
                        "form_submitted": "✅",
                        "completed_at": iso_now(),
                    })
                    break
        except Exception as e:
            print("Error marking target completed:", e)

        await log_to_channel(f"✅ Form submitted by `{self.username}` (ID: {self.user_id})")

        await interaction.followup.send("✅ Thanks — your info was submitted.", ephemeral=True)

        # send claim link
        try:
            await interaction.user.send(
                "Thanks for submitting your info.\n\n"
                "To claim your free week of Divine, click the link below👇\n\n"
                "<https://whop.com/checkout/plan_XiUlT3C057H67?d2c=true>"
            )
        except Exception as e:
            print("Error sending claim DM:", e)

class FormView(discord.ui.View):
    def __init__(self, user: discord.User, timeout=None):
        super().__init__(timeout=timeout)
        self.user = user

    @discord.ui.button(
        label="Claim Your Free Week",
        style=discord.ButtonStyle.primary
    )
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = InfoForm(user_id=self.user.id, username=str(self.user))
        await interaction.response.send_modal(modal)

# ------------------------------------------------
# ROLE CHECK
# ------------------------------------------------
async def has_excluded_role(user_id: int) -> bool:
    """Return True if the user is in TARGET_GUILD_ID and has EXCLUDE_ROLE_ID."""
    if not TARGET_GUILD_ID or not EXCLUDE_ROLE_ID:
        return False

    guild = bot.get_guild(TARGET_GUILD_ID)
    if guild is None:
        # try fetch
        try:
            guild = await bot.fetch_guild(TARGET_GUILD_ID)
        except Exception:
            return False

    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except Exception:
        # user not in guild
        return False

    for r in member.roles:
        if r.id == EXCLUDE_ROLE_ID:
            return True
    return False

# ------------------------------------------------
# DM HELPERS
# ------------------------------------------------
def get_message(key: str, default: str = "") -> str:
    return MESSAGE_CACHE.get(key, default)

async def send_embed_dm(user: discord.User, message_key: str, fallback: str):
    text = get_message(message_key, fallback)
    text = (
        text.replace("<@user>", user.mention)
            .replace("{user}", user.mention)
            .replace("{username}", user.name)
    )
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    embed.set_image(url=BANNER_URL)
    await user.send(embed=embed, view=FormView(user))

async def send_initial_dm(user: discord.User):
    await send_embed_dm(user, "initial_dm", "Hey! Tap below to claim your free week.")
    await log_to_channel(f"📤 Sent initial DM to {user} (ID: {user.id})")

async def send_24h_dm(user: discord.User):
    await send_embed_dm(user, "followup_24h", "Just following up — tap below to claim.")
    await log_to_channel(f"🔁 Sent 24h follow-up to {user} (ID: {user.id})")

async def send_72h_dm(user: discord.User):
    await send_embed_dm(user, "followup_72h", "**IMPORTANT NOTICE** — last chance to claim.")
    await log_to_channel(f"🔁 Sent 72h follow-up to {user} (ID: {user.id})")

# ------------------------------------------------
# REBASE ON STARTUP (for emergency quit)
# ------------------------------------------------
async def rebase_initials_for_followups():
    """
    After a restart, any user who already got the initial DM (initial_sent == ✅)
    but has not yet gotten reminder / second reminder will have sent_at reset to now,
    so their followups start 24h from THIS restart.
    """
    try:
        targets, ws = load_targets()
        now_iso = iso_now()
        rebased = 0
        for idx, row in enumerate(targets, start=2):
            if str(row.get("initial_sent", "")).strip() == "✅":
                if not str(row.get("reminder_sent", "")).strip() and not str(row.get("second_reminder_sent", "")).strip():
                    update_target_row(ws, idx, {"sent_at": now_iso})
                    rebased += 1
        if rebased:
            await log_to_channel(f"🔁 Rebased {rebased} targets to now for follow-ups.")
    except Exception as e:
        await log_to_channel(f"⚠️ Could not rebase followups: `{e}`")

# ------------------------------------------------
# EVENTS
# ------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        loaded = load_messages()
        await log_to_channel(f"🗂 Loaded message keys: {list(loaded.keys())}")
    except Exception as e:
        await log_to_channel(f"⚠️ Could not load messages on startup: `{repr(e)}`")

    # rebase followups after emergency quit
    await rebase_initials_for_followups()

    try:
        await bot.tree.sync()
    except Exception as e:
        print("Error syncing commands:", e)

    await log_to_channel("🟣 Divine DM bot is online.")
    followup_checker.start()

# ------------------------------------------------
# COMMANDS
# ------------------------------------------------
@bot.tree.command(name="blast", description="DM all users from the targets sheet.")
async def blast(interaction: discord.Interaction):
    # owner gate
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You can't run this.", ephemeral=True)
        return

    # channel gate
    if LOG_CHANNEL_ID and interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message("Please run this command in the logs channel.", ephemeral=True)
        return

    await interaction.response.send_message("Sending DMs (15s delay per successful DM)…", ephemeral=True)

    targets, ws = load_targets()
    sent_count = 0

    for idx, row in enumerate(targets, start=2):
        user_id_str = str(row.get("user_id", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        form_submitted = str(row.get("form_submitted", "")).strip().upper()

        if not user_id_str:
            continue

        # skip if already somewhere in the flow or already submitted
        if status in (
            "initial_sent",
            "followup_24h_sent",
            "followup_72h_sent",
            "form_submitted",
            "completed",
            "has_excluded_role",
        ) or form_submitted in ("TRUE", "✅"):
            continue

        user_id = int(user_id_str)

        # role gate: skip people with excluded role
        if await has_excluded_role(user_id):
            update_target_row(ws, idx, {
                "status": "has_excluded_role"
            })
            await log_to_channel(f"⏭️ Skipped {user_id} (has excluded role).")
            continue

        try:
            user = await bot.fetch_user(user_id)
            await send_initial_dm(user)

            update_target_row(ws, idx, {
                "status": "initial_sent",
                "initial_sent": "✅",
                "dm_error": "",
                "sent_at": iso_now(),   # for followup timing
            })
            sent_count += 1

            # delay ONLY on actual send
            await asyncio.sleep(15)

        except Exception as e:
            update_target_row(ws, idx, {"dm_error": f"❌ {e}"})
            await log_to_channel(f"⚠️ Failed to DM {user_id}: `{e}`")
            # no delay here

    await interaction.followup.send(f"✅ Done. Sent {sent_count} DMs.", ephemeral=True)
    await log_to_channel(f"✅ /blast complete. Sent {sent_count} DMs.")

@bot.tree.command(name="test", description="Send all 3 Divine DMs to yourself (for testing).")
async def test_command(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("You can't run this.", ephemeral=True)
        return
    if LOG_CHANNEL_ID and interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message("Please run this command in the logs channel.", ephemeral=True)
        return

    await interaction.response.send_message("Sending test DMs to you…", ephemeral=True)

    user = interaction.user
    await send_initial_dm(user)
    await asyncio.sleep(1)
    await send_24h_dm(user)
    await asyncio.sleep(1)
    await send_72h_dm(user)

    await log_to_channel(f"🧪 Sent test DMs to {user} (ID: {user.id})")

# ------------------------------------------------
# FOLLOWUP LOOP
# ------------------------------------------------
@tasks.loop(minutes=5)
async def followup_checker():
    # reload messages quietly
    try:
        load_messages()
    except Exception:
        pass

    # load targets
    try:
        targets, ws = load_targets()
    except Exception as e:
        print("Error loading targets in loop:", e)
        return

    now = datetime.datetime.utcnow()

    for idx, row in enumerate(targets, start=2):
        user_id_str = str(row.get("user_id", "")).strip()
        if not user_id_str:
            continue

        user_id = int(user_id_str)

        status = str(row.get("status", "")).strip().lower()
        form_submitted = str(row.get("form_submitted", "")).strip()
        initial_sent = str(row.get("initial_sent", "")).strip()
        reminder_sent = str(row.get("reminder_sent", "")).strip()
        second_sent = str(row.get("second_reminder_sent", "")).strip()
        completed = row.get("completed_at", "")

        # stop if submitted, completed, or explicitly skipped
        if status == "has_excluded_role":
            continue
        if form_submitted in ("✅", "TRUE") or status == "form_submitted" or completed:
            continue

        if not initial_sent:
            continue

        sent_at = row.get("sent_at", "")
        if not sent_at:
            continue

        # role gate again for followups
        if await has_excluded_role(user_id):
            update_target_row(ws, idx, {"status": "has_excluded_role"})
            await log_to_channel(f"⏭️ Skipped follow-up to {user_id} (has excluded role).")
            continue

        try:
            sent_time = parse_iso(sent_at)
        except Exception:
            continue

        delta = now - sent_time

        # 24h follow-up
        if delta >= datetime.timedelta(hours=24) and not reminder_sent:
            try:
                user = await bot.fetch_user(user_id)
                await send_24h_dm(user)
                update_target_row(ws, idx, {
                    "reminder_sent": "✅",
                    "status": "followup_24h_sent",
                })
                await asyncio.sleep(5)
            except Exception as e:
                await log_to_channel(f"⚠️ Failed 24h follow-up to {user_id}: `{e}`")

        # 72h follow-up
        if delta >= datetime.timedelta(hours=72) and not second_sent:
            try:
                user = await bot.fetch_user(user_id)
                await send_72h_dm(user)
                update_target_row(ws, idx, {
                    "second_reminder_sent": "✅",
                    "status": "followup_72h_sent",
                })
                await asyncio.sleep(5)
            except Exception as e:
                await log_to_channel(f"⚠️ Failed 72h follow-up to {user_id}: `{e}`")

# ------------------------------------------------
# RUN
# ------------------------------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(DISCORD_TOKEN)
