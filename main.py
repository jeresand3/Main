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

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    ""
).strip().rstrip("/")

GOOGLE_REDIRECT_URI = (
    f"{PUBLIC_BASE_URL}/callback"
)

DISCORD_REDIRECT_URI = (
    f"{PUBLIC_BASE_URL}/discord-callback"
)

TICKET_CATEGORY_ID = 1552294553413746769

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "verification.db"
)

OAUTH_STATE_LIFETIME = 600
TICKET_PENDING_LIFETIME = 120


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
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

if not GOOGLE_CLIENT_ID:
    raise RuntimeError("GOOGLE_CLIENT_ID is missing.")

if not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("GOOGLE_CLIENT_SECRET is missing.")

if not DISCORD_CLIENT_ID:
    raise RuntimeError("DISCORD_CLIENT_ID is missing.")

if not DISCORD_CLIENT_SECRET:
    raise RuntimeError("DISCORD_CLIENT_SECRET is missing.")

if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL is missing.")

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
        timeout=30,
        check_same_thread=False
    )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    return conn


def init_database():

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS google_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                google_sub TEXT NOT NULL UNIQUE,
                google_email TEXT,
                verified_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verified_channels (
                channel_id TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                channel_title TEXT,
                subscriber_count INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                verified_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT,
                google_sub TEXT,
                google_email TEXT,
                channel_id TEXT,
                channel_title TEXT,
                subscriber_count INTEGER,
                view_count INTEGER,
                verified_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_tickets (
                discord_user_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        # Linked Roles Discord OAuth states.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS discord_oauth_states (
                state TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)

        # Google states created from the Linked Roles flow.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS linked_google_states (
                state TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                discord_access_token TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        # Migrate old verification records.
        cursor.execute("""
            SELECT
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            FROM verifications
            WHERE google_sub IS NOT NULL
        """)

        for row in cursor.fetchall():

            (
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            ) = row

            if not discord_user_id or not google_sub:
                continue

            cursor.execute("""
                INSERT OR IGNORE INTO google_accounts (
                    discord_user_id,
                    google_sub,
                    google_email,
                    verified_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                str(discord_user_id),
                str(google_sub),
                google_email,
                verified_at or
                datetime.now(
                    timezone.utc
                ).isoformat()
            ))

        conn.commit()

    finally:

        conn.close()


init_database()


# ============================================================
# GOOGLE DATABASE HELPERS
# ============================================================

def get_google_account_by_sub(google_sub):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                id,
                discord_user_id,
                google_sub,
                google_email,
                verified_at
            FROM google_accounts
            WHERE google_sub = ?
        """, (
            str(google_sub),
        ))

        return cursor.fetchone()

    finally:

        conn.close()


def get_verified_channel(channel_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                channel_id,
                discord_user_id,
                channel_title,
                subscriber_count,
                view_count,
                verified_at
            FROM verified_channels
            WHERE channel_id = ?
        """, (
            str(channel_id),
        ))

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
        """, (
            str(discord_user_id),
        ))

        row = cursor.fetchone()

        return int(row[0]), int(row[1])

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

        verified_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

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
            str(google_sub),
            google_email,
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
                str(channel["id"]),
                str(discord_user_id),
                channel["title"],
                int(channel["subscriber_count"]),
                int(channel["view_count"]),
                verified_at
            ))

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


# ============================================================
# TICKET HELPERS
# ============================================================

def is_pending_ticket(channel_id):

    return str(channel_id).startswith(
        "pending-"
    )


def get_active_ticket(discord_user_id):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            FROM active_tickets
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        row = cursor.fetchone()

        if not row:
            return None

        if is_pending_ticket(row[2]):

            try:

                created = datetime.fromisoformat(
                    row[3]
                )

                age = (
                    datetime.now(timezone.utc)
                    - created
                ).total_seconds()

                if age > TICKET_PENDING_LIFETIME:

                    cursor.execute("""
                        DELETE FROM active_tickets
                        WHERE discord_user_id = ?
                    """, (
                        str(discord_user_id),
                    ))

                    conn.commit()

                    return None

            except Exception:
                pass

        return row

    finally:

        conn.close()


def reserve_ticket_slot(
    discord_user_id,
    guild_id
):

    token = secrets.token_urlsafe(16)

    pending_channel_id = (
        f"pending-{token}"
    )

    created_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            "BEGIN IMMEDIATE"
        )

        cursor.execute("""
            SELECT channel_id
            FROM active_tickets
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        existing = cursor.fetchone()

        if existing:

            conn.rollback()

            return False, existing[0]

        cursor.execute("""
            INSERT INTO active_tickets (
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            str(discord_user_id),
            str(guild_id),
            pending_channel_id,
            created_at
        ))

        conn.commit()

        return True, pending_channel_id

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


def set_active_ticket_channel(
    discord_user_id,
    channel_id
):

    conn = get_connection()

    try:

        conn.execute("""
            UPDATE active_tickets
            SET channel_id = ?
            WHERE discord_user_id = ?
        """, (
            str(channel_id),
            str(discord_user_id)
        ))

        conn.commit()

    finally:

        conn.close()


def delete_active_ticket(
    discord_user_id
):

    conn = get_connection()

    try:

        conn.execute("""
            DELETE FROM active_tickets
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        conn.commit()

    finally:

        conn.close()


# ============================================================
# GOOGLE OAUTH STATE
# ============================================================

def cleanup_expired_oauth_states():

    cutoff = (
        datetime.now(
            timezone.utc
        ).timestamp()
        - OAUTH_STATE_LIFETIME
    )

    conn = get_connection()

    try:

        conn.execute("""
            DELETE FROM oauth_states
            WHERE created_at < ?
        """, (
            cutoff,
        ))

        conn.commit()

    finally:

        conn.close()


def create_oauth_state(
    discord_user_id,
    guild_id,
    channel_id
):

    cleanup_expired_oauth_states()

    state = secrets.token_urlsafe(32)

    created_at = (
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    conn = get_connection()

    try:

        conn.execute("""
            INSERT INTO oauth_states (
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


def get_oauth_state(state):

    cleanup_expired_oauth_states()

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
            FROM oauth_states
            WHERE state = ?
        """, (
            state,
        ))

        row = cursor.fetchone()

        if not row:
            return None

        return {
            "state": row[0],
            "discord_user_id": row[1],
            "guild_id": row[2],
            "channel_id": row[3],
            "created_at": float(row[4])
        }

    finally:

        conn.close()


def consume_oauth_state(state):

    cleanup_expired_oauth_states()

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            "BEGIN IMMEDIATE"
        )

        cursor.execute("""
            SELECT
                state,
                discord_user_id,
                guild_id,
                channel_id,
                created_at
            FROM oauth_states
            WHERE state = ?
        """, (
            state,
        ))

        row = cursor.fetchone()

        if not row:

            conn.rollback()
            return None

        cursor.execute("""
            DELETE FROM oauth_states
            WHERE state = ?
        """, (
            state,
        ))

        conn.commit()

        created_at = float(row[4])

        if (
            datetime.now(
                timezone.utc
            ).timestamp()
            - created_at
            > OAUTH_STATE_LIFETIME
        ):
            return None

        return {
            "state": row[0],
            "discord_user_id": row[1],
            "guild_id": row[2],
            "channel_id": row[3],
            "created_at": created_at
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


def get_login_url(state):

    return (
        f"{PUBLIC_BASE_URL}/login?"
        + urlencode({
            "state": state
        })
    )


# ============================================================
# DISCORD LINKED ROLES STATE
# ============================================================

def create_discord_oauth_state():

    state = secrets.token_urlsafe(32)

    created_at = (
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    conn = get_connection()

    try:

        conn.execute("""
            INSERT INTO discord_oauth_states (
                state,
                created_at
            )
            VALUES (?, ?)
        """, (
            state,
            created_at
        ))

        conn.commit()

        return state

    finally:

        conn.close()


def consume_discord_oauth_state(state):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            "BEGIN IMMEDIATE"
        )

        cursor.execute("""
            SELECT
                state,
                created_at
            FROM discord_oauth_states
            WHERE state = ?
        """, (
            state,
        ))

        row = cursor.fetchone()

        if not row:

            conn.rollback()
            return False

        cursor.execute("""
            DELETE FROM discord_oauth_states
            WHERE state = ?
        """, (
            state,
        ))

        conn.commit()

        age = (
            datetime.now(
                timezone.utc
            ).timestamp()
            - float(row[1])
        )

        return age <= OAUTH_STATE_LIFETIME

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


def create_linked_google_state(
    discord_user_id,
    discord_access_token
):

    state = secrets.token_urlsafe(32)

    created_at = (
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    conn = get_connection()

    try:

        conn.execute("""
            INSERT INTO linked_google_states (
                state,
                discord_user_id,
                discord_access_token,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            state,
            str(discord_user_id),
            discord_access_token,
            created_at
        ))

        conn.commit()

        return state

    finally:

        conn.close()


def consume_linked_google_state(state):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            "BEGIN IMMEDIATE"
        )

        cursor.execute("""
            SELECT
                state,
                discord_user_id,
                discord_access_token,
                created_at
            FROM linked_google_states
            WHERE state = ?
        """, (
            state,
        ))

        row = cursor.fetchone()

        if not row:

            conn.rollback()
            return None

        cursor.execute("""
            DELETE FROM linked_google_states
            WHERE state = ?
        """, (
            state,
        ))

        conn.commit()

        age = (
            datetime.now(
                timezone.utc
            ).timestamp()
            - float(row[3])
        )

        if age > OAUTH_STATE_LIFETIME:
            return None

        return {
            "discord_user_id": row[1],
            "discord_access_token": row[2]
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.message_content = True


class PrintBot(commands.Bot):

    async def setup_hook(self):

        # The public verification message uses a direct link button,
        # so no ticket or legacy verification view is registered.
        pass



bot = PrintBot(
    command_prefix="$",
    intents=intents
)


# ============================================================
# EMBED
# ============================================================

def build_verification_embed():

    return discord.Embed(
        title="🎥 Creator Milestone Verification",
        description=(
            "Connect your YouTube account to verify your "
            "subscriber and view milestones."
        ),
        color=discord.Color.red()
    )


# ============================================================
# CONNECT YOUTUBE
# ============================================================

class ContinueToGoogleButton(Button):

    def __init__(self, login_url):

        super().__init__(
            label="Continue to Google",
            style=discord.ButtonStyle.link,
            url=login_url
        )


class ContinueToGoogleView(View):

    def __init__(self, login_url):

        super().__init__(
            timeout=None
        )

        self.add_item(
            ContinueToGoogleButton(
                login_url
            )
        )


class ConnectYouTubeButton(Button):

    def __init__(self):

        super().__init__(
            label="Connect YouTube",
            style=discord.ButtonStyle.primary,
            emoji="▶️",
            custom_id="connect_youtube_live"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        user = interaction.user
        guild = interaction.guild
        channel = interaction.channel

        try:

            if guild is None or channel is None:

                await interaction.followup.send(
                    "❌ This button can only be used inside the server.",
                    ephemeral=True
                )

                return

            active_ticket = await asyncio.to_thread(
                get_active_ticket,
                user.id
            )

            if not active_ticket:

                await interaction.followup.send(
                    "❌ You do not have an active verification ticket.",
                    ephemeral=True
                )

                return

            if is_pending_ticket(
                active_ticket[2]
            ):

                await interaction.followup.send(
                    "❌ Your verification ticket is still being created. "
                    "Please try again in a moment.",
                    ephemeral=True
                )

                return

            if str(active_ticket[1]) != str(guild.id):

                await interaction.followup.send(
                    "❌ This verification ticket belongs to another server.",
                    ephemeral=True
                )

                return

            if str(active_ticket[2]) != str(channel.id):

                await interaction.followup.send(
                    "❌ This is not your active verification ticket.",
                    ephemeral=True
                )

                return

            state = await asyncio.to_thread(
                create_oauth_state,
                user.id,
                guild.id,
                channel.id
            )

            login_url = get_login_url(
                state
            )

            await interaction.followup.send(
                "🔐 Your secure YouTube connection link is ready.\n\n"
                "Click **Continue to Google** below.",
                view=ContinueToGoogleView(
                    login_url
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Connect YouTube error:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ Something went wrong while creating your "
                "YouTube connection link.",
                ephemeral=True
            )


class ConnectYouTubeView(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

        self.add_item(
            ConnectYouTubeButton()
        )


# ============================================================
# CLOSE TICKET
# ============================================================

class CloseTicketButton(Button):

    def __init__(self):

        super().__init__(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id="close_verification_ticket"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        user = interaction.user
        channel = interaction.channel

        try:

            if channel is None:

                await interaction.followup.send(
                    "❌ This channel could not be identified.",
                    ephemeral=True
                )

                return

            if await bot.is_owner(user):

                await interaction.followup.send(
                    "👑 The bot owner cannot be removed from this ticket.",
                    ephemeral=True
                )

                return

            active_ticket = await asyncio.to_thread(
                get_active_ticket,
                user.id
            )

            if not active_ticket:

                await interaction.followup.send(
                    "❌ You do not have an active verification ticket.",
                    ephemeral=True
                )

                return

            if str(active_ticket[2]) != str(channel.id):

                await interaction.followup.send(
                    "❌ This is not your active verification ticket.",
                    ephemeral=True
                )

                return

            await channel.set_permissions(
                user,
                view_channel=False,
                send_messages=False,
                read_message_history=False
            )

            await asyncio.to_thread(
                delete_active_ticket,
                user.id
            )

            await interaction.followup.send(
                "🔒 Your verification ticket has been closed.",
                ephemeral=True
            )

        except Exception as e:

            print(
                "Close ticket error:",
                repr(e)
            )

            await interaction.followup.send(
                "❌ I couldn't close the ticket.",
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


# ============================================================
# DISCORD LINKED ROLE BUTTON
# ============================================================

class LinkedRoleLoginButton(Button):

    def __init__(self):

        super().__init__(
            label="Verify Creator Milestones",
            style=discord.ButtonStyle.link,
            emoji="🎥",
            url=f"{PUBLIC_BASE_URL}/discord-login"
        )


class LinkedRoleView(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

        self.add_item(
            LinkedRoleLoginButton()
        )


# ============================================================
# VERIFY BUTTON
# ============================================================

class VerifyButton(Button):

    def __init__(self):

        super().__init__(
            label="Verify Creator Milestones",
            style=discord.ButtonStyle.primary,
            emoji="🎥",
            custom_id="verify_creator_milestones"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        user = interaction.user
        guild = interaction.guild

        try:

            if guild is None:

                await interaction.followup.send(
                    "❌ This button can only be used inside a server.",
                    ephemeral=True
                )

                return

            existing_ticket = await asyncio.to_thread(
                get_active_ticket,
                user.id
            )

            if existing_ticket:

                existing_channel_id = str(
                    existing_ticket[2]
                )

                if is_pending_ticket(
                    existing_channel_id
                ):

                    await interaction.followup.send(
                        "❌ Your verification ticket is currently "
                        "being created. Please wait a moment.",
                        ephemeral=True
                    )

                    return

                existing_channel = guild.get_channel(
                    int(existing_channel_id)
                )

                if existing_channel:

                    await interaction.followup.send(
                        f"❌ You already have an active verification "
                        f"ticket: {existing_channel.mention}",
                        ephemeral=True
                    )

                    return

                await asyncio.to_thread(
                    delete_active_ticket,
                    user.id
                )

            reserved, reservation_value = (
                await asyncio.to_thread(
                    reserve_ticket_slot,
                    user.id,
                    guild.id
                )
            )

            if not reserved:

                value = str(
                    reservation_value
                )

                if is_pending_ticket(
                    value
                ):

                    await interaction.followup.send(
                        "❌ Your verification ticket is currently "
                        "being created. Please wait a moment.",
                        ephemeral=True
                    )

                    return

                if value.isdigit():

                    existing_channel = guild.get_channel(
                        int(value)
                    )

                    if existing_channel:

                        await interaction.followup.send(
                            f"❌ You already have an active verification "
                            f"ticket: {existing_channel.mention}",
                            ephemeral=True
                        )

                        return

                await interaction.followup.send(
                    "❌ You already have a verification ticket.",
                    ephemeral=True
                )

                return

            category = guild.get_channel(
                TICKET_CATEGORY_ID
            )

            if not isinstance(
                category,
                discord.CategoryChannel
            ):

                await asyncio.to_thread(
                    delete_active_ticket,
                    user.id
                )

                await interaction.followup.send(
                    "❌ The verification ticket category could not be found.",
                    ephemeral=True
                )

                return

            safe_name = re.sub(
                r"[^a-zA-Z0-9-]",
                "-",
                user.name.lower()
            ).strip("-")

            if not safe_name:
                safe_name = "user"

            channel_name = (
                f"verify-{safe_name}"
            )

            overwrites = {
                guild.default_role:
                    discord.PermissionOverwrite(
                        view_channel=False
                    ),

                user:
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
            }

            if guild.me:

                overwrites[guild.me] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_channels=True,
                        manage_permissions=True
                    )
                )

            try:

                ticket_channel = (
                    await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=overwrites,
                        reason=(
                            "Creator milestone "
                            "verification ticket"
                        )
                    )
                )

            except Exception as e:

                print(
                    "Ticket channel creation failed:",
                    repr(e)
                )

                await asyncio.to_thread(
                    delete_active_ticket,
                    user.id
                )

                await interaction.followup.send(
                    "❌ I couldn't create your verification ticket.",
                    ephemeral=True
                )

                return

            await asyncio.to_thread(
                set_active_ticket_channel,
                user.id,
                ticket_channel.id
            )

            try:

                await ticket_channel.send(
                    content=user.mention,
                    embed=build_verification_embed(),
                    view=ConnectYouTubeView()
                )

                await ticket_channel.send(
                    "When you're finished with verification, "
                    "you can close this ticket below.",
                    view=CloseTicketView()
                )

            except Exception as e:

                print(
                    "Failed sending ticket controls:",
                    repr(e)
                )

            await interaction.followup.send(
                f"✅ Your verification ticket is ready: "
                f"{ticket_channel.mention}",
                ephemeral=True
            )

        except Exception as e:

            print(
                "Verify button error:",
                repr(e)
            )

            try:

                await asyncio.to_thread(
                    delete_active_ticket,
                    user.id
                )

            except Exception:
                pass

            await interaction.followup.send(
                "❌ Something went wrong while creating your "
                "verification ticket.",
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
# FRESH TICKET CONTROLS
# ============================================================

async def send_verification_controls(
    channel,
    user
):

    await channel.send(
        content=user.mention,
        embed=build_verification_embed(),
        view=ConnectYouTubeView()
    )

    await channel.send(
        "You can connect another YouTube account above, "
        "or close this ticket when finished.",
        view=CloseTicketView()
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"Public base URL: {PUBLIC_BASE_URL}"
    )

    print(
        f"Google OAuth redirect URI: "
        f"{GOOGLE_REDIRECT_URI}"
    )

    print(
        f"Discord OAuth redirect URI: "
        f"{DISCORD_REDIRECT_URI}"
    )

    print(
        "Linked Role verification URL: "
        f"{PUBLIC_BASE_URL}/discord-login"
    )


# ============================================================
# $roles
# ============================================================

@bot.command(name="roles")
@commands.is_owner()
async def roles_command(ctx):

    embed = discord.Embed(
        title="🎥 Creator Milestone Linked Role",
        description=(
            "Connect your Discord account to your Google/YouTube "
            "account to verify your creator milestones.\n\n"
            "PRINT securely checks your YouTube statistics and "
            "sends only the milestone values to Discord.\n\n"
            "No verification ticket is required."
        ),
        color=discord.Color.red()
    )

    await ctx.send(
        embed=embed,
        view=LinkedRoleView()
    )


@roles_command.error
async def roles_command_error(
    ctx,
    error
):

    if isinstance(
        error,
        commands.NotOwner
    ):

        await ctx.send(
            "❌ Only the bot owner can use this command."
        )


# ============================================================
# RESET
# ============================================================

def reset_user_verification(
    discord_user_id
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM verified_channels
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        cursor.execute("""
            DELETE FROM google_accounts
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        cursor.execute("""
            DELETE FROM verifications
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        cursor.execute("""
            DELETE FROM active_tickets
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        cursor.execute("""
            DELETE FROM oauth_states
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        cursor.execute("""
            DELETE FROM linked_google_states
            WHERE discord_user_id = ?
        """, (
            str(discord_user_id),
        ))

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


@bot.command(name="resetverification")
@commands.is_owner()
async def resetverification_command(ctx):

    user = ctx.author

    try:

        active_ticket = await asyncio.to_thread(
            get_active_ticket,
            user.id
        )

        if active_ticket:

            channel_id = str(
                active_ticket[2]
            )

            if channel_id.isdigit():

                ticket_channel = (
                    ctx.guild.get_channel(
                        int(channel_id)
                    )
                )

                if ticket_channel:

                    try:

                        await ticket_channel.delete(
                            reason=(
                                "Temporary verification "
                                "test reset"
                            )
                        )

                    except Exception as e:

                        print(
                            "Could not delete old test ticket:",
                            repr(e)
                        )

        await asyncio.to_thread(
            reset_user_verification,
            user.id
        )

        removed_roles = 0

        for role_id in ROLE_MAP.values():

            role = ctx.guild.get_role(
                role_id
            )

            if role and role in user.roles:

                try:

                    await user.remove_roles(
                        role,
                        reason=(
                            "Temporary verification "
                            "test reset"
                        )
                    )

                    removed_roles += 1

                except Exception as e:

                    print(
                        f"Could not remove role {role_id}:",
                        repr(e)
                    )

        await ctx.send(
            "🧹 **Verification test reset complete.**\n\n"
            "Your Google account verification data, "
            "YouTube channel verification data, "
            "legacy verification data, OAuth states, "
            "active ticket, and milestone roles have all "
            "been reset.\n\n"
            f"Removed milestone roles: **{removed_roles}**\n\n"
            "You can now run `$roles` and perform a completely "
            "fresh Linked Role verification test."
        )

    except Exception as e:

        print(
            "Reset verification error:",
            repr(e)
        )

        await ctx.send(
            "❌ The verification test reset failed. "
            "Check the Railway logs."
        )


@resetverification_command.error
async def resetverification_command_error(
    ctx,
    error
):

    if isinstance(
        error,
        commands.NotOwner
    ):

        await ctx.send(
            "❌ Only the bot owner can use this command."
        )


# ============================================================
# GOOGLE / YOUTUBE API
# ============================================================

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)

GOOGLE_TOKEN_URL = (
    "https://oauth2.googleapis.com/token"
)

GOOGLE_USERINFO_URL = (
    "https://openidconnect.googleapis.com/v1/userinfo"
)

YOUTUBE_API_URL = (
    "https://www.googleapis.com/youtube/v3"
)


def exchange_code_for_token(code):

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def get_google_user(access_token):

    response = requests.get(
        GOOGLE_USERINFO_URL,
        headers={
            "Authorization": (
                f"Bearer {access_token}"
            )
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def get_all_youtube_channels(
    access_token
):

    channels = []
    page_token = None

    while True:

        params = {
            "part": "snippet,statistics",
            "mine": "true",
            "maxResults": 50
        }

        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            f"{YOUTUBE_API_URL}/channels",
            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        for item in data.get(
            "items",
            []
        ):

            statistics = item.get(
                "statistics",
                {}
            )

            channels.append({
                "id": item["id"],
                "title": item["snippet"]["title"],
                "subscriber_count": int(
                    statistics.get(
                        "subscriberCount",
                        0
                    )
                ),
                "view_count": int(
                    statistics.get(
                        "viewCount",
                        0
                    )
                )
            })

        page_token = data.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return channels


# ============================================================
# DISCORD LINKED ROLES API
# ============================================================

DISCORD_API_URL = (
    "https://discord.com/api/v10"
)


def discord_oauth_authorize_url(
    state
):

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": (
            "identify "
            "role_connections.write"
        ),
        "state": state
    }

    return (
        "https://discord.com/oauth2/authorize?"
        + urlencode(params)
    )


def exchange_discord_code(
    code
):

    response = requests.post(
        f"{DISCORD_API_URL}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI
        },
        auth=(
            DISCORD_CLIENT_ID,
            DISCORD_CLIENT_SECRET
        ),
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def get_discord_user(
    access_token
):

    response = requests.get(
        f"{DISCORD_API_URL}/users/@me",
        headers={
            "Authorization":
                f"Bearer {access_token}"
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def register_linked_role_metadata():

    payload = [
        {
            "key": "subscribers",
            "name": "Subscribers",
            "description": (
                "Total YouTube subscribers "
                "across verified channels"
            ),
            "type": 2
        },
        {
            "key": "total_views",
            "name": "Total Views",
            "description": (
                "Total YouTube views across "
                "verified channels"
            ),
            "type": 2
        }
    ]

    response = requests.put(
        (
            f"{DISCORD_API_URL}/applications/"
            f"{DISCORD_CLIENT_ID}/role-connections/metadata"
        ),
        headers={
            "Authorization":
                f"Bot {BOT_TOKEN}",
            "Content-Type":
                "application/json"
        },
        json=payload,
        timeout=30
    )

    if response.status_code not in (
        200,
        201,
        204
    ):

        print(
            "Linked Role metadata registration failed:",
            response.status_code,
            response.text
        )

        return False

    print(
        "Linked Role metadata registered."
    )

    return True


def update_discord_role_connection(
    discord_access_token,
    subscribers,
    views
):

    payload = {
        "platform_name": "PRINT YouTube",
        "metadata": {
            "subscribers": str(
                int(subscribers)
            ),
            "total_views": str(
                int(views)
            )
        }
    }

    response = requests.put(
        (
            f"{DISCORD_API_URL}/users/@me/"
            f"applications/{DISCORD_CLIENT_ID}/"
            "role-connection"
        ),
        headers={
            "Authorization":
                f"Bearer {discord_access_token}",
            "Content-Type":
                "application/json"
        },
        json=payload,
        timeout=30
    )

    if response.status_code not in (
        200,
        201
    ):

        print(
            "Discord role connection update failed:",
            response.status_code,
            response.text
        )

        return False

    print(
        "Discord role connection updated:",
        response.text
    )

    return True


# ============================================================
# ROLE ASSIGNMENT
# ============================================================

async def assign_roles(
    discord_user_id,
    guild_id
):

    guild = bot.get_guild(
        int(guild_id)
    )

    if not guild:

        print(
            f"Guild {guild_id} not found."
        )

        return

    member = guild.get_member(
        int(discord_user_id)
    )

    if not member:

        try:

            member = await guild.fetch_member(
                int(discord_user_id)
            )

        except Exception:

            print(
                f"Could not find member "
                f"{discord_user_id}."
            )

            return

    subscribers, views = (
        await asyncio.to_thread(
            get_aggregate_totals,
            discord_user_id
        )
    )

    thresholds = [
        (
            "50K_SUBS",
            subscribers >= 50_000
        ),
        (
            "100K_SUBS",
            subscribers >= 100_000
        ),
        (
            "1M_SUBS",
            subscribers >= 1_000_000
        ),
        (
            "1M_VIEWS",
            views >= 1_000_000
        ),
        (
            "10M_VIEWS",
            views >= 10_000_000
        ),
        (
            "50M_VIEWS",
            views >= 50_000_000
        ),
        (
            "100M_VIEWS",
            views >= 100_000_000
        ),
        (
            "1B_VIEWS",
            views >= 1_000_000_000
        )
    ]

    for role_name, qualifies in thresholds:

        if not qualifies:
            continue

        role = guild.get_role(
            ROLE_MAP[role_name]
        )

        if not role:

            print(
                f"Role {ROLE_MAP[role_name]} not found."
            )

            continue

        if role not in member.roles:

            try:

                await member.add_roles(
                    role,
                    reason=(
                        "YouTube creator "
                        "milestone verification"
                    )
                )

            except Exception as e:

                print(
                    f"Could not add {role_name}:",
                    repr(e)
                )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# LINKED ROLES LOGIN
# ============================================================

@app.get("/discord-login")
async def discord_login():

    state = await asyncio.to_thread(
        create_discord_oauth_state
    )

    return RedirectResponse(
        discord_oauth_authorize_url(
            state
        ),
        status_code=302
    )


# ============================================================
# DISCORD OAUTH CALLBACK
# ============================================================

@app.get("/discord-callback")
async def discord_callback(
    request: Request
):

    state = request.query_params.get(
        "state"
    )

    code = request.query_params.get(
        "code"
    )

    error = request.query_params.get(
        "error"
    )

    if error:

        return HTMLResponse(
            f"""
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Discord authorization cancelled</h2>
            <p>{html.escape(error)}</p>
            <p>You can close this page.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    if not state or not code:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Invalid Discord OAuth callback</h2>
            <p>The required information was missing.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    valid_state = await asyncio.to_thread(
        consume_discord_oauth_state,
        state
    )

    if not valid_state:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Discord verification link expired</h2>
            <p>Please start the Linked Role verification again.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    try:

        token_data = await asyncio.to_thread(
            exchange_discord_code,
            code
        )

        discord_access_token = (
            token_data["access_token"]
        )

    except Exception as e:

        print(
            "Discord OAuth token exchange failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Discord connection failed</h2>
            <p>We couldn't authorize your Discord account.</p>
            </body>
            </html>
            """,
            status_code=500
        )

    try:

        discord_user = await asyncio.to_thread(
            get_discord_user,
            discord_access_token
        )

        discord_user_id = discord_user["id"]

    except Exception as e:

        print(
            "Discord user lookup failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Could not identify your Discord account</h2>
            <p>Please try again.</p>
            </body>
            </html>
            """,
            status_code=500
        )

    google_state = await asyncio.to_thread(
        create_linked_google_state,
        discord_user_id,
        discord_access_token
    )

    return RedirectResponse(
        f"{PUBLIC_BASE_URL}/linked-google-login?"
        + urlencode({
            "state": google_state
        }),
        status_code=302
    )


# ============================================================
# LINKED ROLES → GOOGLE
# ============================================================

@app.get("/linked-google-login")
async def linked_google_login(
    request: Request
):

    state = request.query_params.get(
        "state"
    )

    if not state:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Invalid verification link</h2>
            </body>
            </html>
            """,
            status_code=400
        )

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                state,
                created_at
            FROM linked_google_states
            WHERE state = ?
        """, (
            state,
        ))

        row = cursor.fetchone()

    finally:

        conn.close()

    if not row:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Verification link expired</h2>
            <p>Please start Linked Role verification again.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    age = (
        datetime.now(
            timezone.utc
        ).timestamp()
        - float(row[1])
    )

    if age > OAUTH_STATE_LIFETIME:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Verification link expired</h2>
            <p>Please start Linked Role verification again.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": (
            "openid "
            "email "
            "https://www.googleapis.com/auth/"
            "youtube.readonly"
        ),
        "access_type": "offline",
        "prompt": "select_account",
        "state": state
    }

    return RedirectResponse(
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(params),
        status_code=302
    )


# ============================================================
# GOOGLE LOGIN
# ============================================================

@app.get("/login")
async def login(
    request: Request
):

    state = request.query_params.get(
        "state"
    )

    if not state:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Invalid verification link</h2>
            <p>No OAuth state was provided.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    state_data = await asyncio.to_thread(
        get_oauth_state,
        state
    )

    if not state_data:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Verification link expired</h2>
            <p>
                Return to Discord and press
                <b>Connect YouTube</b> again.
            </p>
            </body>
            </html>
            """,
            status_code=400
        )

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": (
            "openid "
            "email "
            "https://www.googleapis.com/auth/"
            "youtube.readonly"
        ),
        "access_type": "offline",
        "prompt": "select_account",
        "state": state
    }

    return RedirectResponse(
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(params),
        status_code=302
    )


# ============================================================
# GOOGLE CALLBACK
# ============================================================

@app.get("/callback")
async def callback(
    request: Request
):

    state = request.query_params.get(
        "state"
    )

    code = request.query_params.get(
        "code"
    )

    error = request.query_params.get(
        "error"
    )

    if error:

        return HTMLResponse(
            f"""
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Google authorization cancelled</h2>
            <p>{html.escape(error)}</p>
            <p>
                You can close this page and try again.
            </p>
            </body>
            </html>
            """,
            status_code=400
        )

    if not state or not code:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Invalid OAuth callback</h2>
            </body>
            </html>
            """,
            status_code=400
        )

    # ========================================================
    # DETERMINE FLOW
    # ========================================================

    ticket_state = await asyncio.to_thread(
        consume_oauth_state,
        state
    )

    linked_state = None

    if ticket_state is None:

        linked_state = await asyncio.to_thread(
            consume_linked_google_state,
            state
        )

        if not linked_state:

            return HTMLResponse(
                """
                <html>
                <body style="
                    background:#111;
                    color:white;
                    font-family:Arial;
                    padding:40px;
                ">
                <h2>Verification link expired</h2>
                <p>Please start verification again.</p>
                </body>
                </html>
                """,
                status_code=400
            )

        discord_user_id = (
            linked_state["discord_user_id"]
        )

        discord_access_token = (
            linked_state["discord_access_token"]
        )

        guild_id = None
        ticket_channel_id = None
        linked_role_flow = True

    else:

        discord_user_id = (
            ticket_state["discord_user_id"]
        )

        guild_id = (
            ticket_state["guild_id"]
        )

        ticket_channel_id = (
            ticket_state["channel_id"]
        )

        discord_access_token = None
        linked_role_flow = False

    # ========================================================
    # EXISTING TICKET CHECK
    # ========================================================

    if not linked_role_flow:

        active_ticket = await asyncio.to_thread(
            get_active_ticket,
            discord_user_id
        )

        if not active_ticket:

            return HTMLResponse(
                """
                <html>
                <body style="
                    background:#111;
                    color:white;
                    font-family:Arial;
                    padding:40px;
                ">
                <h2>Verification ticket closed</h2>
                <p>
                    Your verification ticket is no longer active.
                </p>
                </body>
                </html>
                """,
                status_code=400
            )

        if is_pending_ticket(
            active_ticket[2]
        ):

            return HTMLResponse(
                """
                <html>
                <body style="
                    background:#111;
                    color:white;
                    font-family:Arial;
                    padding:40px;
                ">
                <h2>
                    Verification ticket is still being created
                </h2>
                <p>
                    Please return to Discord and try again shortly.
                </p>
                </body>
                </html>
                """,
                status_code=400
            )

        if (
            str(active_ticket[1])
            != str(guild_id)
            or
            str(active_ticket[2])
            != str(ticket_channel_id)
        ):

            return HTMLResponse(
                """
                <html>
                <body style="
                    background:#111;
                    color:white;
                    font-family:Arial;
                    padding:40px;
                ">
                <h2>Verification ticket mismatch</h2>
                <p>
                    Please start a new connection from your
                    active ticket.
                </p>
                </body>
                </html>
                """,
                status_code=400
            )

    # ========================================================
    # GOOGLE TOKEN
    # ========================================================

    try:

        token_data = await asyncio.to_thread(
            exchange_code_for_token,
            code
        )

        access_token = (
            token_data["access_token"]
        )

    except Exception as e:

        print(
            "Google token exchange failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Google connection failed</h2>
            <p>
                The authorization code could not be exchanged.
            </p>
            </body>
            </html>
            """,
            status_code=500
        )

    # ========================================================
    # GOOGLE USER
    # ========================================================

    try:

        google_user = await asyncio.to_thread(
            get_google_user,
            access_token
        )

    except Exception as e:

        print(
            "Google user lookup failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Could not identify your Google account</h2>
            <p>Please try again.</p>
            </body>
            </html>
            """,
            status_code=500
        )

    google_sub = google_user.get(
        "sub"
    )

    google_email = google_user.get(
        "email"
    )

    if not google_sub:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Google account information missing</h2>
            <p>Please try again.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    # ========================================================
    # DUPLICATE GOOGLE ACCOUNT
    # ========================================================

    existing_google_account = (
        await asyncio.to_thread(
            get_google_account_by_sub,
            google_sub
        )
    )

    if existing_google_account:

        if (
            str(existing_google_account[1])
            == str(discord_user_id)
        ):

            message = (
                "This Gmail is already verified "
                "for your Discord account."
            )

        else:

            message = (
                "This Gmail is already verified "
                "by another Discord account."
            )

        return HTMLResponse(
            f"""
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Google account already verified</h2>
            <p>{html.escape(message)}</p>
            <p>Return to Discord to continue.</p>
            </body>
            </html>
            """,
            status_code=409
        )

    # ========================================================
    # YOUTUBE CHANNELS
    # ========================================================

    try:

        channels = await asyncio.to_thread(
            get_all_youtube_channels,
            access_token
        )

    except Exception as e:

        print(
            "YouTube channel lookup failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>YouTube lookup failed</h2>
            <p>
                We couldn't retrieve your YouTube channels.
            </p>
            <p>Please try again.</p>
            </body>
            </html>
            """,
            status_code=500
        )

    if not channels:

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>No YouTube channels found</h2>
            <p>
                This Google account does not appear to have
                an accessible YouTube channel.
            </p>
            </body>
            </html>
            """,
            status_code=400
        )

    # ========================================================
    # DUPLICATE CHANNELS
    # ========================================================

    duplicate_channels = []

    for channel in channels:

        existing_channel = (
            await asyncio.to_thread(
                get_verified_channel,
                channel["id"]
            )
        )

        if existing_channel:

            duplicate_channels.append(
                channel["title"]
            )

    if duplicate_channels:

        channel_names = ", ".join(
            duplicate_channels
        )

        return HTMLResponse(
            f"""
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>YouTube channel already verified</h2>
            <p>
                The following channel(s) are already verified:
            </p>
            <p>{html.escape(channel_names)}</p>
            <p>Return to Discord to continue.</p>
            </body>
            </html>
            """,
            status_code=409
        )

    # ========================================================
    # SAVE
    # ========================================================

    try:

        await asyncio.to_thread(
            save_google_account_and_channels,
            discord_user_id,
            google_sub,
            google_email,
            channels
        )

    except sqlite3.IntegrityError as e:

        print(
            "Duplicate verification prevented:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>This account or channel is already verified</h2>
            <p>Return to Discord to continue.</p>
            </body>
            </html>
            """,
            status_code=409
        )

    except Exception as e:

        print(
            "Database save failed:",
            repr(e)
        )

        return HTMLResponse(
            """
            <html>
            <body style="
                background:#111;
                color:white;
                font-family:Arial;
                padding:40px;
            ">
            <h2>Verification could not be saved</h2>
            <p>Please try again.</p>
            </body>
            </html>
            """,
            status_code=500
        )

    # ========================================================
    # AGGREGATE
    # ========================================================

    subscribers, views = (
        await asyncio.to_thread(
            get_aggregate_totals,
            discord_user_id
        )
    )

    # ========================================================
    # LINKED ROLE UPDATE
    # ========================================================

    linked_role_success = True

    if linked_role_flow:

        linked_role_success = (
            await asyncio.to_thread(
                update_discord_role_connection,
                discord_access_token,
                subscribers,
                views
            )
        )

    # ========================================================
    # NORMAL BOT ROLE ASSIGNMENT
    # ========================================================

    if not linked_role_flow:

        try:

            if bot.loop.is_running():

                asyncio.run_coroutine_threadsafe(
                    assign_roles(
                        discord_user_id,
                        guild_id
                    ),
                    bot.loop
                )

        except Exception as e:

            print(
                "Role assignment scheduling failed:",
                repr(e)
            )

        # Send another Connect YouTube button.

        try:

            channel = bot.get_channel(
                int(ticket_channel_id)
            )

            guild = bot.get_guild(
                int(guild_id)
            )

            if channel and guild:

                member = guild.get_member(
                    int(discord_user_id)
                )

                if member:

                    asyncio.run_coroutine_threadsafe(
                        send_verification_controls(
                            channel,
                            member
                        ),
                        bot.loop
                    )

        except Exception as e:

            print(
                "New controls scheduling failed:",
                repr(e)
            )

    # ========================================================
    # SUCCESS PAGE
    # ========================================================

    safe_email = html.escape(
        google_email or "Google account"
    )

    channel_count = len(
        channels
    )

    if linked_role_flow:

        if linked_role_success:

            title = (
                "✅ Linked Role Verification Complete"
            )

            extra = (
                "Your YouTube statistics have been "
                "sent to Discord. You can return to "
                "Discord and finish the Linked Role setup."
            )

        else:

            title = (
                "⚠️ YouTube Verification Complete"
            )

            extra = (
                "Your YouTube account was verified, "
                "but Discord could not update the "
                "Linked Role connection. Please try "
                "the Linked Role verification again."
            )

    else:

        title = (
            "✅ Verification Complete"
        )

        extra = (
            "You can return to Discord now."
        )

    return HTMLResponse(
        f"""
        <html>

        <head>
            <title>Verification Complete</title>
        </head>

        <body style="
            margin:0;
            min-height:100vh;
            background:#111;
            color:white;
            font-family:Arial,sans-serif;
            display:flex;
            align-items:center;
            justify-content:center;
        ">

        <div style="
            max-width:650px;
            padding:40px;
            text-align:center;
        ">

        <h1>{title}</h1>

        <p>
            Your YouTube account has been successfully verified.
        </p>

        <p>
            <b>{safe_email}</b>
        </p>

        <hr style="margin:30px 0;opacity:.2;">

        <p>
            YouTube channels verified:
            <b>{channel_count}</b>
        </p>

        <p>
            Total subscribers:
            <b>{subscribers:,}</b>
        </p>

        <p>
            Total views:
            <b>{views:,}</b>
        </p>

        <p style="
            margin-top:30px;
            opacity:.75;
        ">
            {extra}
        </p>

        </div>

        </body>
        </html>
        """
    )


# ============================================================
# HOME
# ============================================================

@app.get("/")
async def home():

    return HTMLResponse(
        f"""
        <html>

        <head>
            <title>PRINT Creator Verification</title>
        </head>

        <body style="
            margin:0;
            min-height:100vh;
            background:#111;
            color:white;
            font-family:Arial,sans-serif;
            display:flex;
            align-items:center;
            justify-content:center;
        ">

        <div style="
            text-align:center;
            max-width:650px;
            padding:40px;
        ">

        <h1>PRINT Creator Verification</h1>

        <p>
            This service handles YouTube creator milestone
            verification for the PRINT Discord server.
        </p>

        </div>

        </body>
        </html>
        """
    )


# ============================================================
# PRIVACY
# ============================================================

@app.get("/privacy")
async def privacy():

    return HTMLResponse(
        """
        <html>

        <head>
            <title>Privacy Policy</title>
        </head>

        <body style="
            background:#111;
            color:white;
            font-family:Arial,sans-serif;
            line-height:1.6;
            padding:40px;
        ">

        <h1>Privacy Policy</h1>

        <p>
            This verification service uses Google OAuth
            to verify YouTube creator statistics.
        </p>

        <p>
            The service stores the information required
            to prevent duplicate verification and assign
            Discord milestone roles.
        </p>

        <p>
            YouTube account information is used only for
            the creator verification system.
        </p>

        </body>
        </html>
        """
    )


# ============================================================
# WEB SERVER
# ============================================================

def run_web_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    # Register Linked Role metadata once at startup.
    try:

        register_linked_role_metadata()

    except Exception as e:

        print(
            "Linked Role metadata startup error:",
            repr(e)
        )

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    web_thread.start()

    bot.run(
        BOT_TOKEN
    )
