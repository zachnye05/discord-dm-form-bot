import os
import json
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands

import gspread

# ─────────────────────────────
# ENV + CONSTANTS
# ─────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

TARGETS_WS_NAME = "targets"
MESSAGES_WS_NAME = "messages"
RESPONSES_WS_NAME = "responses"

# targets sheet column indexes (1-based) – MATCHES YOUR SHEET
TARGET_COL_USER_ID = 1          # A
TARGET_COL_STATUS = 2           # B
TARGET_COL_INITIAL_SENT = 3     # C
TARGET_COL_REMINDER_SENT = 4    # D
TARGET_COL_SECOND_REMINDER_SENT = 5  # E
TARGET_COL_COMPLETED_AT = 6     # F
TARGET_COL_FORM_SUBMITTED = 7   # G
TARGET_COL_DM_ERROR = 8         # H

# keys we expect when we read targets as dicts
COL_USER_ID = "user_id"
COL_INITIAL_SENT = "initial_sent"
COL_REMINDER_SENT = "reminder_sent"
COL_SECOND_REMINDER_SENT = "second_reminder_sent"
COL_COMPLETED_AT = "completed_at"
COL_FORM_SUBMITTED = "form_submitted"
COL_DM_ERROR = "dm_error"

# guild/role gate
TARGET_GUILD_ID = 667532381376217089
EXCLUDED_ROLE_IDS = {
    744273392240164985,
    1327765815314878514,
}

# embed / button look
EMBED_COLOR = 0x963BF3
BANNER_URL = "https://cdn.discordapp.com/attachments/1436108078612484189/1436115777265598555/image.png"
BUTTON_LABEL = "Claim Your Free Week"

# pacing
INITIAL_DM_DELAY_SECONDS = 15
FOLLOWUP_DM_DELAY_SECONDS = 5

# caching
TARGETS_CACHE = []
MESSAGES_CACHE = {}
LAST_TARGETS_FETCH = 0.0
LAST_MESSAGES_FETCH = 0.0
TARGETS_REFRESH_SECONDS = 600
MESSAGES_REFRESH_SECONDS = 600

# ─────────────────────────────
# DISCORD SETUP
# ─────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # only works if enabled in Discord portal
bot = commands.Bot(command_prefix="!", intents=intents)


# ─────────────────────────────
# GOOGLE SHEETS HELPERS
# ─────────────────────────────
def get_sheet_client():
    service_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(service_json)
    gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh


def sheet_update_cell(row_index: int, col_index: int, value: str):
    try:
        sh = get_sheet_client()
        ws = sh.worksheet(TARGETS_WS_NAME)
        ws.update_cell(row_index, col_index, value)
    except Exception as e:
        print(f"[sheets] update error: {e}")


def sheet_append_response(discord_id: str, username: str, first: str, email: str, phone: str):
    try:
        sh = get_sheet_client()
        ws = sh.worksheet(RESPONSES_WS_NAME)
        now = datetime.datetime.utcnow().isoformat()
        ws.append_row([discord_id, username, first, email, phone, now])
    except Exception as e:
        print(f"[sheets] append response error: {e}")


async def fetch_targets_if_needed(force: bool = False):
    global TARGETS_CACHE, LAST_TARGETS_FETCH
    now = asyncio.get_event_loop().time()
    if not force and (now - LAST_TARGETS_FETCH) < TARGETS_REFRESH_SECONDS and TARGETS_CACHE:
        return TARGETS_CACHE

    try:
        sh = get_sheet_client()
        ws = sh.worksheet(TARGETS_WS_NAME)
        rows = ws.get_all_records()
        TARGETS_CACHE = rows
        LAST_TARGETS_FETCH = now
        return TARGETS_CACHE
    except Exception as e:
        print(f"[sheets] targets fetch error: {e}")
        return TARGETS_CACHE


async def fetch_messages_if_needed(force: bool = False):
    """
    messages sheet:
    row 1: key | content
    we force expected_headers so gspread stops complaining about non-unique headers
    """
    global MESSAGES_CACHE, LAST_MESSAGES_FETCH
    now = asyncio.get_event_loop().time()
    if not force and (now - LAST_MESSAGES_FETCH) < MESSAGES_REFRESH_SECONDS and MESSAGES_CACHE:
        return MESSAGES_CACHE

    try:
        sh = get_sheet_client()
        ws = sh.worksheet(MESSAGES_WS_NAME)
        rows = ws.get_all_records(expected_headers=["key", "content"])
        msg_map = {}
        for r in rows:
            k = r.get("key")
            v = r.get("content", "")
            if k:
                msg_map[k] = v
        MESSAGES_CACHE = msg_map
        LAST_MESSAGES_FETCH = now
        return MESSAGES_CACHE
    except Exception as e:
        print(f"[sheets] messages fetch error: {e}")
        return MESSAGES_CACHE


# ─────────────────────────────
# DISCORD UI (MODAL + BUTTON)
# ─────────────────────────────
class InfoForm(discord.ui.Modal, title="Claim your free week"):
    first_name = discord.ui.TextInput(
        label="First name",
        placeholder="Casey",
        required=True,
        max_length=80,
    )
    email = discord.ui.TextInput(
        label="Email",
        placeholder="you@example.com",
        required=True,
        max_length=120,
    )
    phone = discord.ui.TextInput(
        label="Phone number",
        placeholder="(987) 654-3210",
        required=True,
        max_length=50,
    )

    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        # write to responses sheet
        sheet_append_response(
            str(self.user_id),
            str(interaction.user),
            str(self.first_name),
            str(self.email),
            str(self.phone),
        )

        # mark user in targets
        try:
            sh = get_sheet_client()
            ws = sh.worksheet(TARGETS_WS_NAME)
            all_vals = ws.get_all_records()
            for idx, r in enumerate(all_vals, start=2):
                if str(r.get(COL_USER_ID)) == str(self.user_id):
                    now = datetime.datetime.utcnow().isoformat()
                    ws.update_cell(idx, TARGET_COL_COMPLETED_AT, now)
                    ws.update_cell(idx, TARGET_COL_FORM_SUBMITTED, "✅")
                    ws.update_cell(idx, TARGET_COL_STATUS, "✅ form submitted")
                    break
        except Exception as e:
            print(f"[sheets] marking completed failed: {e}")

        # respond
        try:
            await interaction.response.send_message(
                "Thanks for submitting your info.\n\n"
                "To claim your free week of Divine, click the link below👇\n\n"
                "<https://whop.com/checkout/plan_XiUlT3C057H67?d2c=true>",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            print(f"[modal] error responding to interaction: {e}")


class ClaimView(discord.ui.View):
    def __init__(self, target_user_id: int):
        super().__init__(timeout=None)
        self.target_user_id = target_user_id

    @discord.ui.button(
        label=BUTTON_LABEL,
        style=discord.ButtonStyle.blurple,
        custom_id="claim_free_week_btn",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InfoForm(self.target_user_id))


# ─────────────────────────────
# HELPERS
# ─────────────────────────────
async def send_log(message: str):
    if not LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(message)


def build_embed(body: str) -> discord.Embed:
    emb = discord.Embed(description=body, color=EMBED_COLOR)
    emb.set_image(url=BANNER_URL)
    return emb


async def check_excluded(user_id: int) -> bool:
    guild = bot.get_guild(TARGET_GUILD_ID)
    if not guild:
        return False
    member = guild.get_member(user_id)
    if not member:
        return False
    member_roles = {r.id for r in member.roles}
    return any(rid in member_roles for rid in EXCLUDED_ROLE_IDS)


def iso_to_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


# ─────────────────────────────
# MAIN LOOPS
# ─────────────────────────────
async def broadcaster_loop():
    """Send the FIRST DM to people who don't have initial_sent yet."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        targets = await fetch_targets_if_needed()
        messages = await fetch_messages_if_needed()

        initial_body = messages.get(
            "initial_dm",
            "Hey <@user>, tap below to claim your free week.",
        )
        now_iso = datetime.datetime.utcnow().isoformat()

        for idx, row in enumerate(targets, start=2):
            user_id = str(row.get(COL_USER_ID, "")).strip()
            if not user_id:
                continue

            # already sent?
            if row.get(COL_INITIAL_SENT):
                continue

            # role gate
            try:
                if await check_excluded(int(user_id)):
                    # mark status and skip
                    sheet_update_cell(idx, TARGET_COL_STATUS, "⛔ excluded role")
                    continue
            except Exception as e:
                print(f"[gate] error checking roles: {e}")

            # find user
            user = bot.get_user(int(user_id)) if user_id.isdigit() else None
            if not user and user_id.isdigit():
                try:
                    user = await bot.fetch_user(int(user_id))
                except Exception:
                    user = None

            if not user:
                # update status + error
                sheet_update_cell(idx, TARGET_COL_STATUS, "❌ user not found")
                sheet_update_cell(idx, TARGET_COL_DM_ERROR, "404 Not Found (error code: 10013): Unknown User")
                await send_log(f"⚠️ Could not DM `{user_id}` (user not found)")
                continue

            # personalize
            content = initial_body.replace("<@user>", user.mention)
            try:
                embed = build_embed(content)
                view = ClaimView(int(user_id))
                await user.send(embed=embed, view=view)

                # write to sheet
                sheet_update_cell(idx, TARGET_COL_STATUS, "✅ initial sent")
                sheet_update_cell(idx, TARGET_COL_INITIAL_SENT, now_iso)
                TARGETS_CACHE[idx - 2][COL_INITIAL_SENT] = now_iso

                await send_log(f"📨 Sent initial DM to <@{user_id}> ({user_id})")

                # ONLY delay on success
                await asyncio.sleep(INITIAL_DM_DELAY_SECONDS)
            except Exception as e:
                print(f"[dm] error sending to {user_id}: {e}")
                sheet_update_cell(idx, TARGET_COL_STATUS, "❌ DM failed")
                sheet_update_cell(idx, TARGET_COL_DM_ERROR, str(e))
                TARGETS_CACHE[idx - 2][COL_DM_ERROR] = str(e)
                # no delay

        await asyncio.sleep(60)


async def followup_loop():
    """Send 24h and 72h follow-ups based on initial_sent timestamp."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        targets = await fetch_targets_if_needed()
        messages = await fetch_messages_if_needed()

        follow24 = messages.get("followup_24h", "Just following up — tap below to claim.")
        follow72 = messages.get("followup_72h", "**IMPORTANT NOTICE** — last chance.")
        now = datetime.datetime.utcnow()

        for idx, row in enumerate(targets, start=2):
            user_id = str(row.get(COL_USER_ID, "")).strip()
            if not user_id:
                continue

            initial_ts = iso_to_dt(row.get(COL_INITIAL_SENT))
            if not initial_ts:
                continue

            # 24h follow-up
            if not row.get(COL_REMINDER_SENT):
                if (now - initial_ts).total_seconds() >= 24 * 3600:
                    user = bot.get_user(int(user_id)) if user_id.isdigit() else None
                    if not user and user_id.isdigit():
                        try:
                            user = await bot.fetch_user(int(user_id))
                        except Exception:
                            user = None
                    if user:
                        try:
                            embed = build_embed(follow24.replace("<@user>", user.mention))
                            view = ClaimView(int(user_id))
                            await user.send(embed=embed, view=view)
                            sheet_update_cell(idx, TARGET_COL_REMINDER_SENT, "✅")
                            sheet_update_cell(idx, TARGET_COL_STATUS, "✅ 24h follow-up sent")
                            TARGETS_CACHE[idx - 2][COL_REMINDER_SENT] = "✅"
                            await send_log(f"🔁 Sent 24h follow-up to <@{user_id}> ({user_id})")
                            await asyncio.sleep(FOLLOWUP_DM_DELAY_SECONDS)
                        except Exception as e:
                            print(f"[dm] 24h error to {user_id}: {e}")
                            sheet_update_cell(idx, TARGET_COL_DM_ERROR, str(e))
                    continue  # don’t try 72h in same pass

            # 72h follow-up
            if not row.get(COL_SECOND_REMINDER_SENT):
                if (now - initial_ts).total_seconds() >= 72 * 3600:
                    user = bot.get_user(int(user_id)) if user_id.isdigit() else None
                    if not user and user_id.isdigit():
                        try:
                            user = await bot.fetch_user(int(user_id))
                        except Exception:
                            user = None
                    if user:
                        try:
                            embed = build_embed(follow72.replace("<@user>", user.mention))
                            view = ClaimView(int(user_id))
                            await user.send(embed=embed, view=view)
                            sheet_update_cell(idx, TARGET_COL_SECOND_REMINDER_SENT, "✅")
                            sheet_update_cell(idx, TARGET_COL_STATUS, "✅ 72h follow-up sent")
                            TARGETS_CACHE[idx - 2][COL_SECOND_REMINDER_SENT] = "✅"
                            await send_log(f"🔁 Sent 72h follow-up to <@{user_id}> ({user_id})")
                            await asyncio.sleep(FOLLOWUP_DM_DELAY_SECONDS)
                        except Exception as e:
                            print(f"[dm] 72h error to {user_id}: {e}")
                            sheet_update_cell(idx, TARGET_COL_DM_ERROR, str(e))

        await asyncio.sleep(60)


# ─────────────────────────────
# COMMANDS
# ─────────────────────────────
def is_admin_interaction(interaction: discord.Interaction) -> bool:
    if interaction.user is None:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


@bot.tree.command(name="test", description="Send all 3 DMs to yourself right now.")
async def test_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    messages = await fetch_messages_if_needed(force=True)
    user = interaction.user

    initial_body = messages.get("initial_dm", "Hey <@user>, tap below to claim.")
    follow24 = messages.get("followup_24h", "Just following up — tap below to claim.")
    follow72 = messages.get("followup_72h", "**IMPORTANT NOTICE** — last chance.")

    for body in (initial_body, follow24, follow72):
        embed = build_embed(body.replace("<@user>", user.mention))
        view = ClaimView(user.id)
        await user.send(embed=embed, view=view)
        await asyncio.sleep(1)

    await interaction.followup.send("Sent you the 3 test DMs ✅", ephemeral=True)
    await send_log(f"📝 Sent test DMs to {user.mention} (ID: {user.id})")


@bot.tree.command(name="blast", description="(Admin) force a full reload of sheets cache.")
async def blast_cmd(interaction: discord.Interaction):
    if not is_admin_interaction(interaction):
        await interaction.response.send_message("You must be admin to do this.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await fetch_targets_if_needed(force=True)
    await fetch_messages_if_needed(force=True)
    await interaction.followup.send("Reloaded sheets cache ✅", ephemeral=True)
    await send_log(f"♻️ Sheets cache reloaded by {interaction.user.mention}")


# ─────────────────────────────
# BOT EVENTS
# ─────────────────────────────
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"[cmd sync] {e}")

    await send_log("🟣 Divine Messenger online.")
    bot.loop.create_task(broadcaster_loop())
    bot.loop.create_task(followup_loop())


# ─────────────────────────────
# RUN
# ─────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
