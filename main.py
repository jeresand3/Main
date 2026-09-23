```python
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

# These MUST be configured in Render Environment Variables.
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Example:
# https://your-service.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Automatically becomes:
# https://your-service.onrender.com/callback
REDIRECT_URI = f"{PUBLIC_BASE_URL}/callback"

# Discord category where verification channels are created.
TICKET_CATEGORY_ID = 1552294553413746769

# SQLite database location.
#
# Locally this uses:
# ./verification.db
#
# On Render, if you configure a persistent disk and mount it at
# /var/data, set:
#
# DATABASE_FILE=/var/data/verification.db
#
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
# DISCORD
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class VerificationBot(commands.Bot):

    async def setup_hook(self):
        # Register persistent verification button exactly once.
        self.add_view(VerifyView())


bot = VerificationBot(
    command_prefix="$",
    intents=intents
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30
    )

    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def init_database():
    conn = get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                discord_user_id TEXT NOT NULL UNIQUE,

                google_sub TEXT NOT NULL UNIQUE,
                google_email TEXT NOT NULL,

                total_subscribers INTEGER NOT NULL,
                total_views INTEGER NOT NULL,

                verified_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verified_channels (
                channel_id TEXT PRIMARY KEY,

                discord_user_id TEXT NOT NULL,

                channel_title TEXT NOT NULL,

                subscriber_count INTEGER NOT NULL,
                view_count INTEGER NOT NULL,

                verified_at TEXT NOT NULL
            )
        """)

        conn.commit()

    finally:
        conn.close()


init_database()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_verification_by_discord(discord_user_id):
    conn = get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM verifications
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        return cursor.fetchone()

    finally:
        conn.close()


def get_verification_by_google_sub(google_sub):
    conn = get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM verifications
            WHERE google_sub = ?
        """, (google_sub,))

        return cursor.fetchone()

    finally:
        conn.close()


def get_verified_channel(channel_id):
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


def save_verification_and_channels(
    discord_user_id,
    google_sub,
    google_email,
    total_subscribers,
    total_views,
    channels
):
    """
    Save the verification and every YouTube channel
    in one database transaction.

    If anything fails, nothing is partially saved.
    """

    conn = get_connection()

    try:
        cursor = conn.cursor()

        verified_at = datetime.now(
            timezone.utc
        ).isoformat()

        cursor.execute("""
            INSERT INTO verifications (
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

        for channel in channels:

            cursor.execute("""
                INSERT INTO verified_channels (
                    channel_id,
                    discord_user_id,
                    channel_title,
                    subscriber_count,
                    view_count,
                    verified_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                channel["id"],
                str(discord_user_id),
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

# state -> {
#     "discord_user_id": ...,
#     "guild_id": ...,
#     "created_at": ...
# }
oauth_states = {}

# user_id -> verification channel ID
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
        oauth_states.pop(state, None)


# ============================================================
# DISCORD VERIFICATION BUTTON
# ============================================================

class VerifyButton(Button):

    def __init__(self):

        super().__init__(
            label="Verify Creator Milestones",
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

        # Clean old OAuth states.
        cleanup_expired_oauth_states()

        # ----------------------------------------------------
        # Check if Discord account already verified
        # ----------------------------------------------------

        existing = get_verification_by_discord(
            user.id
        )

        if existing:

            await interaction.response.send_message(
                "❌ Your Discord account has already been verified.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # Check active verification channel
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

            # Channel no longer exists.
            active_verification_channels.pop(
                user.id,
                None
            )

        # ----------------------------------------------------
        # Find verification category
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
        # Create private verification channel
        # ----------------------------------------------------

        # Using the Discord ID prevents duplicate names
        # when two users have similar usernames.
        channel_name = f"verify-{user.id}"

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
        # Create secure OAuth state
        # ----------------------------------------------------

        state = secrets.token_urlsafe(32)

        oauth_states[state] = {
            "discord_user_id": user.id,
            "guild_id": guild.id,
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

        await channel.send(
            f"👋 {user.mention}\n\n"
            "Click the button below to connect your "
            "YouTube/Google account.\n\n"
            "Your YouTube channel statistics will only "
            "be used to determine your Discord milestone roles.",
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
# DISCORD BOT READY
# ============================================================

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
# $roles COMMAND
# ============================================================

@bot.command(name="roles")
@commands.is_owner()
async def setup_roles(ctx):

    """
    ONLY THE DISCORD OWNER OF THE BOT APPLICATION
    CAN RUN THIS COMMAND.
    """

    embed = discord.Embed(
        title="🎥 Creator Milestone Verification",
        description=(
            "Connect your YouTube account to verify your "
            "subscriber and view milestones."
        ),
        color=discord.Color.red()
    )

    await ctx.send(
        embed=embed,
        view=VerifyView()
    )


@setup_roles.error
async def setup_roles_error(ctx, error):

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
# GOOGLE LOGIN
# ============================================================

@app.get("/login")
async def login(state: str):

    cleanup_expired_oauth_states()

    # --------------------------------------------------------
    # Validate state
    # --------------------------------------------------------

    if state not in oauth_states:

        return HTMLResponse(
            "Invalid or expired verification session.",
            status_code=400
        )

    # --------------------------------------------------------
    # Validate Google configuration
    # --------------------------------------------------------

    if (
        not GOOGLE_CLIENT_ID
        or not GOOGLE_CLIENT_SECRET
    ):

        return HTMLResponse(
            "Google OAuth is not configured.",
            status_code=500
        )

    # --------------------------------------------------------
    # Google OAuth scopes
    # --------------------------------------------------------

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
async def callback(request: Request):

    # --------------------------------------------------------
    # Check for OAuth errors
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
    # Get OAuth code and state
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

    # --------------------------------------------------------
    # Validate OAuth state
    # --------------------------------------------------------

    state_data = oauth_states.pop(
        state,
        None
    )

    if not state_data:

        return HTMLResponse(
            "This verification session is invalid or expired.",
            status_code=400
        )

    # --------------------------------------------------------
    # Check state expiration
    # --------------------------------------------------------

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
    # Check Discord verification status again
    # --------------------------------------------------------

    existing_discord = (
        get_verification_by_discord(
            discord_user_id
        )
    )

    if existing_discord:

        return HTMLResponse(
            "This Discord account is already verified.",
            status_code=400
        )

    # --------------------------------------------------------
    # Exchange Google authorization code
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
    # Get Google account identity
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
    # DUPLICATE GOOGLE ACCOUNT CHECK
    # --------------------------------------------------------

    existing_google = (
        get_verification_by_google_sub(
            google_sub
        )
    )

    if existing_google:

        return HTMLResponse(
            """
            <h2>Already verified</h2>
            <p>
            This Google account is already verified.
            You cannot verify the same Google account again.
            </p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # Get ALL YouTube channels
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
    # Check every channel for duplicate verification
    # --------------------------------------------------------

    for channel in channels:

        channel_id = channel[
            "id"
        ]

        existing_channel = (
            get_verified_channel(
                channel_id
            )
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
                """,
                status_code=400
            )

    # --------------------------------------------------------
    # Aggregate ALL channels
    # --------------------------------------------------------

    total_subscribers = sum(
        channel["subscribers"]
        for channel in channels
    )

    total_views = sum(
        channel["views"]
        for channel in channels
    )

    print("--------------------------------")
    print("NEW VERIFICATION")
    print("Discord ID:", discord_user_id)
    print("Google:", google_email)
    print("Guild ID:", guild_id)
    print("Channels:", len(channels))

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
        "TOTAL:",
        total_subscribers,
        "subs |",
        total_views,
        "views"
    )

    print("--------------------------------")

    # --------------------------------------------------------
    # Save everything atomically
    # --------------------------------------------------------

    try:

        save_verification_and_channels(
            discord_user_id=
                discord_user_id,

            google_sub=
                google_sub,

            google_email=
                google_email,

            total_subscribers=
                total_subscribers,

            total_views=
                total_views,

            channels=
                channels
        )

    except sqlite3.IntegrityError:

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
    # Assign Discord roles in the correct guild
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
    # Close verification channel later
    # --------------------------------------------------------

    asyncio.run_coroutine_threadsafe(
        close_verification_channel(
            discord_user_id
        ),
        bot.loop
    )

    # --------------------------------------------------------
    # Success page
    # --------------------------------------------------------

    channel_text = "<br>".join(
        f"• {html.escape(channel['title'])}"
        for channel in channels
    )

    safe_email = html.escape(
        google_email
    )

    return HTMLResponse(
        f"""
        <!DOCTYPE html>

        <html>

        <head>
            <title>Verification Complete</title>
        </head>

        <body>

            <h1>✅ Verification Complete</h1>

            <p>
            <strong>Google account:</strong>
            {safe_email}
            </p>

            <p>
            <strong>Channels verified:</strong>
            </p>

            {channel_text}

            <hr>

            <p>
            <strong>Total subscribers:</strong>
            {total_subscribers:,}
            </p>

            <p>
            <strong>Total views:</strong>
            {total_views:,}
            </p>

            <p>
            Your Discord roles are being assigned.
            You can return to Discord now.
            </p>

        </body>

        </html>
        """
    )


# ============================================================
# GOOGLE TOKEN EXCHANGE
# ============================================================

def exchange_code_for_token(code):

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


# ============================================================
# GOOGLE USER INFO
# ============================================================

def get_google_user(access_token):

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


# ============================================================
# GET ALL YOUTUBE CHANNELS
# ============================================================

def get_all_youtube_channels(access_token):

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

            params["pageToken"] = page_token

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
    # ADD ROLES
    # --------------------------------------------------------

    if not roles_to_add:

        print(
            f"{member} did not reach any milestone."
        )

        return

    try:

        await member.add_roles(
            *roles_to_add,
            reason="YouTube milestone verification"
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

    await asyncio.sleep(15)

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
            reason=
                "YouTube verification completed"
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
            The application reads YouTube channel
            subscriber and view statistics through
            Google's YouTube API after the user grants
            permission.
            </p>

            <p>
            No verification can be started from this page.
            Start verification through the PRINT Discord server.
            </p>

            <h2>Privacy</h2>

            <p>
            YouTube data is used only to determine
            creator milestone roles in the Discord server.
            </p>

            <p>
            A complete privacy policy should be published
            before submitting the Google OAuth application
            for production use.
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

    # Start Discord in a background thread.
    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True
    )

    bot_thread.start()

    # Start FastAPI on Render's assigned port.
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
```
