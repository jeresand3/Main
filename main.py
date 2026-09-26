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

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "verification.db"
)

# OAuth verification sessions last 24 hours.
# The same session can be reused unlimited times
# while the ticket is active.
OAUTH_SESSION_LIFETIME = 86400


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
        # GOOGLE ACCOUNTS
        #
        # Multiple Google accounts may belong to the same
        # Discord account.
        #
        # google_sub remains globally unique so the same
        # Google account cannot be verified twice.
        # ----------------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS google_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                google_sub TEXT NOT NULL UNIQUE,
                google_email TEXT NOT NULL,
                verified_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # VERIFIED YOUTUBE CHANNELS
        #
        # Every YouTube channel can only be verified once.
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # OLD TABLE
        #
        # Kept for compatibility with an older database.
        # It is no longer used for new verification records.
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # ACTIVE VERIFICATION TICKETS
        #
        # This makes the "one ticket per member" rule survive
        # a Render restart.
        # ----------------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_verification_tickets (
                discord_user_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # REUSABLE OAUTH SESSIONS
        #
        # One reusable OAuth state belongs to one ticket.
        # It is NOT deleted after the first successful login.
        #
        # This is the major fix for:
        # "Invalid or expired verification session."
        # ----------------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verification_sessions (
                state TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # MIGRATE OLD VERIFICATIONS
        # ----------------------------------------------------

        cursor.execute("""
            INSERT OR IGNORE INTO google_accounts (
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            )
            SELECT
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            FROM verifications
        """)

        conn.commit()

    finally:

        conn.close()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_google_account_by_sub(google_sub):

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


def get_accounts_for_discord(discord_user_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM google_accounts
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        return cursor.fetchall()

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


def get_aggregate_totals(discord_user_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COALESCE(SUM(subscriber_count), 0),
                COALESCE(SUM(view_count), 0)
            FROM verified_channels
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        row = cursor.fetchone()

        return (
            int(row[0] or 0),
            int(row[1] or 0)
        )

    finally:

        conn.close()


def get_active_ticket(discord_user_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                guild_id,
                channel_id,
                created_at
            FROM active_verification_tickets
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        return cursor.fetchone()

    finally:

        conn.close()


def create_active_ticket(
    discord_user_id,
    guild_id,
    channel_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

        cursor.execute("""
            INSERT INTO active_verification_tickets (
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            str(discord_user_id),
            str(guild_id),
            str(channel_id),
            created_at
        ))

        conn.commit()

    finally:

        conn.close()


def remove_active_ticket(discord_user_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM active_verification_tickets
            WHERE discord_user_id = ?
        """, (str(discord_user_id),))

        conn.commit()

    finally:

        conn.close()


def save_google_account_and_channels(
    discord_user_id,
    google_sub,
    google_email,
    channels
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        verified_at = datetime.now(
            timezone.utc
        ).isoformat()

        # ----------------------------------------------------
        # Save Google account.
        # ----------------------------------------------------

        cursor.execute("""
            INSERT INTO google_accounts (
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            str(discord_user_id),
            google_sub,
            google_email,
            verified_at
        ))

        # ----------------------------------------------------
        # Save every YouTube channel owned by this account.
        # ----------------------------------------------------

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
# OAUTH SESSION DATABASE HELPERS
# ============================================================

def cleanup_expired_oauth_sessions():

    cutoff = (
        datetime.now(
            timezone.utc
        ).timestamp()
        - OAUTH_SESSION_LIFETIME
    )

    conn = get_connection()

    try:

        cursor = conn.cursor()

        # SQLite stores the timestamps as ISO strings.
        # We therefore inspect the sessions in Python.
        cursor.execute("""
            SELECT state, created_at
            FROM verification_sessions
        """)

        rows = cursor.fetchall()

        expired_states = []

        for state, created_at in rows:

            try:

                created_timestamp = datetime.fromisoformat(
                    created_at
                ).timestamp()

            except Exception:

                expired_states.append(state)
                continue

            if created_timestamp < cutoff:

                expired_states.append(state)

        for state in expired_states:

            cursor.execute("""
                DELETE FROM verification_sessions
                WHERE state = ?
            """, (state,))

        conn.commit()

    finally:

        conn.close()


def get_verification_session(state):

    cleanup_expired_oauth_sessions()

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                state,
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            FROM verification_sessions
            WHERE state = ?
        """, (state,))

        return cursor.fetchone()

    finally:

        conn.close()


def create_or_get_verification_session(
    discord_user_id,
    guild_id,
    channel_id
):

    cleanup_expired_oauth_sessions()

    conn = get_connection()

    try:

        cursor = conn.cursor()

        # ----------------------------------------------------
        # Check whether this ticket already has a reusable
        # OAuth session.
        # ----------------------------------------------------

        cursor.execute("""
            SELECT state
            FROM verification_sessions
            WHERE channel_id = ?
        """, (str(channel_id),))

        existing = cursor.fetchone()

        if existing:

            return existing[0]

        # ----------------------------------------------------
        # Create a new reusable state.
        # ----------------------------------------------------

        state = secrets.token_urlsafe(32)

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

        cursor.execute("""
            INSERT INTO verification_sessions (
                state,
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            state,
            str(discord_user_id),
            str(guild_id),
            str(channel_id),
            created_at
        ))

        conn.commit()

        return state

    finally:

        conn.close()


def delete_verification_session_for_channel(
    channel_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM verification_sessions
            WHERE channel_id = ?
        """, (str(channel_id),))

        conn.commit()

    finally:

        conn.close()


# ============================================================
# OLD IN-MEMORY LOCKS
# ============================================================

# These locks only prevent two nearly simultaneous button
# clicks from creating two channels in the same bot process.
# The database remains the actual source of truth.
verification_creation_locks = {}


# ============================================================
# DISCORD TICKET CONTROLS
# ============================================================

class ConnectYouTubeButton(Button):

    def __init__(self, login_url):

        super().__init__(
            label="Connect YouTube",
            style=discord.ButtonStyle.link,
            url=login_url
        )


class ConnectYouTubeView(View):

    def __init__(self, login_url):

        super().__init__(
            timeout=None
        )

        self.add_item(
            ConnectYouTubeButton(
                login_url
            )
        )


class CloseTicketButton(Button):

    def __init__(self):

        super().__init__(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            custom_id="close_verification_ticket"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        user = interaction.user
        channel = interaction.channel

        if channel is None:

            await interaction.response.send_message(
                "❌ This button can only be used inside a verification ticket.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # BOT OWNER CANNOT BE REMOVED.
        # ----------------------------------------------------

        try:

            if await bot.is_owner(user):

                await interaction.response.send_message(
                    "🛡️ The bot owner cannot be removed from the verification ticket.",
                    ephemeral=True
                )

                return

        except Exception as e:

            print(
                "Owner check error:",
                repr(e)
            )

        try:

            # ------------------------------------------------
            # Remove this member's access to the ticket.
            # ------------------------------------------------

            await channel.set_permissions(
                user,
                view_channel=False,
                send_messages=False,
                read_message_history=False
            )

            # ------------------------------------------------
            # Delete the active-ticket database record.
            # ------------------------------------------------

            remove_active_ticket(
                user.id
            )

            # ------------------------------------------------
            # Delete the reusable OAuth session.
            # ------------------------------------------------

            delete_verification_session_for_channel(
                channel.id
            )

            await interaction.response.send_message(
                "✅ Your verification ticket has been closed.",
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I don't have permission to close this ticket.",
                ephemeral=True
            )

        except Exception as e:

            print(
                "Close ticket error:",
                repr(e)
            )

            await interaction.response.send_message(
                "❌ Something went wrong while closing the ticket.",
                ephemeral=True
            )


class CloseTicketView(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

        self.add_item(
            CloseTicketButton()
        )


async def send_verification_controls(
    discord_user_id,
    guild_id,
    channel
):

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # This creates ONE reusable OAuth state for this ticket.
    # Pressing Connect YouTube multiple times uses the same
    # session instead of invalidating it after the first use.
    # --------------------------------------------------------

    state = create_or_get_verification_session(
        discord_user_id,
        guild_id,
        channel.id
    )

    login_url = get_login_url(
        state
    )

    # --------------------------------------------------------
    # CONNECT YOUTUBE MESSAGE
    # --------------------------------------------------------

    await channel.send(
        "🔗 **Connect YouTube**\n\n"
        "Click the button below to connect a Google/YouTube "
        "account. You can use this button again to add another "
        "account. Your accounts and channels will be combined "
        "for milestone roles.",
        view=ConnectYouTubeView(
            login_url
        )
    )

    # --------------------------------------------------------
    # CLOSE TICKET MESSAGE
    # --------------------------------------------------------

    await channel.send(
        "🎫 **Finished verifying?**\n\n"
        "Press the button below to leave this verification ticket.",
        view=CloseTicketView()
    )


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

        # ----------------------------------------------------
        # ONE TICKET PER MEMBER
        # ----------------------------------------------------

        lock = verification_creation_locks.setdefault(
            user.id,
            asyncio.Lock()
        )

        async with lock:

            existing_ticket = get_active_ticket(
                user.id
            )

            if existing_ticket:

                existing_guild_id = existing_ticket[0]
                existing_channel_id = existing_ticket[1]

                # Make sure the stored ticket belongs to this
                # Discord server.
                if str(existing_guild_id) == str(guild.id):

                    existing_channel = guild.get_channel(
                        int(existing_channel_id)
                    )

                    if existing_channel:

                        await interaction.response.send_message(
                            "❌ You already have an active "
                            f"verification channel: "
                            f"{existing_channel.mention}",
                            ephemeral=True
                        )

                        return

                    # The channel was deleted manually.
                    # Remove the stale database record.
                    remove_active_ticket(
                        user.id
                    )

                else:

                    # A stale ticket exists for another guild.
                    # Remove it so it cannot block verification.
                    remove_active_ticket(
                        user.id
                    )

            category = guild.get_channel(
                TICKET_CATEGORY_ID
            )

            if category is None:

                await interaction.response.send_message(
                    "❌ Verification category could not be found.",
                    ephemeral=True
                )

                return

            username = re.sub(
                r"[^a-zA-Z0-9_-]",
                "-",
                user.name
            ).strip("-").lower()

            if not username:

                username = str(
                    user.id
                )

            channel_name = f"verify-{username}"[:100]

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

            # ------------------------------------------------
            # Save the active ticket BEFORE sending controls.
            # ------------------------------------------------

            try:

                create_active_ticket(
                    user.id,
                    guild.id,
                    channel.id
                )

            except sqlite3.IntegrityError:

                # Extremely unlikely race protection.
                # Delete the newly-created channel because another
                # ticket already won the database race.
                try:

                    await channel.delete(
                        reason="Duplicate verification ticket prevented"
                    )

                except Exception:
                    pass

                await interaction.response.send_message(
                    "❌ You already have an active verification ticket.",
                    ephemeral=True
                )

                return

            # ------------------------------------------------
            # Create the reusable OAuth session and controls.
            # ------------------------------------------------

            try:

                await send_verification_controls(
                    user.id,
                    guild.id,
                    channel
                )

            except Exception as e:

                print(
                    "Could not send verification controls:",
                    repr(e)
                )

                # Clean up if controls could not be created.
                remove_active_ticket(
                    user.id
                )

                delete_verification_session_for_channel(
                    channel.id
                )

                try:

                    await channel.delete(
                        reason="Verification controls could not be created"
                    )

                except Exception:
                    pass

                await interaction.response.send_message(
                    "❌ Something went wrong while setting up your verification ticket.",
                    ephemeral=True
                )

                return

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

        self.add_view(
            CloseTicketView()
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
# $roles COMMAND
# ============================================================

@bot.command(name="roles")
@commands.is_owner()
async def setup_roles(ctx):

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

    # --------------------------------------------------------
    # The state MUST exist in the database.
    # --------------------------------------------------------

    state_data = get_verification_session(
        state
    )

    if not state_data:

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
            <p>You can return to Discord and press
            Connect YouTube again.</p>
            """,
            status_code=400
        )

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
    # IMPORTANT FIX:
    #
    # DO NOT pop/delete the state here.
    #
    # The same state belongs to the ticket and is intentionally
    # reusable for additional Google accounts.
    # --------------------------------------------------------

    state_data = get_verification_session(
        state
    )

    if not state_data:

        return HTMLResponse(
            "This verification session is invalid or expired. "
            "Please return to your Discord verification ticket "
            "and press Connect YouTube again.",
            status_code=400
        )

    (
        saved_state,
        discord_user_id,
        guild_id,
        ticket_channel_id,
        created_at
    ) = state_data

    # --------------------------------------------------------
    # Confirm the session has not expired.
    # --------------------------------------------------------

    try:

        created_timestamp = datetime.fromisoformat(
            created_at
        ).timestamp()

    except Exception:

        delete_verification_session_for_channel(
            ticket_channel_id
        )

        return HTMLResponse(
            "This verification session is invalid. "
            "Please return to Discord and press Connect YouTube again.",
            status_code=400
        )

    if (
        datetime.now(
            timezone.utc
        ).timestamp()
        - created_timestamp
        > OAUTH_SESSION_LIFETIME
    ):

        delete_verification_session_for_channel(
            ticket_channel_id
        )

        return HTMLResponse(
            "This verification session expired. "
            "Please return to Discord and press Connect YouTube again.",
            status_code=400
        )

    # --------------------------------------------------------
    # Confirm the ticket still exists.
    # --------------------------------------------------------

    active_ticket = get_active_ticket(
        discord_user_id
    )

    if not active_ticket:

        return HTMLResponse(
            "This verification ticket is no longer active. "
            "Please start a new verification from Discord.",
            status_code=400
        )

    # --------------------------------------------------------
    # Make sure the callback is still pointing at the same
    # ticket.
    # --------------------------------------------------------

    if str(active_ticket[1]) != str(ticket_channel_id):

        return HTMLResponse(
            "This verification session no longer matches "
            "your active verification ticket.",
            status_code=400
        )

    # --------------------------------------------------------
    # EXCHANGE GOOGLE AUTHORIZATION CODE
    #
    # The authorization CODE itself is one-time-use.
    # That is normal.
    #
    # The reusable thing is our application state/session.
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
    # IDENTIFY GOOGLE ACCOUNT
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
    # SAME GOOGLE ACCOUNT CANNOT BE VERIFIED TWICE
    # --------------------------------------------------------

    existing_google = (
        get_google_account_by_sub(
            google_sub
        )
    )

    if existing_google:

        safe_email = html.escape(
            google_email
        )

        return HTMLResponse(
            f"""
            <h2>Already verified</h2>

            <p>
            This Gmail is already verified.
            </p>

            <p>
            <strong>{safe_email}</strong>
            </p>

            <p>
            Please return to Discord and connect a
            different Google account.
            </p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # GET ALL YOUTUBE CHANNELS FOR THIS GOOGLE ACCOUNT
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
            "Could not retrieve your YouTube channels. "
            "Please try again.",
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

            <p>
            Please return to Discord and try another account.
            </p>
            """,
            status_code=400
        )

    # --------------------------------------------------------
    # SAME YOUTUBE CHANNEL CANNOT BE VERIFIED TWICE
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

                <p>
                Please return to Discord and connect
                another account or channel.
                </p>
                """,
                status_code=400
            )

    # --------------------------------------------------------
    # CALCULATE THIS ACCOUNT'S TOTAL
    # --------------------------------------------------------

    total_subscribers = sum(
        channel["subscribers"]
        for channel in channels
    )

    total_views = sum(
        channel["views"]
        for channel in channels
    )

    # --------------------------------------------------------
    # LOG VERIFICATION
    # --------------------------------------------------------

    print(
        "--------------------------------"
    )

    print(
        "NEW GOOGLE ACCOUNT VERIFICATION"
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
        "Ticket Channel ID:",
        ticket_channel_id
    )

    print(
        "Channels:",
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
        "THIS ACCOUNT TOTAL:",
        total_subscribers,
        "subs |",
        total_views,
        "views"
    )

    print(
        "--------------------------------"
    )

    # --------------------------------------------------------
    # SAVE GOOGLE ACCOUNT + CHANNELS
    # --------------------------------------------------------

    try:

        save_google_account_and_channels(
            discord_user_id=discord_user_id,
            google_sub=google_sub,
            google_email=google_email,
            channels=channels
        )

    except sqlite3.IntegrityError:

        return HTMLResponse(
            """
            <h2>Already verified</h2>

            <p>
            This Google account or one of these
            YouTube channels has already been verified.
            </p>

            <p>
            Please return to Discord and connect
            a different account.
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
            "The verification could not be saved. "
            "Please try again.",
            status_code=500
        )

    # --------------------------------------------------------
    # GET COMBINED TOTALS FROM ALL ACCOUNTS/CHANNELS
    # BELONGING TO THIS DISCORD MEMBER.
    #
    # Example:
    #
    # Account A = 25M views
    # Account B = 25M views
    #
    # Combined = 50M views
    #
    # Therefore the 50M role is assigned.
    # --------------------------------------------------------

    combined_subscribers, combined_views = (
        get_aggregate_totals(
            discord_user_id
        )
    )

    print(
        "COMBINED TOTAL:",
        combined_subscribers,
        "subs |",
        combined_views,
        "views"
    )

    # --------------------------------------------------------
    # ASSIGN ALL CURRENTLY QUALIFIED ROLES
    # --------------------------------------------------------

    future = asyncio.run_coroutine_threadsafe(
        assign_roles(
            guild_id,
            discord_user_id,
            combined_subscribers,
            combined_views
        ),
        bot.loop
    )

    try:

        # Wait for role assignment to finish so the web page
        # accurately reports that roles were processed.
        future.result(
            timeout=20
        )

    except Exception as e:

        print(
            "Role assignment scheduling error:",
            repr(e)
        )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # DO NOT SEND ANOTHER CONNECT BUTTON HERE.
    #
    # The original Connect YouTube button in the ticket
    # already points to the SAME reusable OAuth session.
    #
    # Therefore the member can simply press that original
    # button again for account #2, #3, #4, etc.
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
            <strong>Google account:</strong>
            {safe_email}
            </p>

            <p>
            <strong>Channels added:</strong>
            </p>

            {channel_text}

            <hr>

            <p>
            <strong>This account's subscribers:</strong>
            {total_subscribers:,}
            </p>

            <p>
            <strong>This account's views:</strong>
            {total_views:,}
            </p>

            <hr>

            <p>
            <strong>Combined subscribers:</strong>
            {combined_subscribers:,}
            </p>

            <p>
            <strong>Combined views:</strong>
            {combined_views:,}
            </p>

            <p>
            Your Discord roles have been updated.
            </p>

            <p>
            Return to your Discord verification ticket
            and press <strong>Connect YouTube</strong> again
            if you want to add another Google/YouTube account.
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

    if not roles_to_add:

        print(
            f"{member} did not reach any milestone."
        )

        return

    # --------------------------------------------------------
    # REMOVE DUPLICATES
    # --------------------------------------------------------

    unique_roles = []

    seen_role_ids = set()

    for role in roles_to_add:

        if role.id not in seen_role_ids:

            unique_roles.append(
                role
            )

            seen_role_ids.add(
                role.id
            )

    # --------------------------------------------------------
    # ADD ROLES
    # --------------------------------------------------------

    try:

        await member.add_roles(
            *unique_roles,
            reason="YouTube milestone verification"
        )

        print(
            f"Added {len(unique_roles)} roles "
            f"to {member}"
        )

        print(
            "Verified totals:",
            subscribers,
            "subs |",
            views,
            "views"
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
            <a href="/privacy">Read our Privacy Policy</a>
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
            After a user authorizes the application through Google,
            the application retrieves information from the user's
            YouTube account to determine whether the user qualifies
            for creator milestone roles in the PRINT Discord server.
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
            The information obtained through Google and YouTube is
            used to verify creator milestones and assign the
            corresponding roles within the PRINT Discord server.
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
            Verification information is stored by the application
            so that a verified Discord account or YouTube channel
            cannot simply be verified repeatedly.
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
            <a href="/">Return to PRINT Creator Verification</a>
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
        "OAuth session lifetime:",
        OAUTH_SESSION_LIFETIME,
        "seconds"
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
