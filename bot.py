# bot.py
import os
import json
import asyncio
import datetime
import random

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

# DM pacing
INITIAL_DM_DELAY_SECONDS = 15  # only on success
FOLLOWUP_DM_DELAY_SECONDS = 5

# sheets caching (fix 429s)
TARGETS_CACHE = []
MESSAGES_CACHE = {}
LAST_TARGETS_FETCH = 0.0
LAST_MESSAGES_FETCH = 0.0
TARGETS_REFRESH_SECONDS = 600  # 10 minutes
MESSAGES_REFRESH_SECONDS = 600

# sheet column names we expect
COL_USER_ID = "user_id"
COL_INITIAL_SENT = "initial_sent"
COL_REMINDER_SENT = "reminder_sent"
COL_SECOND_REMINDER_SENT = "second_reminder_sent"
COL_COMPLETED_AT = "completed_at"
COL_FORM_SUBMITTED = "form_submitted"
COL_DM_ERROR = "dm_error"

# ─────────────────────────────
# DISCORD SETUP
# ─────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # ok to turn on
# we are NOT forcing members intent so the bot won’t crash if you didn’t enable it
bot = commands.Bot(command_prefix="!", intents=intents)


# ─────────────────────────────
# GOOGLE SHEETS HELPERS
# ─────────────────────────────
def get_sheet_client():
    # Railway: GOOGLE_SERVICE_ACCOUNT_JSON is a JSON string of the service account
    service_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(service_json)
    gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh


async def fetch_targets_if_needed(force: bool = False):
    """Read the targets sheet, but not more than every TARGETS_REFRESH_SECONDS."""
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
    except gspread.exceptions.APIError as e:
        print(f"[sheets] targets fetch hit quota: {e}")
        return TARGETS_CACHE


async def fetch_messages_if_needed(force: bool = False):
    """Load messages sheet into a dict: key -> content."""
    global MESSAGES_CACHE, LAST_MESSAGES_FETCH
    now = asyncio.get_event_loop().time()
    if not force and (now - LAST_MESSAGES_FETCH) < MESSAGES_REFRESH_SECONDS and MESSAGES_CACHE:
        return MESSAGES_CACHE

    try:
        sh = get_sheet_client()
        ws = sh.worksheet(MESSAGES_WS_NAME)
        rows = ws.get_all_records()
        msg_map = {}
        for r in rows:
            key = r.get("key")
            content = r.get("content", "")
            if key:
                msg_map[key] = content
        MESSAGES_CACHE = msg_map
        LAST_MESSAGES_FETCH = now
        return MESSAGES_CACHE
    except gspread.exceptions.APIError as e:
        print(f"[sheets] messages fetch hit quota: {e}")
        return MESSAGES_CACHE


def sheet_update_cell(row_index: int, col_index: int, value: str):
    """Small helper to write to sheets without re-reading everything."""
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


# ─────────────────────────────
# DISCORD UI (BUTTON + MODAL)
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
        # log to sheet
        sheet_append_response(
            str(self.user_id),
            str(interaction.user),
            str(self.first_name),
            str(self.email),
            str(self.phone),
        )

        # mark the user as completed in targets sheet
        # we need to find their row
        try:
            sh = get_sheet_client()
            ws = sh.worksheet(TARGETS_WS_NAME)
            all_vals = ws.get_all_records()
            # row 1 = header, so add 2
            for idx, r in enumerate(all_vals, start=2):
                if str(r.get(COL_USER_ID)) == str(self.user_id):
                    now = datetime.datetime.utcnow().isoformat()
                    ws.update_cell(idx, 5, now)  # completed_at
                    ws.update_cell(idx, 6, "✅")  # form_submitted
                    break
        except Exception as e:
            print(f"[sheets] marking completed failed: {e}")

        # send the confirmation DM (ephemeral to the user)
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
        # open the modal
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
    """Return True if user has any excluded role in TARGET_GUILD_ID. If we can't see them, return False."""
    guild = bot.get_guild(TARGET_GUILD_ID)
    if not guild:
        return False
    member = guild.get_member(user_id)
    if not member:
        # we don't have members intent or user not in guild cache → allow
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
    """Loop through targets and send initial DMs to anyone who doesn't have initial_sent yet."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        targets = await fetch_targets_if_needed()
        messages = await fetch_messages_if_needed()

        initial_body = messages.get("initial_dm", "Hey <@user>, tap below to claim your free week.")
        now_iso = datetime.datetime.utcnow().isoformat()

        for idx, row in enumerate(targets, start=2):  # 2 = header offset
            user_id = str(row.get(COL_USER_ID, "")).strip()
            if not user_id:
                continue

            # skip if initial already sent
            if row.get(COL_INITIAL_SENT):
                continue

            # role gate
            try:
                if await check_excluded(int(user_id)):
                    print(f"[gate] skipping {user_id} due to excluded role")
                    continue
            except Exception as e:
                print(f"[gate] error checking roles: {e}")

            # try DM
            user = bot.get_user(int(user_id))
            if not user:
                try:
                    user = await bot.fetch_user(int(user_id))
                except Exception:
                    user = None

            if not user:
                # log error
                sheet_update_cell(idx, 7, "user not found")  # dm_error col
                await send_log(f"⚠️ Could not DM `{user_id}` (user not found)")
                continue

            # personalize
            content = initial_body.replace("<@user>", user.mention)

            try:
                embed = build_embed(content)
                view = ClaimView(int(user_id))
                await user.send(embed=embed, view=view)

                # mark initial_sent (col 2)
                sheet_update_cell(idx, 2, now_iso)
                # keep cache in sync
                TARGETS_CACHE[idx - 2][COL_INITIAL_SENT] = now_iso

                await send_log(f"📨 Sent initial DM to <@{user_id}> ({user_id})")

                # IMPORTANT: only delay if we actually sent
                await asyncio.sleep(INITIAL_DM_DELAY_SECONDS)
            except Exception as e:
                print(f"[dm] error sending to {user_id}: {e}")
                sheet_update_cell(idx, 7, str(e))  # dm_error
                TARGETS_CACHE[idx - 2][COL_DM_ERROR] = str(e)
                # no delay here – move on

        # sleep a bit before next scan
        await asyncio.sleep(60)


async def followup_loop():
    """Check every minute for people who need 24h or 72h follow-ups."""
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

            initial_sent_ts = iso_to_dt(row.get(COL_INITIAL_SENT))
            if not initial_sent_ts:
                # never sent initial → skip
                continue

            # 24h follow-up
            if not row.get(COL_REMINDER_SENT):
                if (now - initial_sent_ts).total_seconds() >= 24 * 3600:
                    # send 24h follow-up
                    user = bot.get_user(int(user_id))
                    if not user:
                        try:
                            user = await bot.fetch_user(int(user_id))
                        except Exception:
                            user = None

                    if user:
                        try:
                            embed = build_embed(follow24.replace("<@user>", user.mention))
                            view = ClaimView(int(user_id))
                            await user.send(embed=embed, view=view)
                            sheet_update_cell(idx, 3, "✅")  # reminder_sent
                            TARGETS_CACHE[idx - 2][COL_REMINDER_SENT] = "✅"
                            await send_log(f"🔁 Sent 24h follow-up to <@{user_id}> ({user_id})")
                            await asyncio.sleep(FOLLOWUP_DM_DELAY_SECONDS)
                        except Exception as e:
                            print(f"[dm] 24h error to {user_id}: {e}")
                    continue  # don't also do 72h same pass

            # 72h follow-up
            if not row.get(COL_SECOND_REMINDER_SENT):
                # 72h from initial
                if (now - initial_sent_ts).total_seconds() >= 72 * 3600:
                    user = bot.get_user(int(user_id))
                    if not user:
                        try:
                            user = await bot.fetch_user(int(user_id))
                        except Exception:
                            user = None
                    if user:
                        try:
                            embed = build_embed(follow72.replace("<@user>", user.mention))
                            view = ClaimView(int(user_id))
                            await user.send(embed=embed, view=view)
                            sheet_update_cell(idx, 4, "✅")  # second_reminder_sent
                            TARGETS_CACHE[idx - 2][COL_SECOND_REMINDER_SENT] = "✅"
                            await send_log(f"🔁 Sent 72h follow-up to <@{user_id}> ({user_id})")
                            await asyncio.sleep(FOLLOWUP_DM_DELAY_SECONDS)
                        except Exception as e:
                            print(f"[dm] 72h error to {user_id}: {e}")

        await asyncio.sleep(60)


# ─────────────────────────────
# COMMANDS
# ─────────────────────────────
def is_admin_interaction(interaction: discord.Interaction) -> bool:
    # easiest: if they have administrator in that guild
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
    # start loops
    bot.loop.create_task(broadcaster_loop())
    bot.loop.create_task(followup_loop())


# ─────────────────────────────
# RUN
# ─────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
