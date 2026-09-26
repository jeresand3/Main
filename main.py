import re
import os
import html
import sqlite3
import secrets
import asyncio
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode

import discord
from discord.ext import commands
from discord.ui import Button, View

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse

import uvicorn
import requests


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
REDIRECT_URI = f"{PUBLIC_BASE_URL}/callback"

TICKET_CATEGORY_ID = 1552294553413746769
DATABASE_FILE = os.getenv("DATABASE_FILE", "verification.db")

ROLE_MAP = {
    "50K_SUBS": (50_000, 1552213495909711933),
    "100K_SUBS": (100_000, 1552242435290038292),
    "1M_SUBS": (1_000_000, 1552242872474927165),
    "1M_VIEWS": (1_000_000, 1552243824867147827),
    "10M_VIEWS": (10_000_000, 1552241736867381269),
    "50M_VIEWS": (50_000_000, 1552243153602609172),
    "100M_VIEWS": (100_000_000, 1552243231025139754),
    "1B_VIEWS": (1_000_000_000, 1552243361250025534),
}


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================

def validate_configuration():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not PUBLIC_BASE_URL:
        missing.append("PUBLIC_BASE_URL")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    if not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must start with https://")


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS google_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            google_sub TEXT NOT NULL UNIQUE,
            google_email TEXT NOT NULL,
            total_subscribers INTEGER NOT NULL DEFAULT 0,
            total_views INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT NOT NULL,
            UNIQUE(discord_user_id, google_sub)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS verified_channels (
            channel_id TEXT PRIMARY KEY,
            discord_user_id TEXT NOT NULL,
            google_sub TEXT NOT NULL,
            channel_title TEXT NOT NULL,
            subscriber_count INTEGER NOT NULL,
            view_count INTEGER NOT NULL,
            verified_at TEXT NOT NULL
        )
        """
    )

    # Migrate an older database if the old table exists.
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='verifications'"
    )
    old_table_exists = cursor.fetchone() is not None

    if old_table_exists:
        cursor.execute("PRAGMA table_info(verifications)")
        old_columns = {row[1] for row in cursor.fetchall()}

        if {"discord_user_id", "channel_id"}.issubset(old_columns):
            if {
                "google_sub",
                "channel_title",
                "subscriber_count",
                "view_count",
                "verified_at",
            }.issubset(old_columns):
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO verified_channels (
                        channel_id,
                        discord_user_id,
                        google_sub,
                        channel_title,
                        subscriber_count,
                        view_count,
                        verified_at
                    )
                    SELECT
                        channel_id,
                        discord_user_id,
                        google_sub,
                        channel_title,
                        subscriber_count,
                        view_count,
                        verified_at
                    FROM verifications
                    WHERE channel_id IS NOT NULL
                    """
                )

    # Some earlier versions may have created verified_channels
    # without google_sub.
    cursor.execute("PRAGMA table_info(verified_channels)")
    verified_columns = {row[1] for row in cursor.fetchall()}

    if "google_sub" not in verified_columns:
        cursor.execute("ALTER TABLE verified_channels ADD COLUMN google_sub TEXT")

    connection.commit()
    connection.close()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_google_account(discord_user_id, google_sub):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM google_accounts
        WHERE discord_user_id = ? AND google_sub = ?
        """,
        (str(discord_user_id), google_sub),
    ).fetchone()

    connection.close()
    return row


def get_google_account_by_sub(google_sub):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM google_accounts
        WHERE google_sub = ?
        """,
        (google_sub,),
    ).fetchone()

    connection.close()
    return row


def get_accounts_for_discord(discord_user_id):
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT *
        FROM google_accounts
        WHERE discord_user_id = ?
        ORDER BY verified_at ASC
        """,
        (str(discord_user_id),),
    ).fetchall()

    connection.close()
    return rows


def get_verified_channel(channel_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM verified_channels
        WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchone()

    connection.close()
    return row


def get_aggregate_totals(discord_user_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT
            COALESCE(SUM(total_subscribers), 0) AS total_subscribers,
            COALESCE(SUM(total_views), 0) AS total_views
        FROM google_accounts
        WHERE discord_user_id = ?
        """,
        (str(discord_user_id),),
    ).fetchone()

    connection.close()

    return int(row["total_subscribers"]), int(row["total_views"])


def save_google_account_and_channels(
    discord_user_id,
    google_sub,
    google_email,
    channels,
):
    verified_at = datetime.now(timezone.utc).isoformat()

    total_subscribers = sum(
        channel["subscriber_count"] for channel in channels
    )

    total_views = sum(
        channel["view_count"] for channel in channels
    )

    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO google_accounts (
                discord_user_id,
                google_sub,
                google_email,
                total_subscribers,
                total_views,
                verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(discord_user_id),
                google_sub,
                google_email,
                total_subscribers,
                total_views,
                verified_at,
            ),
        )

        for channel in channels:
            connection.execute(
                """
                INSERT INTO verified_channels (
                    channel_id,
                    discord_user_id,
                    google_sub,
                    channel_title,
                    subscriber_count,
                    view_count,
                    verified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel["channel_id"],
                    str(discord_user_id),
                    google_sub,
                    channel["channel_title"],
                    channel["subscriber_count"],
                    channel["view_count"],
                    verified_at,
                ),
            )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return total_subscribers, total_views


# ============================================================
# OAUTH / ACTIVE VERIFICATION STATE
# ============================================================

oauth_states = {}
active_verification_channels = {}

# Prevents rapid multiple clicks from creating multiple tickets.
verification_creation_locks = {}


def cleanup_expired_oauth_states():
    now = datetime.now(timezone.utc).timestamp()
    expired = []

    for state, data in oauth_states.items():
        if now - data["created_at"] > 600:
            expired.append(state)

    for state in expired:
        oauth_states.pop(state, None)


# ============================================================
# DISCORD VERIFICATION BUTTONS
# ============================================================

class VerifyButton(Button):
    def __init__(self):
        super().__init__(
            label="Verify / Add YouTube Account",
            style=discord.ButtonStyle.primary,
            custom_id="verify_add_youtube_account",
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        guild = interaction.guild

        if guild is None:
            await interaction.response.send_message(
                "This button can only be used inside the Discord server.",
                ephemeral=True,
            )
            return

        # Prevent rapid double/triple clicks from creating
        # multiple verification tickets.
        user_lock = verification_creation_locks.get(user.id)

        if user_lock is None:
            user_lock = asyncio.Lock()
            verification_creation_locks[user.id] = user_lock

        if user_lock.locked():
            await interaction.response.send_message(
                "⏳ Your verification channel is already being created. Please wait a moment.",
                ephemeral=True,
            )
            return

        async with user_lock:

            existing_channel_id = active_verification_channels.get(user.id)

            if existing_channel_id:
                existing_channel = guild.get_channel(existing_channel_id)

                if existing_channel:
                    await interaction.response.send_message(
                        f"You already have an active verification channel: "
                        f"{existing_channel.mention}",
                        ephemeral=True,
                    )
                    return

                active_verification_channels.pop(user.id, None)

            category = guild.get_channel(TICKET_CATEGORY_ID)

            if category is not None and not isinstance(
                category, discord.CategoryChannel
            ):
                category = None

            bot_member = guild.me

            if bot_member is None:
                await interaction.response.send_message(
                    "I could not find my bot member in this server. Please try again.",
                    ephemeral=True,
                )
                return

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False
                ),
                user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                ),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                ),
            }

            safe_username = re.sub(
                r"[^a-zA-Z0-9-]",
                "-",
                user.name,
            ).strip("-")

            if not safe_username:
                safe_username = "user"

            safe_username = safe_username[:70]

            try:
                channel = await guild.create_text_channel(
                    name=f"verify-{safe_username}",
                    category=category,
                    overwrites=overwrites,
                    reason="YouTube creator milestone verification",
                )

            except discord.DiscordException:
                await interaction.response.send_message(
                    "❌ I could not create your verification channel. "
                    "Please check my Discord permissions and try again.",
                    ephemeral=True,
                )
                return

            # IMPORTANT:
            # This is set immediately after the channel is created,
            # preventing additional verification tickets.
            active_verification_channels[user.id] = channel.id

            state = secrets.token_urlsafe(32)

            oauth_states[state] = {
                "discord_user_id": user.id,
                "guild_id": guild.id,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }

            login_url = (
                f"{PUBLIC_BASE_URL}/login?"
                f"{urlencode({'state': state})}"
            )

            existing_accounts = get_accounts_for_discord(user.id)

            if existing_accounts:
                description = (
                    "You are adding another Google / YouTube account.\n\n"
                    "Any channels found on this Google account will be "
                    "added to your existing verified totals."
                )
            else:
                description = (
                    "Connect the Google account that owns your YouTube "
                    "channel(s).\n\n"
                    "If that Google account owns multiple YouTube channels, "
                    "they will all be checked automatically."
                )

            embed = discord.Embed(
                title="🎥 YouTube Verification",
                description=description,
                color=discord.Color.red(),
            )

            embed.add_field(
                name="How it works",
                value=(
                    "1. Click **Connect YouTube**.\n"
                    "2. Sign in with Google.\n"
                    "3. Approve the requested YouTube access.\n"
                    "4. Your eligible channels are checked automatically."
                ),
                inline=False,
            )

            view = OAuthView(login_url)

            try:
                await channel.send(
                    content=user.mention,
                    embed=embed,
                    view=view,
                )

            except discord.DiscordException:
                active_verification_channels.pop(user.id, None)
                oauth_states.pop(state, None)

                try:
                    await channel.delete(
                        reason="Verification channel setup failed"
                    )
                except discord.DiscordException:
                    pass

                await interaction.response.send_message(
                    "❌ I could not finish setting up your verification "
                    "channel. Please try again.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ Your verification channel is ready: {channel.mention}",
                ephemeral=True,
            )


class VerifyView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())


class OAuthButton(Button):
    def __init__(self, login_url):
        super().__init__(
            label="Connect YouTube",
            style=discord.ButtonStyle.link,
            url=login_url,
        )


class OAuthView(View):
    def __init__(self, login_url):
        super().__init__(timeout=None)
        self.add_item(OAuthButton(login_url))


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class VerificationBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(VerifyView())


bot = VerificationBot(
    command_prefix="$",
    intents=intents,
)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Discord bot is online.")


@bot.command(name="roles")
@commands.is_owner()
async def roles_command(ctx):
    embed = discord.Embed(
        title="🎥 Creator Milestone Verification",
        description=(
            "Connect your YouTube account to verify your "
            "subscriber and view milestones."
        ),
        color=discord.Color.red(),
    )

    embed.set_footer(
        text="Your YouTube statistics are checked through Google OAuth."
    )

    await ctx.send(
        embed=embed,
        view=VerifyView(),
    )


@roles_command.error
async def roles_command_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.send(
            "Only the bot owner can use this command."
        )


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI()


# ============================================================
# GOOGLE OAUTH
# ============================================================

@app.get("/login")
async def login(request: Request, state: str):
    cleanup_expired_oauth_states()

    state_data = oauth_states.get(state)

    if not state_data:
        return HTMLResponse(
            "<h2>Invalid or expired verification session.</h2>",
            status_code=400,
        )

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": (
            "openid email "
            "https://www.googleapis.com/auth/youtube.readonly"
        ),
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
    }

    google_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )

    return RedirectResponse(url=google_url)


@app.get("/callback")
async def callback(request: Request):
    error = request.query_params.get("error")
    state = request.query_params.get("state")

    if error:
        if state:
            oauth_states.pop(state, None)

        return HTMLResponse(
            "<h2>Google verification was cancelled or failed.</h2>"
            f"<p>Error: {html.escape(error)}</p>",
            status_code=400,
        )

    if not state:
        return HTMLResponse(
            "<h2>Missing OAuth state.</h2>",
            status_code=400,
        )

    state_data = oauth_states.pop(state, None)

    if not state_data:
        return HTMLResponse(
            "<h2>This verification session is invalid or has expired.</h2>",
            status_code=400,
        )

    if (
        datetime.now(timezone.utc).timestamp()
        - state_data["created_at"]
        > 600
    ):
        return HTMLResponse(
            "<h2>This verification session has expired.</h2>"
            "<p>Please create a new verification channel in Discord.</p>",
            status_code=400,
        )

    code = request.query_params.get("code")

    if not code:
        return HTMLResponse(
            "<h2>Google did not return an authorization code.</h2>",
            status_code=400,
        )

    discord_user_id = state_data["discord_user_id"]

    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")

        if not access_token:
            raise RuntimeError(
                "Google did not return an access token."
            )

        google_user = get_google_user(access_token)

        google_sub = google_user.get("sub")
        google_email = google_user.get(
            "email",
            "Unknown email",
        )

        if not google_sub:
            raise RuntimeError(
                "Google did not return a stable account ID."
            )

        # A Google account can only belong to one Discord account.
        existing_google_account = get_google_account_by_sub(
            google_sub
        )

        if existing_google_account:

            if str(existing_google_account["discord_user_id"]) == str(
                discord_user_id
            ):
                message = (
                    "This Gmail is already verified on your Discord account."
                )
            else:
                message = (
                    "This Gmail is already verified by another "
                    "Discord account."
                )

            return HTMLResponse(
                "<h2>Google account already verified</h2>"
                f"<p>{html.escape(message)}</p>"
                "<p>You can close this page and return to Discord.</p>",
                status_code=409,
            )

        channels = get_all_youtube_channels(access_token)

        if not channels:
            return HTMLResponse(
                "<h2>No YouTube channels were found.</h2>"
                "<p>Make sure you signed into the Google account "
                "that owns the YouTube channel.</p>",
                status_code=404,
            )

        # A channel can only be verified once anywhere.
        already_verified = []
        new_channels = []

        for channel in channels:
            existing_channel = get_verified_channel(
                channel["channel_id"]
            )

            if existing_channel:
                already_verified.append(
                    channel["channel_title"]
                )
            else:
                new_channels.append(channel)

        if already_verified:
            names = ", ".join(
                html.escape(name)
                for name in already_verified
            )

            return HTMLResponse(
                "<h2>A YouTube channel is already verified</h2>"
                f"<p>The following channel(s) are already verified: "
                f"{names}</p>"
                "<p>No new account was added. Return to Discord and "
                "use a different Google account/channel.</p>",
                status_code=409,
            )

        if not new_channels:
            return HTMLResponse(
                "<h2>No new YouTube channels were found.</h2>"
                "<p>All channels on this Google account have already "
                "been verified.</p>",
                status_code=409,
            )

        account_subscribers, account_views = (
            save_google_account_and_channels(
                discord_user_id=discord_user_id,
                google_sub=google_sub,
                google_email=google_email,
                channels=new_channels,
            )
        )

        combined_subscribers, combined_views = (
            get_aggregate_totals(discord_user_id)
        )

        # Send the role assignment to Discord's event loop.
        asyncio.run_coroutine_threadsafe(
            assign_roles(
                discord_user_id,
                combined_subscribers,
                combined_views,
            ),
            bot.loop,
        )

        # Close the verification ticket after 15 seconds.
        asyncio.run_coroutine_threadsafe(
            close_verification_channel(discord_user_id),
            bot.loop,
        )

        channel_names = "<br>".join(
            f"• {html.escape(channel['channel_title'])}"
            for channel in new_channels
        )

        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>YouTube Verification Complete</title>

                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        max-width: 700px;
                        margin: 50px auto;
                        padding: 20px;
                        line-height: 1.6;
                    }}

                    .box {{
                        border: 1px solid #ddd;
                        border-radius: 12px;
                        padding: 24px;
                    }}
                </style>
            </head>

            <body>
                <div class="box">
                    <h1>✅ Verification Complete</h1>

                    <p>
                        <strong>Google account:</strong>
                        {html.escape(google_email)}
                    </p>

                    <p>
                        <strong>Channels added:</strong>
                    </p>

                    <p>
                        {channel_names}
                    </p>

                    <hr>

                    <p>
                        <strong>This Google account:</strong><br>
                        {account_subscribers:,} subscribers<br>
                        {account_views:,} views
                    </p>

                    <p>
                        <strong>Your combined verified total:</strong><br>
                        {combined_subscribers:,} subscribers<br>
                        {combined_views:,} views
                    </p>

                    <p>
                        You can close this page and return to Discord.
                    </p>
                </div>
            </body>
            </html>
            """,
            status_code=200,
        )

    except requests.RequestException as exc:
        print(f"OAuth/network error: {exc}")

        return HTMLResponse(
            "<h2>Google verification failed.</h2>"
            "<p>A network request to Google failed. Please try again.</p>",
            status_code=502,
        )

    except Exception as exc:
        print(f"Verification error: {exc}")

        return HTMLResponse(
            "<h2>Verification failed.</h2>"
            "<p>An unexpected error occurred. Please return to Discord "
            "and try again.</p>",
            status_code=500,
        )


# ============================================================
# GOOGLE API HELPERS
# ============================================================

def exchange_code_for_token(code):
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def get_google_user(access_token):
    response = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={
            "Authorization": f"Bearer {access_token}"
        },
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def get_all_youtube_channels(access_token):
    channels = []
    page_token = None

    while True:

        params = {
            "part": "snippet,statistics",
            "mine": "true",
            "maxResults": 50,
        }

        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            params=params,
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        for item in data.get("items", []):

            statistics = item.get("statistics", {})
            snippet = item.get("snippet", {})

            channels.append(
                {
                    "channel_id": item["id"],
                    "channel_title": snippet.get(
                        "title",
                        "Unnamed channel",
                    ),
                    "subscriber_count": int(
                        statistics.get(
                            "subscriberCount",
                            0,
                        )
                    ),
                    "view_count": int(
                        statistics.get(
                            "viewCount",
                            0,
                        )
                    ),
                }
            )

        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return channels


# ============================================================
# ROLE ASSIGNMENT
# ============================================================

async def assign_roles(
    discord_user_id,
    total_subscribers,
    total_views,
):
    for guild in bot.guilds:

        member = guild.get_member(discord_user_id)

        if member is None:
            continue

        role_ids_to_add = set()

        for role_name, (threshold, role_id) in ROLE_MAP.items():

            if "SUBS" in role_name:
                if total_subscribers >= threshold:
                    role_ids_to_add.add(role_id)

            elif "VIEWS" in role_name:
                if total_views >= threshold:
                    role_ids_to_add.add(role_id)

        for role_id in role_ids_to_add:

            role = guild.get_role(role_id)

            if role is None:
                print(
                    f"Role {role_id} was not found in guild {guild.id}."
                )
                continue

            try:

                if role not in member.roles:
                    await member.add_roles(
                        role,
                        reason=(
                            "YouTube creator milestone verification"
                        ),
                    )

            except discord.DiscordException as exc:
                print(
                    f"Could not add role {role_id} to {member} "
                    f"in guild {guild.id}: {exc}"
                )


# ============================================================
# CLOSE VERIFICATION CHANNEL
# ============================================================

async def close_verification_channel(discord_user_id):

    await asyncio.sleep(15)

    channel_id = active_verification_channels.pop(
        discord_user_id,
        None,
    )

    if not channel_id:
        return

    for guild in bot.guilds:

        channel = guild.get_channel(channel_id)

        if channel is not None:

            try:
                await channel.delete(
                    reason="YouTube verification completed"
                )

            except discord.DiscordException as exc:
                print(
                    f"Could not delete verification channel "
                    f"{channel_id}: {exc}"
                )

            break


# ============================================================
# BASIC WEB PAGES
# ============================================================

@app.get("/")
async def home():
    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Creator Verification</title>
        </head>

        <body>
            <h1>Creator Verification</h1>
            <p>The verification service is online.</p>

            <p>
                <a href="/privacy">Privacy Policy</a>
            </p>

            <p>
                <a href="/terms">Terms of Service</a>
            </p>
        </body>
        </html>
        """
    )


@app.get("/privacy")
async def privacy():
    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Privacy Policy</title>
        </head>

        <body>
            <h1>Privacy Policy</h1>

            <p>
                This service uses Google OAuth to verify YouTube
                channel statistics.
            </p>

            <p>
                The service stores the Google account identifier,
                Google email, verified YouTube channel identifiers,
                subscriber counts, view counts, Discord user ID,
                and verification time for the purpose of creator
                milestone verification.
            </p>

            <p>
                Google access is requested only for the YouTube
                information required for verification.
                OAuth access tokens are not stored by this application.
            </p>

            <p>
                You may stop using the verification service at any time.
            </p>
        </body>
        </html>
        """
    )


@app.get("/terms")
async def terms():
    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Terms of Service</title>
        </head>

        <body>
            <h1>Terms of Service</h1>

            <p>
                This service is provided for verifying YouTube
                creator milestones within the associated Discord server.
            </p>

            <p>
                Users must only connect Google accounts and YouTube
                channels that they are authorized to verify.
            </p>

            <p>
                The server administrators may change or remove
                verification roles according to the server's rules.
            </p>
        </body>
        </html>
        """
    )


# ============================================================
# RUN BOT + WEB SERVER
# ============================================================

def run_bot():
    bot.run(BOT_TOKEN)


if __name__ == "__main__":

    validate_configuration()

    init_database()

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True,
    )

    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
