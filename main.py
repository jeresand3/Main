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

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    ""
).rstrip("/")

REDIRECT_URI = f"{PUBLIC_BASE_URL}/callback"

TICKET_CATEGORY_ID = 1552294553413746769

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "verification.db"
)


# ============================================================
# ROLE IDs
# ============================================================

ROLE_MAP = {
    "50K_SUBS": 1552213495909711933,
    "100K_SUBS": 1552242435290038292,
    "1M_SUBS": 1552242872474927165,

    "1M_VIEWS": 1552243824867147827,
    "10M_VIEWS": 1552241736867381269,
    "50M_VIEWS": 1552243153602609172,
    "100M_VIEWS": 1552243231025139754,
    "1B_VIEWS": 1552243361250025534,
}


# ============================================================
# BASIC VALIDATION
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
            "Missing environment variables: "
            + ", ".join(missing)
        )

    if not PUBLIC_BASE_URL.startswith("https://"):

        raise RuntimeError(
            "PUBLIC_BASE_URL must start with https://"
        )


# ============================================================
# DATABASE
# ============================================================

def get_connection():

    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30
    )

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    return conn


def init_database():

    conn = get_connection()

    try:

        cursor = conn.cursor()

        # ----------------------------------------------------
        # NEW MULTI-GOOGLE-ACCOUNT TABLE
        # ----------------------------------------------------
        #
        # One Discord member can now have multiple
        # Google accounts.
        #
        # But each Google account can belong to only
        # one Discord member.
        #
        # ----------------------------------------------------

        cursor.execute("""
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
        """)

        # ----------------------------------------------------
        # VERIFIED YOUTUBE CHANNELS
        # ----------------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verified_channels (
                channel_id TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                google_sub TEXT NOT NULL,
                channel_title TEXT NOT NULL,
                subscriber_count INTEGER NOT NULL,
                view_count INTEGER NOT NULL,
                verified_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # MIGRATE OLD DATABASE
        # ----------------------------------------------------
        #
        # Older versions stored one Google account per
        # Discord member in "verifications".
        #
        # Copy those records into google_accounts so existing
        # verifications continue working.
        #
        # ----------------------------------------------------

        old_table_exists = cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'verifications'
        """).fetchone()

        if old_table_exists:

            old_rows = cursor.execute("""
                SELECT
                    discord_user_id,
                    google_sub,
                    google_email,
                    total_subscribers,
                    total_views,
                    verified_at
                FROM verifications
            """).fetchall()

            for row in old_rows:

                try:

                    cursor.execute("""
                        INSERT OR IGNORE INTO google_accounts (
                            discord_user_id,
                            google_sub,
                            google_email,
                            total_subscribers,
                            total_views,
                            verified_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, row)

                except Exception as e:

                    print(
                        "Database migration warning:",
                        repr(e)
                    )

        # ----------------------------------------------------
        # MIGRATE OLD VERIFIED CHANNELS
        # ----------------------------------------------------
        #
        # Old verified_channels tables did not contain
        # google_sub.
        #
        # SQLite cannot simply add a required column to an
        # existing table, so we inspect the schema.
        #
        # ----------------------------------------------------

        channel_columns = [
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(verified_channels)"
            ).fetchall()
        ]

        if "google_sub" not in channel_columns:

            cursor.execute("""
                ALTER TABLE verified_channels
                ADD COLUMN google_sub TEXT DEFAULT ''
            """)

            # Try to associate existing channels with the
            # Discord member's existing Google account.
            cursor.execute("""
                UPDATE verified_channels
                SET google_sub = (
                    SELECT google_sub
                    FROM google_accounts
                    WHERE google_accounts.discord_user_id =
                          verified_channels.discord_user_id
                    LIMIT 1
                )
                WHERE google_sub = ''
            """)

        conn.commit()

    finally:

        conn.close()


# ============================================================
# GOOGLE ACCOUNT DATABASE HELPERS
# ============================================================

def get_google_account(
    discord_user_id,
    google_sub
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM google_accounts
            WHERE discord_user_id = ?
            AND google_sub = ?
        """, (
            str(discord_user_id),
            google_sub
        ))

        return cursor.fetchone()

    finally:

        conn.close()


def get_google_account_by_sub(
    google_sub
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM google_accounts
            WHERE google_sub = ?
        """, (google_sub,))

        return cursor.fetchone()

    finally:

        conn.close()


def get_accounts_for_discord(
    discord_user_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                google_sub,
                google_email,
                total_subscribers,
                total_views,
                verified_at
            FROM google_accounts
            WHERE discord_user_id = ?
            ORDER BY verified_at ASC
        """, (str(discord_user_id),))

        return cursor.fetchall()

    finally:

        conn.close()


def get_aggregate_totals(
    discord_user_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COALESCE(SUM(total_subscribers), 0),
                COALESCE(SUM(total_views), 0)
            FROM google_accounts
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        row = cursor.fetchone()

        return (
            int(row[0]),
            int(row[1])
        )

    finally:

        conn.close()


def get_verified_channel(
    channel_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM verified_channels
            WHERE channel_id = ?
        """, (channel_id,))

        return cursor.fetchone()

    finally:

        conn.close()


def save_google_account_and_channels(
    discord_user_id,
    google_sub,
    google_email,
    total_subscribers,
    total_views,
    channels
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        verified_at = datetime.now(
            timezone.utc
        ).isoformat()

        # ----------------------------------------------------
        # ADD GOOGLE ACCOUNT
        # ----------------------------------------------------

        cursor.execute("""
            INSERT INTO google_accounts (
                discord_user_id,
                google_sub,
                google_email,
                total_subscribers,
                total_views,
                verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(discord_user_id),
            google_sub,
            google_email,
            total_subscribers,
            total_views,
            verified_at
        ))

        # ----------------------------------------------------
        # ADD CHANNELS
        # ----------------------------------------------------

        for channel in channels:

            cursor.execute("""
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
            """, (
                channel["id"],
                str(discord_user_id),
                google_sub,
                channel["title"],
                channel["subscribers"],
                channel["views"],
                verified_at
            ))

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# OAUTH STATE
# ============================================================

oauth_states = {}

active_verification_channels = {}


def cleanup_expired_oauth_states():

    now = datetime.now(
        timezone.utc
    ).timestamp()

    expired = []

    for state, data in oauth_states.items():

        if now - data["created_at"] > 600:

            expired.append(state)

    for state in expired:

        oauth_states.pop(
            state,
            None
        )


# ============================================================
# DISCORD VERIFICATION BUTTON
# ============================================================

class VerifyButton(Button):

    def __init__(self):

        super().__init__(
            label="Verify / Add YouTube Account",
            style=discord.ButtonStyle.primary,
            custom_id="verify_btn"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        user = interaction.user
        guild = interaction.guild

        if guild is None:

            await interaction.response.send_message(
                "❌ This button can only be used inside the Discord server.",
                ephemeral=True
            )

            return

        cleanup_expired_oauth_states()

        # ----------------------------------------------------
        # ACTIVE VERIFICATION CHANNEL CHECK
        # ----------------------------------------------------

        existing_channel_id = (
            active_verification_channels.get(
                user.id
            )
        )

        if existing_channel_id:

            existing_channel = guild.get_channel(
                existing_channel_id
            )

            if existing_channel:

                await interaction.response.send_message(
                    "❌ You already have an active "
                    f"verification channel: {existing_channel.mention}",
                    ephemeral=True
                )

                return

            active_verification_channels.pop(
                user.id,
                None
            )

        # ----------------------------------------------------
        # FIND CATEGORY
        # ----------------------------------------------------

        category = guild.get_channel(
            TICKET_CATEGORY_ID
        )

        if category is None:

            await interaction.response.send_message(
                "❌ Verification category could not be found.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # CHANNEL NAME
        # ----------------------------------------------------

        username = re.sub(
            r"[^a-zA-Z0-9_-]",
            "-",
            user.name
        ).strip("-").lower()

        if not username:

            username = str(user.id)

        channel_name = f"verify-{username}"[:100]

        # ----------------------------------------------------
        # CREATE PRIVATE CHANNEL
        # ----------------------------------------------------

        try:

            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites={

                    guild.default_role:
                        discord.PermissionOverwrite(
                            view_channel=False
                        ),

                    user:
                        discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True
                        ),

                    guild.me:
                        discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True
                        )
                }
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I don't have permission to create verification channels.",
                ephemeral=True
            )

            return

        except Exception as e:

            print(
                "Channel creation error:",
                repr(e)
            )

            await interaction.response.send_message(
                "❌ Something went wrong while creating your verification channel.",
                ephemeral=True
            )

            return

        active_verification_channels[
            user.id
        ] = channel.id

        # ----------------------------------------------------
        # CREATE OAUTH STATE
        # ----------------------------------------------------

        state = secrets.token_urlsafe(32)

        oauth_states[state] = {
            "discord_user_id":
                user.id,

            "guild_id":
                guild.id,

            "created_at":
                datetime.now(
                    timezone.utc
                ).timestamp()
        }

        login_url = (
            f"{PUBLIC_BASE_URL}/login?"
            + urlencode({
                "state": state
            })
        )

        # ----------------------------------------------------
        # DETERMINE WHETHER THIS IS FIRST OR ADDITIONAL
        # ACCOUNT
        # ----------------------------------------------------

        existing_accounts = get_accounts_for_discord(
            user.id
        )

        if existing_accounts:

            message = (
                f"👋 {user.mention}\n\n"
                "You're adding another Google/YouTube "
                "account to your creator verification.\n\n"
                "Your verified YouTube accounts and channels "
                "will be combined when calculating your "
                "milestone roles.\n\n"
                "For example:\n"
                "25M views on one account + 25M views on "
                "another account = 50M total views."
            )

        else:

            message = (
                f"👋 {user.mention}\n\n"
                "Click the button below to connect your "
                "YouTube/Google account.\n\n"
                "You can add additional Google accounts later "
                "if you have more YouTube channels."
            )

        await channel.send(
            message,
            view=OAuthView(login_url)
        )

        await interaction.response.send_message(
            "✅ Your private verification channel is ready: "
            f"{channel.mention}",
            ephemeral=True
        )


class VerifyView(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

        self.add_item(
            VerifyButton()
        )


class OAuthButton(Button):

    def __init__(self, login_url):

        super().__init__(
            label="Connect YouTube",
            style=discord.ButtonStyle.link,
            url=login_url
        )


class OAuthView(View):

    def __init__(self, login_url):

        super().__init__(
            timeout=None
        )

        self.add_item(
            OAuthButton(login_url)
        )


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

intents.members = True
intents.message_content = True


class VerificationBot(commands.Bot):

    async def setup_hook(self):

        self.add_view(
            VerifyView()
        )


bot = VerificationBot(
    command_prefix="$",
    intents=intents
)


@bot.event
async def on_ready():

    print(
        f"Logged in as: {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"Connected to {len(bot.guilds)} Discord server(s)."
    )


# ============================================================
# $ROLES COMMAND
# ============================================================

@bot.command(name="roles")
@commands.is_owner()
async def setup_roles(ctx):

    embed = discord.Embed(
        title="🎥 Creator Milestone Verification",
        description=(
            "Connect your YouTube accounts to verify your "
            "combined subscriber and view milestones.\n\n"
            "You can add multiple Google accounts if you "
            "own YouTube channels across different accounts."
        ),
        color=discord.Color.red()
    )

    await ctx.send(
        embed=embed,
        view=VerifyView()
    )


@setup_roles.error
async def setup_roles_error(
    ctx,
    error
):

    if isinstance(
        error,
        commands.NotOwner
    ):

        await ctx.send(
            "❌ Only the bot owner can use this command.",
            delete_after=5
        )

        return

    print(
        "$roles command error:",
        repr(error)
    )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# GOOGLE LOGIN
# ============================================================

@app.get("/login")
async def login(state: str):

    cleanup_expired_oauth_states()

    if state not in oauth_states:

        return HTMLResponse(
            "Invalid or expired verification session.",
            status_code=400
        )

    if (
        not GOOGLE_CLIENT_ID
        or not GOOGLE_CLIENT_SECRET
    ):

        return HTMLResponse(
            "Google OAuth is not configured.",
            status_code=500
        )

    scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/youtube.readonly"
    ]

    params = {

        "client_id":
            GOOGLE_CLIENT_ID,

        "redirect_uri":
            REDIRECT_URI,

        "response_type":
            "code",

        "scope":
            " ".join(scopes),

        "state":
            state,

        "access_type":
            "offline",

        "prompt":
            "select_account"
    }

    google_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )

    return RedirectResponse(
        google_url
    )


# ============================================================
# GOOGLE CALLBACK
# ============================================================

@app.get("/callback")
async def callback(
    request: Request
):

    # --------------------------------------------------------
    # OAUTH ERROR
    # --------------------------------------------------------

    error = request.query_params.get(
        "error"
    )

    if error:

        safe_error = html.escape(
            error
        )

        return HTMLResponse(
            f"""
            <h2>Verification cancelled</h2>
            <p>Google returned: {safe_error}</p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # GET CODE + STATE
    # --------------------------------------------------------

    code = request.query_params.get(
        "code"
    )

    state = request.query_params.get(
        "state"
    )

    if not code or not state:

        return HTMLResponse(
            "Missing OAuth code or state.",
            status_code=400
        )

    state_data = oauth_states.pop(
        state,
        None
    )

    if not state_data:

        return HTMLResponse(
            "This verification session is invalid or expired.",
            status_code=400
        )

    created_at = state_data[
        "created_at"
    ]

    if (
        datetime.now(
            timezone.utc
        ).timestamp()
        - created_at
        > 600
    ):

        return HTMLResponse(
            "This verification session expired. "
            "Please start again from Discord.",
            status_code=400
        )

    discord_user_id = state_data[
        "discord_user_id"
    ]

    guild_id = state_data[
        "guild_id"
    ]

    # --------------------------------------------------------
    # GOOGLE TOKEN EXCHANGE
    # --------------------------------------------------------

    try:

        token_data = await asyncio.to_thread(
            exchange_code_for_token,
            code
        )

    except Exception as e:

        print(
            "Google token exchange exception:",
            repr(e)
        )

        return HTMLResponse(
            "Google authorization failed. Please try again.",
            status_code=400
        )

    if "error" in token_data:

        print(
            "Google token error:",
            token_data
        )

        return HTMLResponse(
            "Google authorization failed. Please try again.",
            status_code=400
        )

    access_token = token_data.get(
        "access_token"
    )

    if not access_token:

        return HTMLResponse(
            "Google did not provide an access token.",
            status_code=400
        )

    # --------------------------------------------------------
    # GOOGLE ACCOUNT
    # --------------------------------------------------------

    google_user = await asyncio.to_thread(
        get_google_user,
        access_token
    )

    if not google_user:

        return HTMLResponse(
            "Could not identify your Google account.",
            status_code=400
        )

    google_sub = google_user.get(
        "sub"
    )

    google_email = google_user.get(
        "email",
        "Unknown Google account"
    )

    if not google_sub:

        return HTMLResponse(
            "Google account identification failed.",
            status_code=400
        )

    # --------------------------------------------------------
    # GOOGLE ACCOUNT DUPLICATE CHECK
    # --------------------------------------------------------
    #
    # A Google account can only belong to one Discord member.
    #
    # --------------------------------------------------------

    existing_google = get_google_account_by_sub(
        google_sub
    )

    if existing_google:

        existing_discord_id = existing_google[1]

        if str(existing_discord_id) == str(discord_user_id):

            return HTMLResponse(
                """
                <h2>Google account already added</h2>

                <p>
                This Google account is already connected
                to your Discord verification.
                </p>

                <p>
                Please connect a different Google account
                if you want to add more YouTube channels.
                </p>
                """,
                status_code=400
            )

        return HTMLResponse(
            """
            <h2>Google account already verified</h2>

            <p>
            This Google account is already verified by
            another Discord member.
            </p>

            <p>
            A Google account cannot be claimed by multiple
            Discord members.
            </p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # GET ALL YOUTUBE CHANNELS
    # --------------------------------------------------------

    youtube_result = await asyncio.to_thread(
        get_all_youtube_channels,
        access_token
    )

    if youtube_result["error"]:

        print(
            "YouTube API error:",
            youtube_result["error"]
        )

        return HTMLResponse(
            "Could not retrieve your YouTube channels.",
            status_code=400
        )

    channels = youtube_result[
        "channels"
    ]

    if not channels:

        return HTMLResponse(
            """
            <h2>No YouTube channels found</h2>

            <p>
            The Google account you connected does not
            have a YouTube channel that this authorization
            can access.
            </p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # CHANNEL DUPLICATE CHECK
    # --------------------------------------------------------

    for channel in channels:

        channel_id = channel[
            "id"
        ]

        existing_channel = get_verified_channel(
            channel_id
        )

        if existing_channel:

            safe_title = html.escape(
                channel["title"]
            )

            return HTMLResponse(
                f"""
                <h2>Channel already verified</h2>

                <p>
                The YouTube channel
                <strong>{safe_title}</strong>
                is already verified.
                </p>

                <p>
                A YouTube channel can only be claimed
                by one Discord member.
                </p>
                """,
                status_code=400
            )

    # --------------------------------------------------------
    # TOTALS FOR THIS GOOGLE ACCOUNT
    # --------------------------------------------------------

    account_subscribers = sum(
        channel["subscribers"]
        for channel in channels
    )

    account_views = sum(
        channel["views"]
        for channel in channels
    )

    # --------------------------------------------------------
    # SAVE NEW GOOGLE ACCOUNT + CHANNELS
    # --------------------------------------------------------

    try:

        save_google_account_and_channels(
            discord_user_id=discord_user_id,
            google_sub=google_sub,
            google_email=google_email,
            total_subscribers=account_subscribers,
            total_views=account_views,
            channels=channels
        )

    except sqlite3.IntegrityError as e:

        print(
            "Database integrity error:",
            repr(e)
        )

        return HTMLResponse(
            """
            <h2>Already verified</h2>

            <p>
            This Google account or one of these
            YouTube channels has already been verified.
            </p>
            """,
            status_code=400
        )

    except Exception as e:

        print(
            "Database error:",
            repr(e)
        )

        return HTMLResponse(
            "The verification could not be saved. Please try again.",
            status_code=500
        )

    # --------------------------------------------------------
    # CALCULATE COMBINED TOTALS
    # --------------------------------------------------------

    total_subscribers, total_views = get_aggregate_totals(
        discord_user_id
    )

    # --------------------------------------------------------
    # LOG VERIFICATION
    # --------------------------------------------------------

    print(
        "========================================"
    )

    print(
        "NEW GOOGLE ACCOUNT VERIFIED"
    )

    print(
        "Discord ID:",
        discord_user_id
    )

    print(
        "Google:",
        google_email
    )

    print(
        "Guild ID:",
        guild_id
    )

    print(
        "Channels added:",
        len(channels)
    )

    for channel in channels:

        print(
            "-",
            channel["title"],
            "|",
            channel["subscribers"],
            "subs |",
            channel["views"],
            "views"
        )

    print(
        "THIS ACCOUNT:",
        account_subscribers,
        "subs |",
        account_views,
        "views"
    )

    print(
        "COMBINED TOTAL:",
        total_subscribers,
        "subs |",
        total_views,
        "views"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # ASSIGN COMBINED ROLES
    # --------------------------------------------------------

    asyncio.run_coroutine_threadsafe(
        assign_roles(
            guild_id,
            discord_user_id,
            total_subscribers,
            total_views
        ),
        bot.loop
    )

    # --------------------------------------------------------
    # CLOSE VERIFICATION CHANNEL
    # --------------------------------------------------------

    asyncio.run_coroutine_threadsafe(
        close_verification_channel(
            discord_user_id
        ),
        bot.loop
    )

    # --------------------------------------------------------
    # HTML RESULT
    # --------------------------------------------------------

    channel_text = "<br>".join(
        f"• {html.escape(channel['title'])}"
        for channel in channels
    )

    safe_email = html.escape(
        google_email
    )

    existing_accounts = get_accounts_for_discord(
        discord_user_id
    )

    account_count = len(
        existing_accounts
    )

    return HTMLResponse(
        f"""
        <!DOCTYPE html>

        <html>

        <head>

            <meta charset="UTF-8">

            <meta
                name="viewport"
                content="width=device-width, initial-scale=1.0"
            >

            <title>Verification Complete</title>

        </head>

        <body>

            <h1>✅ Verification Complete</h1>

            <p>
            <strong>Google account added:</strong>
            {safe_email}
            </p>

            <p>
            <strong>Total Google accounts linked:</strong>
            {account_count}
            </p>

            <h3>Channels added</h3>

            {channel_text}

            <hr>

            <h3>Combined Creator Totals</h3>

            <p>
            <strong>Total subscribers:</strong>
            {total_subscribers:,}
            </p>

            <p>
            <strong>Total views:</strong>
            {total_views:,}
            </p>

            <p>
            Your Discord milestone roles are being updated
            using your combined verified totals.
            </p>

            <p>
            You can return to Discord now.
            </p>

        </body>

        </html>
        """
    )


# ============================================================
# GOOGLE / YOUTUBE HELPERS
# ============================================================

def exchange_code_for_token(
    code
):

    response = requests.post(
        "https://oauth2.googleapis.com/token",

        data={

            "code":
                code,

            "client_id":
                GOOGLE_CLIENT_ID,

            "client_secret":
                GOOGLE_CLIENT_SECRET,

            "redirect_uri":
                REDIRECT_URI,

            "grant_type":
                "authorization_code"
        },

        timeout=20
    )

    response.raise_for_status()

    return response.json()


def get_google_user(
    access_token
):

    response = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",

        headers={
            "Authorization":
                f"Bearer {access_token}"
        },

        timeout=20
    )

    if response.status_code != 200:

        print(
            "Google userinfo error:",
            response.text
        )

        return None

    return response.json()


def get_all_youtube_channels(
    access_token
):

    channels = []

    page_token = None

    while True:

        params = {

            "part":
                "snippet,statistics",

            "mine":
                "true",

            "maxResults":
                50
        }

        if page_token:

            params[
                "pageToken"
            ] = page_token

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",

            headers={
                "Authorization":
                    f"Bearer {access_token}"
            },

            params=params,

            timeout=20
        )

        if response.status_code != 200:

            return {
                "channels": [],
                "error": response.text
            }

        data = response.json()

        for item in data.get(
            "items",
            []
        ):

            statistics = item.get(
                "statistics",
                {}
            )

            snippet = item.get(
                "snippet",
                {}
            )

            channel_id = item.get(
                "id"
            )

            if not channel_id:

                continue

            channel = {

                "id":
                    channel_id,

                "title":
                    snippet.get(
                        "title",
                        "Unknown Channel"
                    ),

                "subscribers":
                    int(
                        statistics.get(
                            "subscriberCount",
                            0
                        )
                    ),

                "views":
                    int(
                        statistics.get(
                            "viewCount",
                            0
                        )
                    )
            }

            channels.append(
                channel
            )

        page_token = data.get(
            "nextPageToken"
        )

        if not page_token:

            break

    return {

        "channels":
            channels,

        "error":
            None
    }


# ============================================================
# ROLE ASSIGNMENT
# ============================================================

async def assign_roles(
    guild_id,
    discord_user_id,
    subscribers,
    views
):

    guild = bot.get_guild(
        guild_id
    )

    if guild is None:

        print(
            "Could not find Discord guild:",
            guild_id
        )

        return

    member = guild.get_member(
        discord_user_id
    )

    if member is None:

        try:

            member = await guild.fetch_member(
                discord_user_id
            )

        except discord.NotFound:

            print(
                "Could not find Discord member:",
                discord_user_id
            )

            return

        except Exception as e:

            print(
                "Could not fetch Discord member:",
                repr(e)
            )

            return

    roles_to_add = []

    # --------------------------------------------------------
    # SUBSCRIBER MILESTONES
    # --------------------------------------------------------

    subscriber_milestones = [

        (
            50_000,
            "50K_SUBS"
        ),

        (
            100_000,
            "100K_SUBS"
        ),

        (
            1_000_000,
            "1M_SUBS"
        )
    ]

    for threshold, role_key in subscriber_milestones:

        if subscribers >= threshold:

            role = guild.get_role(
                ROLE_MAP[role_key]
            )

            if role:

                roles_to_add.append(
                    role
                )

    # --------------------------------------------------------
    # VIEW MILESTONES
    # --------------------------------------------------------

    view_milestones = [

        (
            1_000_000,
            "1M_VIEWS"
        ),

        (
            10_000_000,
            "10M_VIEWS"
        ),

        (
            50_000_000,
            "50M_VIEWS"
        ),

        (
            100_000_000,
            "100M_VIEWS"
        ),

        (
            1_000_000_000,
            "1B_VIEWS"
        )
    ]

    for threshold, role_key in view_milestones:

        if views >= threshold:

            role = guild.get_role(
                ROLE_MAP[role_key]
            )

            if role:

                roles_to_add.append(
                    role
                )

    # --------------------------------------------------------
    # ASSIGN
    # --------------------------------------------------------

    if not roles_to_add:

        print(
            f"{member} did not reach any milestone."
        )

        return

    try:

        await member.add_roles(
            *roles_to_add,
            reason="Combined YouTube milestone verification"
        )

        print(
            f"Added {len(roles_to_add)} roles "
            f"to {member}"
        )

    except discord.Forbidden:

        print(
            "ERROR: Bot cannot assign one or more roles."
        )

        print(
            "Make sure the bot's highest role is "
            "above the milestone roles."
        )

    except Exception as e:

        print(
            "Role assignment error:",
            repr(e)
        )


# ============================================================
# CLOSE VERIFICATION CHANNEL
# ============================================================

async def close_verification_channel(
    discord_user_id
):

    await asyncio.sleep(
        15
    )

    channel_id = (
        active_verification_channels.pop(
            discord_user_id,
            None
        )
    )

    if not channel_id:

        return

    channel = bot.get_channel(
        channel_id
    )

    if channel is None:

        return

    try:

        await channel.delete(
            reason="YouTube verification completed"
        )

    except discord.NotFound:

        pass

    except discord.Forbidden:

        print(
            "Bot does not have permission "
            "to delete verification channel."
        )

    except Exception as e:

        print(
            "Could not delete verification channel:",
            repr(e)
        )


# ============================================================
# FASTAPI HOME PAGE
# ============================================================

@app.get("/")
async def homepage():

    return HTMLResponse(
        """
        <!DOCTYPE html>

        <html>

        <head>

            <meta charset="UTF-8">

            <meta
                name="viewport"
                content="width=device-width, initial-scale=1.0"
            >

            <title>PRINT Creator Verification</title>

        </head>

        <body>

            <h1>PRINT Creator Verification</h1>

            <p>
            This website is used by members of the
            PRINT Discord server to verify YouTube
            creator milestones.
            </p>

            <p>
            Members may connect multiple Google accounts.
            Verified YouTube channel statistics are combined
            when determining creator milestone roles.
            </p>

            <p>
            The application reads YouTube channel
            subscriber and view statistics through
            Google's YouTube API after the user grants
            permission.
            </p>

            <p>
            No verification can be started from this page.
            Start verification through the PRINT Discord server.
            </p>

            <p>
            <a href="/privacy">
            Read our Privacy Policy
            </a>
            </p>

            <p>
            <a href="/terms">
            Terms of Service
            </a>
            </p>

        </body>

        </html>
        """
    )


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get("/privacy")
async def privacy_policy():

    return HTMLResponse(
        """
        <!DOCTYPE html>

        <html>

        <head>

            <meta charset="UTF-8">

            <meta
                name="viewport"
                content="width=device-width, initial-scale=1.0"
            >

            <title>
            PRINT Creator Verification - Privacy Policy
            </title>

        </head>

        <body>

            <h1>Privacy Policy</h1>

            <p>
            <strong>Last updated:</strong>
            September 24, 2026
            </p>

            <h2>1. What this application does</h2>

            <p>
            PRINT Creator Verification is a Discord verification
            application that allows members of the PRINT Discord
            server to verify YouTube creator milestones.
            </p>

            <p>
            Members may connect multiple Google accounts.
            YouTube statistics from verified channels associated
            with those accounts may be combined to determine
            milestone roles.
            </p>

            <h2>2. Information we collect</h2>

            <p>
            When you use the verification system, the application
            may receive and store:
            </p>

            <ul>

                <li>Your Discord user ID</li>

                <li>Your Google account identifier</li>

                <li>Your Google account email address</li>

                <li>Your YouTube channel ID</li>

                <li>Your YouTube channel name</li>

                <li>Your YouTube subscriber count</li>

                <li>Your YouTube channel view count</li>

                <li>The date and time of verification</li>

            </ul>

            <h2>3. How your information is used</h2>

            <p>
            Information obtained through Google and YouTube is
            used to verify creator milestones and assign the
            corresponding roles within the PRINT Discord server.
            </p>

            <p>
            When multiple Google accounts are connected to the
            same Discord member, verified channel statistics may
            be combined for milestone calculations.
            </p>

            <p>
            The information is not used to provide advertising,
            sell personal information, or build advertising profiles.
            </p>

            <h2>4. Google and YouTube access</h2>

            <p>
            The application uses Google's OAuth authorization system.
            You choose whether to grant the requested permissions.
            </p>

            <p>
            The application requests access necessary to identify
            your Google account and retrieve YouTube channel
            information required for verification.
            </p>

            <p>
            You can revoke the application's access to your Google
            account through your Google Account security settings.
            </p>

            <h2>5. Data storage</h2>

            <p>
            Verification information is stored so that a verified
            Google account or YouTube channel cannot simply be
            claimed repeatedly.
            </p>

            <h2>6. Data sharing</h2>

            <p>
            Verification information is not sold or shared with
            third parties for advertising purposes.
            </p>

            <p>
            The application may interact with Google, YouTube,
            Discord, and the hosting provider as necessary to
            operate the verification system.
            </p>

            <h2>7. Data deletion</h2>

            <p>
            If you want your stored verification information removed,
            contact the administrators of the PRINT Discord server.
            Requests can be reviewed and handled by the application
            administrators.
            </p>

            <h2>8. Changes to this policy</h2>

            <p>
            This Privacy Policy may be updated when the verification
            system or its data practices change.
            </p>

            <h2>9. Contact</h2>

            <p>
            For questions about this Privacy Policy or the
            verification system, contact the administrators of the
            PRINT Discord server.
            </p>

            <p>
            <a href="/">
            Return to PRINT Creator Verification
            </a>
            </p>

        </body>

        </html>
        """
    )


# ============================================================
# TERMS OF SERVICE
# ============================================================

@app.get("/terms")
async def terms_of_service():

    return HTMLResponse(
        """
        <!DOCTYPE html>

        <html>

        <head>

            <meta charset="UTF-8">

            <meta
                name="viewport"
                content="width=device-width, initial-scale=1.0"
            >

            <title>
            PRINT Creator Verification - Terms of Service
            </title>

        </head>

        <body>

            <h1>Terms of Service</h1>

            <p>
            <strong>Last updated:</strong>
            September 24, 2026
            </p>

            <h2>1. Use of the service</h2>

            <p>
            PRINT Creator Verification is provided to members
            of the PRINT Discord server to verify YouTube creator
            milestones.
            </p>

            <h2>2. Accurate information</h2>

            <p>
            Users must only connect Google accounts and YouTube
            channels that they are authorized to use.
            </p>

            <h2>3. Verification</h2>

            <p>
            Milestone roles are based on the YouTube information
            available through the authorized Google accounts.
            </p>

            <p>
            Multiple authorized Google accounts may be connected
            to the same Discord member. Verified channel statistics
            may be combined when determining milestone roles.
            </p>

            <h2>4. Abuse</h2>

            <p>
            Attempts to claim another person's Google account or
            YouTube channel, bypass verification protections, or
            otherwise abuse the verification system may result in
            verification being rejected or removed.
            </p>

            <h2>5. Changes</h2>

            <p>
            The PRINT administrators may change or discontinue
            the verification system when necessary.
            </p>

            <p>
            <a href="/">
            Return to PRINT Creator Verification
            </a>
            </p>

        </body>

        </html>
        """
    )


# ============================================================
# RUN DISCORD BOT
# ============================================================

def run_bot():

    try:

        bot.run(
            BOT_TOKEN
        )

    except Exception as e:

        print(
            "Discord bot stopped:",
            repr(e)
        )


# ============================================================
# START EVERYTHING
# ============================================================

if __name__ == "__main__":

    validate_configuration()

    init_database()

    print(
        "===================================="
    )

    print(
        "PRINT verification system starting"
    )

    print(
        "Public URL:",
        PUBLIC_BASE_URL
    )

    print(
        "Redirect URI:",
        REDIRECT_URI
    )

    print(
        "Database:",
        DATABASE_FILE
    )

    print(
        "===================================="
    )

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True
    )

    bot_thread.start()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8000"
            )
        )
    )
