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
            "Missing required environment variables: "
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
    connection = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():
    connection = get_connection()
    cursor = connection.cursor()

    # --------------------------------------------------------
    # Google accounts
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS google_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            google_sub TEXT NOT NULL UNIQUE,
            google_email TEXT NOT NULL,
            total_subscribers INTEGER NOT NULL DEFAULT 0,
            total_views INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT NOT NULL
        )
        """
    )

    # --------------------------------------------------------
    # Verified YouTube channels
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Verification tickets
    #
    # One active ticket per Discord member.
    #
    # The UNIQUE partial index below is the important part:
    # SQLite itself prevents two open tickets for the same
    # Discord user.
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            channel_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        one_open_ticket_per_user
        ON verification_tickets(discord_user_id)
        WHERE status = 'open'
        """
    )

    # --------------------------------------------------------
    # OAuth states
    #
    # These are stored in memory because OAuth sessions are
    # short-lived. Database ticket state remains persistent.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Old database migration
    # --------------------------------------------------------

    cursor.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        AND name='verifications'
        """
    )

    old_table_exists = cursor.fetchone() is not None

    if old_table_exists:
        cursor.execute(
            "PRAGMA table_info(verifications)"
        )

        old_columns = {
            row[1]
            for row in cursor.fetchall()
        }

        required_old_columns = {
            "discord_user_id",
            "channel_id",
            "google_sub",
            "channel_title",
            "subscriber_count",
            "view_count",
            "verified_at",
        }

        if required_old_columns.issubset(old_columns):
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
        WHERE discord_user_id = ?
        AND google_sub = ?
        """,
        (
            str(discord_user_id),
            google_sub,
        ),
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
            COALESCE(SUM(total_subscribers), 0)
            AS total_subscribers,

            COALESCE(SUM(total_views), 0)
            AS total_views

        FROM google_accounts

        WHERE discord_user_id = ?
        """,
        (str(discord_user_id),),
    ).fetchone()

    connection.close()

    return (
        int(row["total_subscribers"]),
        int(row["total_views"]),
    )


# ============================================================
# TICKET DATABASE HELPERS
# ============================================================

def get_open_ticket(discord_user_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM verification_tickets
        WHERE discord_user_id = ?
        AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(discord_user_id),),
    ).fetchone()

    connection.close()

    return row


def get_ticket_by_channel(channel_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM verification_tickets
        WHERE channel_id = ?
        AND status = 'open'
        LIMIT 1
        """,
        (str(channel_id),),
    ).fetchone()

    connection.close()

    return row


def claim_normal_ticket(
    discord_user_id,
    guild_id,
):
    """
    Atomically claims the one allowed open ticket for a
    normal member.

    Returns:
        (True, ticket_id)
        (False, existing_ticket_row)
    """

    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        existing = connection.execute(
            """
            SELECT *
            FROM verification_tickets
            WHERE discord_user_id = ?
            AND status = 'open'
            LIMIT 1
            """,
            (str(discord_user_id),),
        ).fetchone()

        if existing:
            connection.commit()
            return False, existing

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

        cursor = connection.execute(
            """
            INSERT INTO verification_tickets (
                discord_user_id,
                guild_id,
                channel_id,
                status,
                created_at
            )
            VALUES (?, ?, NULL, 'open', ?)
            """,
            (
                str(discord_user_id),
                str(guild_id),
                created_at,
            ),
        )

        ticket_id = cursor.lastrowid

        connection.commit()

        return True, ticket_id

    except sqlite3.IntegrityError:

        connection.rollback()

        existing = connection.execute(
            """
            SELECT *
            FROM verification_tickets
            WHERE discord_user_id = ?
            AND status = 'open'
            LIMIT 1
            """,
            (str(discord_user_id),),
        ).fetchone()

        return False, existing

    finally:
        connection.close()


def create_owner_ticket(
    discord_user_id,
    guild_id,
):
    """
    Server owner bypass.

    The owner is allowed to create unlimited test tickets.
    """

    connection = get_connection()

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    cursor = connection.execute(
        """
        INSERT INTO verification_tickets (
            discord_user_id,
            guild_id,
            channel_id,
            status,
            created_at
        )
        VALUES (?, ?, NULL, 'open', ?)
        """,
        (
            str(discord_user_id),
            str(guild_id),
            created_at,
        ),
    )

    ticket_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return ticket_id


def set_ticket_channel(
    ticket_id,
    channel_id,
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE verification_tickets
        SET channel_id = ?
        WHERE id = ?
        """,
        (
            str(channel_id),
            ticket_id,
        ),
    )

    connection.commit()
    connection.close()


def mark_ticket_closed(ticket_id):
    connection = get_connection()

    closed_at = datetime.now(
        timezone.utc
    ).isoformat()

    connection.execute(
        """
        UPDATE verification_tickets
        SET status = 'closed',
            closed_at = ?
        WHERE id = ?
        """,
        (
            closed_at,
            ticket_id,
        ),
    )

    connection.commit()
    connection.close()


def mark_ticket_failed(ticket_id):
    connection = get_connection()

    closed_at = datetime.now(
        timezone.utc
    ).isoformat()

    connection.execute(
        """
        UPDATE verification_tickets
        SET status = 'failed',
            closed_at = ?
        WHERE id = ?
        """,
        (
            closed_at,
            ticket_id,
        ),
    )

    connection.commit()
    connection.close()


# ============================================================
# SAVE GOOGLE ACCOUNT + CHANNELS
# ============================================================

def save_google_account_and_channels(
    discord_user_id,
    google_sub,
    google_email,
    channels,
):
    verified_at = datetime.now(
        timezone.utc
    ).isoformat()

    total_subscribers = sum(
        channel["subscriber_count"]
        for channel in channels
    )

    total_views = sum(
        channel["view_count"]
        for channel in channels
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

    return (
        total_subscribers,
        total_views,
    )


# ============================================================
# OAUTH STATE
# ============================================================

oauth_states = {}


def cleanup_expired_oauth_states():
    now = datetime.now(
        timezone.utc
    ).timestamp()

    expired_states = []

    for state, data in oauth_states.items():

        if (
            now - data["created_at"]
            > 600
        ):
            expired_states.append(state)

    for state in expired_states:
        oauth_states.pop(
            state,
            None,
        )


# ============================================================
# SERVER OWNER CHECK
# ============================================================

def is_server_owner(
    user,
    guild,
):
    if guild is None:
        return False

    return user.id == guild.owner_id


# ============================================================
# TICKET PERMISSION HELPERS
# ============================================================

async def remove_member_from_ticket(
    channel,
    member,
):
    try:
        await channel.set_permissions(
            member,
            view_channel=False,
            send_messages=False,
            read_message_history=False,
            overwrite=None,
            reason="Creator verification ticket closed",
        )

    except discord.DiscordException as exc:
        print(
            f"Could not remove {member} from ticket "
            f"{channel.id}: {exc}"
        )


# ============================================================
# VERIFICATION BUTTON
# ============================================================

class VerifyButton(Button):

    def __init__(self):
        super().__init__(
            label="Verify / Add YouTube Account",
            style=discord.ButtonStyle.primary,
            custom_id="verify_add_youtube_account",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        user = interaction.user
        guild = interaction.guild

        if guild is None:

            await interaction.response.send_message(
                "This button can only be used inside the Discord server.",
                ephemeral=True,
            )

            return

        owner = is_server_owner(
            user,
            guild,
        )

        # ----------------------------------------------------
        # Normal members:
        # exactly ONE open creator verification ticket.
        # ----------------------------------------------------

        if not owner:

            existing_ticket = get_open_ticket(
                user.id
            )

            if existing_ticket:

                existing_channel_id = (
                    existing_ticket["channel_id"]
                )

                existing_channel = None

                if existing_channel_id:
                    existing_channel = guild.get_channel(
                        int(existing_channel_id)
                    )

                # If the ticket still exists, NEVER create another.
                if existing_channel:

                    await interaction.response.send_message(
                        (
                            "You already have an active Creator "
                            "Verification ticket: "
                            f"{existing_channel.mention}\n\n"
                            "Use the **Continue YouTube Verification** "
                            "button inside that ticket to add another "
                            "YouTube/Google account."
                        ),
                        ephemeral=True,
                    )

                    return

                # The ticket was manually deleted.
                # Mark it closed so the member can create a new one.
                mark_ticket_closed(
                    existing_ticket["id"]
                )

        # ----------------------------------------------------
        # Claim a ticket.
        # ----------------------------------------------------

        if owner:

            ticket_id = create_owner_ticket(
                user.id,
                guild.id,
            )

        else:

            success, result = claim_normal_ticket(
                user.id,
                guild.id,
            )

            if not success:

                existing_ticket = result

                existing_channel = None

                if (
                    existing_ticket
                    and existing_ticket["channel_id"]
                ):

                    existing_channel = guild.get_channel(
                        int(existing_ticket["channel_id"])
                    )

                if existing_channel:

                    await interaction.response.send_message(
                        (
                            "You already have an active Creator "
                            "Verification ticket: "
                            f"{existing_channel.mention}"
                        ),
                        ephemeral=True,
                    )

                    return

                # Very unlikely race/recovery case.
                await interaction.response.send_message(
                    (
                        "You already have an active verification "
                        "session. Please use that ticket."
                    ),
                    ephemeral=True,
                )

                return

            ticket_id = result

        # ----------------------------------------------------
        # Find ticket category.
        # ----------------------------------------------------

        category = guild.get_channel(
            TICKET_CATEGORY_ID
        )

        if category is not None and not isinstance(
            category,
            discord.CategoryChannel,
        ):
            category = None

        bot_member = guild.me

        if bot_member is None:

            mark_ticket_failed(ticket_id)

            await interaction.response.send_message(
                "I could not find my bot member in this server.",
                ephemeral=True,
            )

            return

        # ----------------------------------------------------
        # Ticket permissions.
        # ----------------------------------------------------

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
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

        # Explicitly allow the server owner to see the ticket.
        if guild.owner:

            overwrites[guild.owner] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

        # ----------------------------------------------------
        # Safe ticket name.
        # ----------------------------------------------------

        safe_username = re.sub(
            r"[^a-zA-Z0-9-]",
            "-",
            user.name,
        ).strip("-")

        if not safe_username:
            safe_username = "user"

        safe_username = safe_username[:70]

        # ----------------------------------------------------
        # Create ONE ticket.
        # ----------------------------------------------------

        try:

            channel = await guild.create_text_channel(
                name=f"verify-{safe_username}",
                category=category,
                overwrites=overwrites,
                reason="YouTube creator milestone verification",
            )

        except discord.DiscordException as exc:

            print(
                f"Could not create verification channel: {exc}"
            )

            mark_ticket_failed(ticket_id)

            await interaction.response.send_message(
                (
                    "❌ I could not create your Creator Verification "
                    "ticket. Please check the bot's Discord permissions."
                ),
                ephemeral=True,
            )

            return

        # Save the actual channel ID.
        set_ticket_channel(
            ticket_id,
            channel.id,
        )

        # ----------------------------------------------------
        # Ticket embed.
        # ----------------------------------------------------

        embed = discord.Embed(
            title="🎥 Creator Milestone Verification",
            description=(
                "Welcome to Creator Milestone Verification.\n\n"
                "Use **Continue YouTube Verification** whenever "
                "you want to connect a Google/YouTube account."
            ),
            color=discord.Color.red(),
        )

        embed.add_field(
            name="How this works",
            value=(
                "• You can use **Continue YouTube Verification** "
                "multiple times.\n"
                "• Each Google account's accessible YouTube "
                "channels are checked.\n"
                "• A YouTube channel can only be verified once.\n"
                "• Your verified subscriber and view totals are "
                "combined.\n"
                "• When you are completely finished, press "
                "**Close Ticket**.\n"
                "• Your milestone roles will be calculated before "
                "you are removed from this ticket."
            ),
            inline=False,
        )

        embed.add_field(
            name="Important",
            value=(
                "Do not press Close Ticket until you have finished "
                "adding all of your YouTube accounts."
            ),
            inline=False,
        )

        if owner:

            embed.add_field(
                name="🛠️ Owner Testing Mode",
                value=(
                    "Server owner testing bypass is active. "
                    "The server owner may create and test "
                    "verification tickets without the normal "
                    "ticket limits."
                ),
                inline=False,
            )

        # ----------------------------------------------------
        # Ticket controls.
        # ----------------------------------------------------

        view = VerificationTicketView()

        try:

            await channel.send(
                content=user.mention,
                embed=embed,
                view=view,
            )

        except discord.DiscordException as exc:

            print(
                f"Could not send verification ticket message: {exc}"
            )

            mark_ticket_failed(ticket_id)

            try:
                await channel.delete(
                    reason="Verification ticket setup failed",
                )
            except discord.DiscordException:
                pass

            await interaction.response.send_message(
                (
                    "❌ I could not finish setting up your "
                    "verification ticket."
                ),
                ephemeral=True,
            )

            return

        await interaction.response.send_message(
            (
                "✅ Your Creator Verification ticket has been "
                f"created: {channel.mention}"
            ),
            ephemeral=True,
        )


# ============================================================
# CONTINUE YOUTUBE VERIFICATION BUTTON
# ============================================================

class ContinueVerificationButton(Button):

    def __init__(self):
        super().__init__(
            label="Continue YouTube Verification",
            style=discord.ButtonStyle.success,
            custom_id="continue_youtube_verification",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        user = interaction.user
        guild = interaction.guild
        channel = interaction.channel

        if guild is None or channel is None:

            await interaction.response.send_message(
                "This button can only be used inside a verification ticket.",
                ephemeral=True,
            )

            return

        ticket = get_ticket_by_channel(
            channel.id
        )

        if not ticket:

            await interaction.response.send_message(
                (
                    "This verification ticket is no longer active."
                ),
                ephemeral=True,
            )

            return

        ticket_owner_id = int(
            ticket["discord_user_id"]
        )

        owner = is_server_owner(
            user,
            guild,
        )

        # Only the ticket member or server owner may continue.
        if (
            user.id != ticket_owner_id
            and not owner
        ):

            await interaction.response.send_message(
                (
                    "Only the member who owns this verification "
                    "ticket or the server owner can use this button."
                ),
                ephemeral=True,
            )

            return

        # ----------------------------------------------------
        # Generate a fresh OAuth state every time.
        #
        # This means Continue can be used repeatedly.
        # ----------------------------------------------------

        cleanup_expired_oauth_states()

        state = secrets.token_urlsafe(32)

        oauth_states[state] = {
            "discord_user_id": ticket_owner_id,
            "guild_id": guild.id,
            "ticket_id": ticket["id"],
            "channel_id": channel.id,
            "created_at": datetime.now(
                timezone.utc
            ).timestamp(),
        }

        login_url = (
            f"{PUBLIC_BASE_URL}/login?"
            + urlencode(
                {
                    "state": state,
                }
            )
        )

        login_view = OAuthLoginView(
            login_url
        )

        await interaction.response.send_message(
            (
                "🔗 Click **Connect YouTube** below to connect "
                "the next Google/YouTube account.\n\n"
                "You can return to this ticket afterward and "
                "press **Continue YouTube Verification** again "
                "for another account."
            ),
            view=login_view,
            ephemeral=True,
        )


# ============================================================
# CLOSE TICKET BUTTON
# ============================================================

class CloseTicketButton(Button):

    def __init__(self):
        super().__init__(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            custom_id="close_creator_verification_ticket",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        user = interaction.user
        guild = interaction.guild
        channel = interaction.channel

        if guild is None or channel is None:

            await interaction.response.send_message(
                "This is not a valid verification ticket.",
                ephemeral=True,
            )

            return

        ticket = get_ticket_by_channel(
            channel.id
        )

        if not ticket:

            await interaction.response.send_message(
                "This verification ticket is no longer active.",
                ephemeral=True,
            )

            return

        ticket_owner_id = int(
            ticket["discord_user_id"]
        )

        owner = is_server_owner(
            user,
            guild,
        )

        # Only the member or server owner may close.
        if (
            user.id != ticket_owner_id
            and not owner
        ):

            await interaction.response.send_message(
                (
                    "Only the member who owns this ticket or "
                    "the server owner can close it."
                ),
                ephemeral=True,
            )

            return

        # ----------------------------------------------------
        # Recalculate totals at the exact moment the ticket
        # is closed.
        # ----------------------------------------------------

        total_subscribers, total_views = (
            get_aggregate_totals(
                ticket_owner_id
            )
        )

        # ----------------------------------------------------
        # Assign all applicable roles BEFORE removing the
        # member from the ticket.
        # ----------------------------------------------------

        role_result = await assign_roles(
            ticket_owner_id,
            total_subscribers,
            total_views,
            guild,
        )

        mark_ticket_closed(
            ticket["id"]
        )

        # ----------------------------------------------------
        # Tell the user what happened.
        # ----------------------------------------------------

        if role_result["added"]:

            added_roles_text = "\n".join(
                f"• {discord.utils.escape_markdown(role.name)}"
                for role in role_result["added"]
            )

            role_text = (
                "**Roles added:**\n"
                + added_roles_text
            )

        else:

            role_text = (
                "**Roles added:** None\n"
                "You did not currently meet a new milestone threshold."
            )

        embed = discord.Embed(
            title="✅ Creator Verification Complete",
            description=(
                "Your verification ticket is now complete.\n\n"
                "Your verified YouTube statistics were calculated "
                "and your applicable milestone roles were processed."
            ),
            color=discord.Color.green(),
        )

        embed.add_field(
            name="Combined Subscribers",
            value=f"{total_subscribers:,}",
            inline=True,
        )

        embed.add_field(
            name="Combined Views",
            value=f"{total_views:,}",
            inline=True,
        )

        embed.add_field(
            name="Milestone Roles",
            value=role_text,
            inline=False,
        )

        embed.set_footer(
            text=(
                "You have been removed from this ticket. "
                "A server administrator can delete the channel."
            )
        )

        try:

            await channel.send(
                embed=embed
            )

        except discord.DiscordException:
            pass

        # ----------------------------------------------------
        # Remove member from ticket.
        #
        # The channel is NOT deleted.
        # You can delete it manually.
        # ----------------------------------------------------

        ticket_member = guild.get_member(
            ticket_owner_id
        )

        if ticket_member:

            # Do not try to remove the server owner from their
            # own server.
            if ticket_owner_id != guild.owner_id:

                await remove_member_from_ticket(
                    channel,
                    ticket_member,
                )

        # If the owner is testing, keep them in the channel.
        # They can delete it manually afterward.

        if owner and user.id != guild.owner_id:

            await remove_member_from_ticket(
                channel,
                user,
            )

        # Give the interaction user a final confirmation.
        if interaction.response.is_done():

            try:
                await interaction.followup.send(
                    "✅ Ticket closed and verification finalized.",
                    ephemeral=True,
                )
            except discord.DiscordException:
                pass

        else:

            await interaction.response.send_message(
                "✅ Ticket closed and verification finalized.",
                ephemeral=True,
            )


# ============================================================
# TICKET VIEW
# ============================================================

class VerificationTicketView(View):

    def __init__(self):
        super().__init__(
            timeout=None
        )

        self.add_item(
            ContinueVerificationButton()
        )

        self.add_item(
            CloseTicketButton()
        )


# ============================================================
# MAIN VERIFICATION VIEW
# ============================================================

class VerifyView(View):

    def __init__(self):
        super().__init__(
            timeout=None
        )

        self.add_item(
            VerifyButton()
        )


# ============================================================
# OAUTH LOGIN VIEW
# ============================================================

class OAuthLoginButton(Button):

    def __init__(self, login_url):
        super().__init__(
            label="Connect YouTube",
            style=discord.ButtonStyle.link,
            url=login_url,
        )


class OAuthLoginView(View):

    def __init__(self, login_url):
        super().__init__(
            timeout=None
        )

        self.add_item(
            OAuthLoginButton(login_url)
        )


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

intents.members = True
intents.message_content = True


class VerificationBot(commands.Bot):

    async def setup_hook(self):

        # Main verification button.
        self.add_view(
            VerifyView()
        )

        # Ticket buttons.
        self.add_view(
            VerificationTicketView()
        )


bot = VerificationBot(
    command_prefix="$",
    intents=intents,
)


@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        "Discord bot is online."
    )


# ============================================================
# $ROLES
# ============================================================

@bot.command(
    name="roles"
)
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
        text=(
            "Click Verify / Add YouTube Account to begin."
        )
    )

    await ctx.send(
        embed=embed,
        view=VerifyView(),
    )


@roles_command.error
async def roles_command_error(
    ctx,
    error,
):

    if isinstance(
        error,
        commands.NotOwner,
    ):

        await ctx.send(
            "Only the bot owner can use this command."
        )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# GOOGLE LOGIN
# ============================================================

@app.get("/login")
async def login(
    request: Request,
    state: str,
):
    cleanup_expired_oauth_states()

    state_data = oauth_states.get(
        state
    )

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

    return RedirectResponse(
        url=google_url
    )


# ============================================================
# GOOGLE CALLBACK
# ============================================================

@app.get("/callback")
async def callback(
    request: Request,
):

    error = request.query_params.get(
        "error"
    )

    state = request.query_params.get(
        "state"
    )

    # --------------------------------------------------------
    # OAuth cancelled.
    # --------------------------------------------------------

    if error:

        if state:
            oauth_states.pop(
                state,
                None,
            )

        return HTMLResponse(
            (
                "<h2>Google verification was cancelled.</h2>"
                "<p>You can return to your Discord ticket and "
                "press Continue again.</p>"
            ),
            status_code=400,
        )

    if not state:

        return HTMLResponse(
            "<h2>Missing OAuth state.</h2>",
            status_code=400,
        )

    # --------------------------------------------------------
    # Consume OAuth state.
    # --------------------------------------------------------

    state_data = oauth_states.pop(
        state,
        None,
    )

    if not state_data:

        return HTMLResponse(
            (
                "<h2>This verification session is invalid "
                "or has expired.</h2>"
                "<p>Return to Discord and press Continue again.</p>"
            ),
            status_code=400,
        )

    # --------------------------------------------------------
    # Ten-minute expiration.
    # --------------------------------------------------------

    if (
        datetime.now(timezone.utc).timestamp()
        - state_data["created_at"]
        > 600
    ):

        return HTMLResponse(
            (
                "<h2>This verification session has expired.</h2>"
                "<p>Return to Discord and press Continue again.</p>"
            ),
            status_code=400,
        )

    code = request.query_params.get(
        "code"
    )

    if not code:

        return HTMLResponse(
            "<h2>Google did not return an authorization code.</h2>",
            status_code=400,
        )

    discord_user_id = int(
        state_data["discord_user_id"]
    )

    guild_id = int(
        state_data["guild_id"]
    )

    ticket_id = int(
        state_data["ticket_id"]
    )

    ticket_channel_id = int(
        state_data["channel_id"]
    )

    # --------------------------------------------------------
    # Make sure the ticket is still open.
    # --------------------------------------------------------

    connection = get_connection()

    ticket = connection.execute(
        """
        SELECT *
        FROM verification_tickets
        WHERE id = ?
        AND status = 'open'
        LIMIT 1
        """,
        (ticket_id,),
    ).fetchone()

    connection.close()

    if not ticket:

        return HTMLResponse(
            (
                "<h2>This verification ticket is no longer open.</h2>"
            ),
            status_code=409,
        )

    # --------------------------------------------------------
    # Make sure the bot still has the ticket channel.
    # --------------------------------------------------------

    guild = bot.get_guild(
        guild_id
    )

    if guild is None:

        return HTMLResponse(
            (
                "<h2>The Discord server could not be found.</h2>"
            ),
            status_code=404,
        )

    channel = guild.get_channel(
        ticket_channel_id
    )

    if channel is None:

        mark_ticket_closed(
            ticket_id
        )

        return HTMLResponse(
            (
                "<h2>The verification ticket no longer exists.</h2>"
            ),
            status_code=409,
        )

    # --------------------------------------------------------
    # Check whether this Discord member is the server owner.
    # --------------------------------------------------------

    owner_mode = (
        discord_user_id == guild.owner_id
    )

    try:

        # ----------------------------------------------------
        # Exchange authorization code.
        # ----------------------------------------------------

        token_data = exchange_code_for_token(
            code
        )

        access_token = token_data.get(
            "access_token"
        )

        if not access_token:

            raise RuntimeError(
                "Google did not return an access token."
            )

        # ----------------------------------------------------
        # Get Google account information.
        # ----------------------------------------------------

        google_user = get_google_user(
            access_token
        )

        google_sub = google_user.get(
            "sub"
        )

        google_email = google_user.get(
            "email",
            "Unknown email",
        )

        if not google_sub:

            raise RuntimeError(
                "Google did not return a stable account ID."
            )

        # ----------------------------------------------------
        # Get ALL YouTube channels accessible to this account.
        # ----------------------------------------------------

        channels = get_all_youtube_channels(
            access_token
        )

        if not channels:

            return HTMLResponse(
                (
                    "<h2>No YouTube channels were found.</h2>"
                    "<p>Make sure you signed into the Google account "
                    "that owns the YouTube channel.</p>"
                    "<p>Return to Discord and press Continue again "
                    "if you want to try another account.</p>"
                ),
                status_code=404,
            )

        # ----------------------------------------------------
        # DUPLICATE GOOGLE ACCOUNT CHECK
        #
        # Normal members cannot verify the same Google account
        # twice.
        #
        # Server owner bypasses this for testing.
        # ----------------------------------------------------

        existing_google_account = (
            get_google_account_by_sub(
                google_sub
            )
        )

        if (
            existing_google_account
            and not owner_mode
        ):

            if (
                str(
                    existing_google_account[
                        "discord_user_id"
                    ]
                )
                == str(discord_user_id)
            ):

                message = (
                    "This Gmail is already verified on your "
                    "Discord account."
                )

            else:

                message = (
                    "This Gmail is already verified by another "
                    "Discord account."
                )

            return HTMLResponse(
                (
                    "<h2>Google account already verified</h2>"
                    f"<p>{html.escape(message)}</p>"
                    "<p>Return to your Discord ticket and use "
                    "a different Google account.</p>"
                ),
                status_code=409,
            )

        # ----------------------------------------------------
        # DUPLICATE CHANNEL CHECK
        #
        # Every YouTube channel is globally unique for normal
        # verification.
        #
        # Owner bypasses this only for testing.
        # ----------------------------------------------------

        new_channels = []
        duplicate_channels = []

        for channel_data in channels:

            existing_channel = (
                get_verified_channel(
                    channel_data["channel_id"]
                )
            )

            if existing_channel:

                duplicate_channels.append(
                    channel_data
                )

            else:

                new_channels.append(
                    channel_data
                )

        # ----------------------------------------------------
        # NORMAL MEMBER:
        #
        # If even one channel from this Google account has
        # already been verified, block that duplicate.
        #
        # This is deliberately strict so nobody can use an
        # already-verified channel again.
        # ----------------------------------------------------

        if (
            duplicate_channels
            and not owner_mode
        ):

            duplicate_names = "<br>".join(
                (
                    f"• "
                    f"{html.escape(channel['channel_title'])}"
                )
                for channel in duplicate_channels
            )

            return HTMLResponse(
                (
                    "<h2>Channel already verified</h2>"
                    "<p>The following YouTube channel(s) are "
                    "already verified:</p>"
                    f"<p>{duplicate_names}</p>"
                    "<p>No new channels were added.</p>"
                    "<p>Return to your Discord ticket and use "
                    "Continue with another Google/YouTube account "
                    "if necessary.</p>"
                ),
                status_code=409,
            )

        # ----------------------------------------------------
        # OWNER TEST MODE
        #
        # The owner can repeat the same account/channel.
        #
        # We do NOT insert duplicate rows because channel_id
        # is intentionally unique in the database.
        #
        # Instead, owner testing reports the channels while
        # preserving the real database's duplicate protection.
        # ----------------------------------------------------

        if owner_mode:

            if new_channels:

                try:

                    save_google_account_and_channels(
                        discord_user_id=discord_user_id,
                        google_sub=google_sub,
                        google_email=google_email,
                        channels=new_channels,
                    )

                except sqlite3.IntegrityError as exc:

                    print(
                        f"Owner test database duplicate: {exc}"
                    )

            # For owner testing, duplicate channels are allowed
            # to complete the OAuth flow without creating
            # duplicate database rows.

            test_channels = channels

            account_subscribers = sum(
                channel["subscriber_count"]
                for channel in test_channels
            )

            account_views = sum(
                channel["view_count"]
                for channel in test_channels
            )

            combined_subscribers, combined_views = (
                get_aggregate_totals(
                    discord_user_id
                )
            )

            channel_names = "<br>".join(
                (
                    f"• {html.escape(channel['channel_title'])}"
                )
                for channel in test_channels
            )

            duplicate_note = ""

            if duplicate_channels:

                duplicate_note = (
                    "<p><strong>Owner test mode:</strong> "
                    "Previously verified channels were allowed "
                    "through this test. They were not duplicated "
                    "in the database.</p>"
                )

            return HTMLResponse(
                f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <title>Owner Test Verification Complete</title>

                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            max-width: 750px;
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

                        <h1>🛠️ Owner Test Complete</h1>

                        <p>
                            <strong>Google account:</strong>
                            {html.escape(google_email)}
                        </p>

                        <p>
                            <strong>Channels detected:</strong>
                        </p>

                        <p>
                            {channel_names}
                        </p>

                        {duplicate_note}

                        <hr>

                        <p>
                            <strong>This test account:</strong><br>
                            {account_subscribers:,} subscribers<br>
                            {account_views:,} views
                        </p>

                        <p>
                            <strong>Current stored total:</strong><br>
                            {combined_subscribers:,} subscribers<br>
                            {combined_views:,} views
                        </p>

                        <p>
                            Return to Discord and press
                            <strong>Continue YouTube Verification</strong>
                            again to test another OAuth flow.
                        </p>

                    </div>

                </body>
                </html>
                """,
                status_code=200,
            )

        # ----------------------------------------------------
        # Normal member should have at least one completely
        # new channel.
        # ----------------------------------------------------

        if not new_channels:

            return HTMLResponse(
                (
                    "<h2>No new YouTube channels were found.</h2>"
                    "<p>Every channel accessible to this Google "
                    "account has already been verified.</p>"
                    "<p>Return to Discord and use Continue with "
                    "another Google account if necessary.</p>"
                ),
                status_code=409,
            )

        # ----------------------------------------------------
        # SAVE NEW GOOGLE ACCOUNT + ALL ITS NEW CHANNELS.
        # ----------------------------------------------------

        account_subscribers, account_views = (
            save_google_account_and_channels(
                discord_user_id=discord_user_id,
                google_sub=google_sub,
                google_email=google_email,
                channels=new_channels,
            )
        )

        # ----------------------------------------------------
        # Recalculate combined totals.
        # ----------------------------------------------------

        combined_subscribers, combined_views = (
            get_aggregate_totals(
                discord_user_id
            )
        )

        channel_names = "<br>".join(
            (
                f"• {html.escape(channel['channel_title'])}"
            )
            for channel in new_channels
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # We DO NOT assign roles yet.
        #
        # The member can continue adding more accounts.
        # Roles are assigned when Close Ticket is pressed.
        # ----------------------------------------------------

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
                        max-width: 750px;
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

                    <h1>✅ YouTube Account Added</h1>

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
                        <strong>Your current combined total:</strong><br>
                        {combined_subscribers:,} subscribers<br>
                        {combined_views:,} views
                    </p>

                    <hr>

                    <p>
                        <strong>Next step:</strong>
                    </p>

                    <p>
                        Return to your Discord verification ticket.
                        If you have another Google/YouTube account,
                        press <strong>Continue YouTube Verification</strong>
                        again.
                    </p>

                    <p>
                        When you are completely finished,
                        press <strong>Close Ticket</strong>.
                    </p>

                    <p>
                        Your milestone roles will be processed when
                        the ticket is closed.
                    </p>

                </div>

            </body>

            </html>
            """,
            status_code=200,
        )

    # --------------------------------------------------------
    # Network failure.
    # --------------------------------------------------------

    except requests.RequestException as exc:

        print(
            f"OAuth/network error: {exc}"
        )

        return HTMLResponse(
            (
                "<h2>Google verification failed.</h2>"
                "<p>A network request to Google failed.</p>"
                "<p>Return to Discord and press Continue again.</p>"
            ),
            status_code=502,
        )

    # --------------------------------------------------------
    # Unexpected error.
    # --------------------------------------------------------

    except Exception as exc:

        print(
            f"Verification error: {exc}"
        )

        return HTMLResponse(
            (
                "<h2>Verification failed.</h2>"
                "<p>An unexpected error occurred.</p>"
                "<p>Return to Discord and press Continue again.</p>"
            ),
            status_code=500,
        )


# ============================================================
# GOOGLE API HELPERS
# ============================================================

def exchange_code_for_token(
    code,
):
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


def get_google_user(
    access_token,
):
    response = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",

        headers={
            "Authorization": (
                f"Bearer {access_token}"
            )
        },

        timeout=20,
    )

    response.raise_for_status()

    return response.json()


def get_all_youtube_channels(
    access_token,
):
    channels = []

    page_token = None

    while True:

        params = {
            "part": "snippet,statistics",
            "mine": "true",
            "maxResults": 50,
        }

        if page_token:

            params["pageToken"] = (
                page_token
            )

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",

            headers={
                "Authorization": (
                    f"Bearer {access_token}"
                )
            },

            params=params,

            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        for item in data.get(
            "items",
            [],
        ):

            statistics = item.get(
                "statistics",
                {}
            )

            snippet = item.get(
                "snippet",
                {}
            )

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

        page_token = data.get(
            "nextPageToken"
        )

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
    guild=None,
):
    added_roles = []
    failed_roles = []

    guilds_to_check = []

    if guild is not None:

        guilds_to_check.append(
            guild
        )

    else:

        guilds_to_check.extend(
            bot.guilds
        )

    for current_guild in guilds_to_check:

        member = current_guild.get_member(
            discord_user_id
        )

        if member is None:
            continue

        role_ids_to_add = set()

        for (
            role_name,
            (
                threshold,
                role_id,
            ),
        ) in ROLE_MAP.items():

            if "SUBS" in role_name:

                if (
                    total_subscribers
                    >= threshold
                ):

                    role_ids_to_add.add(
                        role_id
                    )

            elif "VIEWS" in role_name:

                if (
                    total_views
                    >= threshold
                ):

                    role_ids_to_add.add(
                        role_id
                    )

        for role_id in role_ids_to_add:

            role = current_guild.get_role(
                role_id
            )

            if role is None:

                failed_roles.append(
                    str(role_id)
                )

                print(
                    f"Role {role_id} was not found "
                    f"in guild {current_guild.id}."
                )

                continue

            try:

                if role not in member.roles:

                    await member.add_roles(
                        role,
                        reason=(
                            "YouTube creator milestone "
                            "verification"
                        ),
                    )

                    added_roles.append(
                        role
                    )

            except discord.DiscordException as exc:

                failed_roles.append(
                    role.name
                )

                print(
                    f"Could not add role "
                    f"{role_id} to {member}: {exc}"
                )

    return {
        "added": added_roles,
        "failed": failed_roles,
    }


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

            <p>
                The verification service is online.
            </p>

            <p>
                <a href="/privacy">
                    Privacy Policy
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
                This service uses Google OAuth to verify
                YouTube channel statistics.
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
            </p>

            <p>
                OAuth access tokens are not stored by this
                application.
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
                creator milestones within the associated
                Discord server.
            </p>

            <p>
                Users must only connect Google accounts and
                YouTube channels that they are authorized to verify.
            </p>

            <p>
                Server administrators may change or remove
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
    bot.run(
        BOT_TOKEN
    )


if __name__ == "__main__":

    validate_configuration()

    init_database()

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True,
    )

    bot_thread.start()

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
