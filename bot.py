import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta
import asyncio
from dotenv import load_dotenv
import pytz
from typing import Optional, List, Literal, Union
import math
import sqlite3
import traceback
import logging
import time
import sys
import aiohttp

# Setup logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('pingur')

# Load and verify environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    logger.error("No Discord token found in environment variables!")
    logger.error("Make sure you have a .env file with DISCORD_TOKEN=your_token")
    sys.exit(1)

# Constants
ITEMS_PER_PAGE = 5
TIME_UNITS = {
    'minutes': 1,
    'hours': 60,
    'days': 1440
}

# Ensure the database directory exists
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reminders.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Set up required bot intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class CommandRegistrationError(Exception):
    pass

class PingurBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.reminder_cache = {}
        self.guild_settings_cache = {}
        self.cache_lock = asyncio.Lock()
        logger.info("Bot initialization started")

    async def setup_hook(self):
        """Initial setup and command registration"""
        logger.info("Starting setup...")
        
        # Initialize database first
        try:
            await setup_database()
            logger.info("Database setup complete")
        except Exception as e:
            logger.error(f"Database setup failed: {e}")
            raise

        # Then register commands
        try:
            logger.info("Starting command registration...")
            # Sync commands globally
            await self.tree.sync()
            logger.info("Global commands synced")
            
            # Sync to all guilds
            for guild in self.guilds:
                try:
                    await self.tree.sync(guild=guild)
                    logger.info(f"Commands synced to guild: {guild.name}")
                except Exception as e:
                    logger.error(f"Failed to sync commands to guild {guild.name}: {e}")
            
            logger.info("Command registration complete")
        except Exception as e:
            logger.error(f"Command registration failed: {e}")
            raise

    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"Logged in as: {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guilds")
        
        try:
            # Start the reminder check loop
            if not check_reminders.is_running():
                check_reminders.start()
                logger.info("Started reminder check loop")
            
            logger.info("Bot is fully ready!")
        except Exception as e:
            logger.error(f"Error during ready event: {e}")
            traceback.print_exc()

    async def on_guild_join(self, guild):
        """Handle new guild joins"""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        try:
            await self.tree.sync(guild=guild)
            logger.info(f"Synced commands to new guild {guild.name}")
        except Exception as e:
            logger.error(f"Failed to sync commands to new guild {guild.name}: {e}")

# Error handling decorator for database operations
def db_operation(operation):
    async def wrapper(*args, **kwargs):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                return await operation(db, *args, **kwargs)
        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            traceback.print_exc()
            return None
        except Exception as e:
            logger.error(f"Operation error: {e}")
            traceback.print_exc()
            return None
    return wrapper

# Permission checking
def check_permissions(interaction: discord.Interaction) -> bool:
    permissions = interaction.channel.permissions_for(interaction.guild.me)
    required_permissions = [
        "send_messages",
        "embed_links",
        "add_reactions",
        "read_message_history",
        "manage_messages"
    ]
    missing = [perm for perm in required_permissions if not getattr(permissions, perm)]
    if missing:
        raise commands.MissingPermissions(missing)
    return True

# Rate limiting decorator
def cooldown(rate: int, per: float):
    def decorator(func):
        cooldowns = {}
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            now = time.time()
            key = (interaction.user.id, func.__name__)
            if key in cooldowns:
                remaining = cooldowns[key] - now
                if remaining > 0:
                    await interaction.response.send_message(
                        f"Please wait {remaining:.1f}s before using this command again.",
                        ephemeral=True
                    )
                    return
            cooldowns[key] = now + per
            return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator

def parse_time(time_str: str, tz: pytz.timezone) -> Optional[datetime]:
    now = datetime.now(tz)
    try:
        if time_str.lower() == 'tomorrow':
            return now.replace(hour=9, minute=0) + timedelta(days=1)
        elif 'tomorrow' in time_str.lower():
            time_part = time_str.lower().replace('tomorrow', '').strip()
            if 'm' in time_part.lower():
                parsed_time = datetime.strptime(time_part, '%I%p').time()
            else:
                parsed_time = datetime.strptime(time_part, '%H:%M').time()
            return now.replace(hour=parsed_time.hour, minute=parsed_time.minute) + timedelta(days=1)
        else:
            if 'm' in time_str.lower():
                parsed_time = datetime.strptime(time_str, '%I%p').time()
            else:
                parsed_time = datetime.strptime(time_str, '%H:%M').time()
            result = now.replace(hour=parsed_time.hour, minute=parsed_time.minute)
            if result < now:
                result += timedelta(days=1)
            return result
    except ValueError:
        return None

bot = PingurBot()

# Database initialization with improved schema
@db_operation
async def setup_database(db):
    # First, check if we need to add the ghost_ping column
    async with db.execute("PRAGMA table_info(reminders)") as cursor:
        columns = await cursor.fetchall()
        has_ghost_ping = any(col[1] == 'ghost_ping' for col in columns)

    if not has_ghost_ping:
        logger.info("Adding ghost_ping column to reminders table...")
        try:
            # Create a backup of the old table
            await db.execute('''
                CREATE TABLE reminders_backup AS SELECT * FROM reminders
            ''')
            
            # Drop the old table
            await db.execute('DROP TABLE reminders')
            
            # Create the new table with all columns
            await db.execute('''
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    user_id INTEGER NOT NULL,
                    target_ids TEXT NOT NULL,
                    target_type TEXT DEFAULT 'user' CHECK(target_type IN ('user', 'role')),
                    message TEXT NOT NULL,
                    interval INTEGER NOT NULL,
                    time_unit TEXT DEFAULT 'minutes' CHECK(time_unit IN ('minutes', 'hours', 'days')),
                    last_ping TIMESTAMP,
                    next_ping TIMESTAMP NOT NULL,
                    dm BOOLEAN DEFAULT false,
                    active BOOLEAN DEFAULT true,
                    recurring BOOLEAN DEFAULT true,
                    ghost_ping BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
                )
            ''')
            
            # Copy data from backup to new table, explicitly setting ghost_ping to false
            await db.execute('''
                INSERT INTO reminders (
                    id, guild_id, channel_id, user_id, target_ids, target_type,
                    message, interval, time_unit, last_ping, next_ping,
                    dm, active, recurring, ghost_ping, created_at
                )
                SELECT 
                    id, guild_id, channel_id, user_id, target_ids, target_type,
                    message, interval, time_unit, last_ping, next_ping,
                    dm, active, recurring, 0, created_at
                FROM reminders_backup
            ''')
            
            # Drop the backup table
            await db.execute('DROP TABLE reminders_backup')
            
            # Update any existing reminders to ensure ghost_ping is false
            await db.execute('UPDATE reminders SET ghost_ping = 0 WHERE ghost_ping IS NULL')
            
            await db.commit()
            logger.info("Successfully added ghost_ping column and migrated data")
        except Exception as e:
            logger.error(f"Error adding ghost_ping column: {str(e)}")
            # Try to restore from backup if something went wrong
            try:
                await db.execute('DROP TABLE IF EXISTS reminders')
                await db.execute('ALTER TABLE reminders_backup RENAME TO reminders')
                await db.commit()
                logger.info("Restored from backup after error")
            except Exception as restore_error:
                logger.error(f"Failed to restore from backup: {str(restore_error)}")
            raise
    else:
        # Create the table if it doesn't exist
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                user_id INTEGER NOT NULL,
                target_ids TEXT NOT NULL,
                target_type TEXT DEFAULT 'user' CHECK(target_type IN ('user', 'role')),
                message TEXT NOT NULL,
                interval INTEGER NOT NULL,
                time_unit TEXT DEFAULT 'minutes' CHECK(time_unit IN ('minutes', 'hours', 'days')),
                last_ping TIMESTAMP,
                next_ping TIMESTAMP NOT NULL,
                dm BOOLEAN DEFAULT false,
                active BOOLEAN DEFAULT true,
                recurring BOOLEAN DEFAULT true,
                ghost_ping BOOLEAN DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            )
        ''')
    
    await db.execute('''
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            default_channel_id INTEGER,
            timezone TEXT DEFAULT 'UTC'
        )
    ''')
    
    await db.execute('''
        CREATE TABLE IF NOT EXISTS reminder_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            time TEXT,
            targets TEXT,
            UNIQUE(guild_id, name)
        )
    ''')
    
    await db.commit()
    logger.info("Database initialized successfully")

def format_time(minutes: int) -> str:
    """Convert minutes to a readable format"""
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif minutes < 1440:
        hours = minutes / 60
        return f"{hours:.1f} hour{'s' if hours != 1 else ''}"
    else:
        days = minutes / 1440
        return f"{days:.1f} day{'s' if days != 1 else ''}"

async def create_reminder_embed(interaction: discord.Interaction, reminder: tuple, show_controls: bool = False) -> discord.Embed:
    """Create an embed for a reminder"""
    rid, guild_id, channel_id, user_id, target_ids, target_type, msg, interval, time_unit, last_ping, next_ping, dm, active, recurring, created_at = reminder
    
    embed = discord.Embed(
        title=f"Reminder #{rid}",
        color=discord.Color.green() if active else discord.Color.red()
    )

    # Get targets (users or roles)
    targets = []
    for tid in target_ids.split(','):
        if target_type == 'user':
            target = interaction.guild.get_member(int(tid))
        else:
            target = interaction.guild.get_role(int(tid))
        if target:
            targets.append(target.mention)

    channel = interaction.guild.get_channel(channel_id)
    creator = interaction.guild.get_member(user_id)

    # Add fields
    embed.add_field(
        name="📌 Targets",
        value=', '.join(targets) or "No valid targets",
        inline=False
    )
    embed.add_field(
        name="⏰ Timing",
        value=(
            f"Interval: {format_time(interval)}\n"
            f"Type: {'Recurring' if recurring else 'One-time'}\n"
            f"Next ping: <t:{int(datetime.fromisoformat(next_ping).timestamp())}:R>"
        ),
        inline=True
    )
    embed.add_field(
        name="Location",
        value=f"Channel: {channel.mention if channel else 'Unknown'}\nDM: {dm}",
        inline=True
    )
    embed.add_field(
        name="ℹ️ Details",
        value=f"Status: {'🟢 Active' if active else '🔴 Inactive'}",
        inline=True
    )
    embed.add_field(
        name="💬 Message",
        value=msg,
        inline=False
    )

    if creator:
        embed.set_footer(text=f"Created by {creator.display_name}")

    if show_controls:
        embed.description = (
            "**Controls**\n"
            "🗑️ - Delete reminder\n"
            "✏️ - Edit reminder\n"
            "⏯️ - Toggle active status\n"
            "◀️ ▶️ - Navigate pages"
        )

    return embed

@bot.tree.command(name="setchannel", description="Set the default channel for reminders in this server")
@app_commands.describe(
    channel="The channel to use for reminders by default"
)
async def set_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO guild_settings (guild_id, default_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) 
            DO UPDATE SET default_channel_id = excluded.default_channel_id
        ''', (interaction.guild_id, channel.id))
        await db.commit()

    embed = discord.Embed(
        title="✅ Default Channel Set",
        description=f"Default reminder channel set to {channel.mention}",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addping", description="Add an interval-based ping (e.g., every X minutes/hours/days)")
@app_commands.describe(
    targets="Users/Roles to remind (mention them, use IDs, or separate multiple with spaces)",
    time_unit="Time unit (minutes/hours/days)",
    interval="Number of time units between pings",
    message="Message to send with the ping",
    dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send ping (optional, uses current channel if not specified)"
)
async def add_ping(
    interaction: discord.Interaction,
    targets: str,
    time_unit: Literal['minutes', 'hours', 'days'],
    interval: int,
    message: str,
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None
):
    try:
        await interaction.response.defer()
        
        # Get server timezone
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT timezone FROM guild_settings WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                result = await cursor.fetchone()
                timezone = result[0] if result else 'UTC'

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Parse targets (users and roles)
        target_ids = []
        target_type = None
        
        for word in targets.split():
            target_id = None
            is_role = False

            if word.startswith('<@&'):  # Role mention
                try:
                    target_id = int(word[3:-1])
                    is_role = True
                except ValueError as e:
                    logger.error(f"Failed to parse role ID from {word}: {str(e)}")
                    continue
            elif word.startswith('<@'):  # User mention
                try:
                    target_id = int(word[2:-1].replace('!', ''))
                except ValueError as e:
                    logger.error(f"Failed to parse user ID from {word}: {str(e)}")
                    continue
            else:  # Raw ID
                try:
                    target_id = int(word)
                    # Check if it's a role ID
                    role = interaction.guild.get_role(target_id)
                    if role:
                        is_role = True
                    else:
                        # Check if it's a valid user ID
                        member = interaction.guild.get_member(target_id)
                        if not member:
                            logger.error(f"Could not find member or role with ID {target_id}")
                            continue
                except ValueError as e:
                    logger.error(f"Failed to parse ID from {word}: {str(e)}")
                    continue

            if target_id:
                if not target_type:
                    target_type = 'role' if is_role else 'user'
                elif (target_type == 'role') != is_role:
                    await interaction.followup.send(
                        "Cannot mix users and roles in the same ping!",
                        ephemeral=True
                    )
                    return
                target_ids.append(target_id)

        if not target_ids:
            await interaction.followup.send(
                "No valid targets found! Please mention users/roles or use their IDs.",
                ephemeral=True
            )
            return

        # Verify all targets exist
        invalid_ids = []
        for tid in target_ids:
            if target_type == 'user':
                if not interaction.guild.get_member(tid):
                    invalid_ids.append(str(tid))
            else:
                if not interaction.guild.get_role(tid):
                    invalid_ids.append(str(tid))
        
        if invalid_ids:
            await interaction.followup.send(
                f"Some {target_type}s were not found: {', '.join(invalid_ids)}",
                ephemeral=True
            )
            return

        if target_type == 'role' and dm:
            await interaction.followup.send(
                "Cannot send DMs to roles! Please use channel mentions for roles.",
                ephemeral=True
            )
            return

        # Get channel ID
        if channel:
            channel_id = channel.id
        elif dm:
            channel_id = None
        else:
            # First try to use the current channel
            channel_id = interaction.channel_id
            if not channel_id:
                # If not in a channel, try to use the default channel
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute('SELECT default_channel_id FROM guild_settings WHERE guild_id = ?', 
                                        (interaction.guild_id,)) as cursor:
                        result = await cursor.fetchone()
                        channel_id = result[0] if result else None

                if not channel_id:
                    await interaction.followup.send(
                        "No channel specified and no default channel set! Please specify a channel or use /setchannel to set a default.",
                        ephemeral=True
                    )
                    return

        # Calculate next ping time
        interval_minutes = interval * TIME_UNITS[time_unit]
        next_ping = now + timedelta(minutes=interval_minutes)
        
        # Ensure timezone-aware datetime storage
        if next_ping.tzinfo is None:
            next_ping = tz.localize(next_ping)
        next_ping = next_ping.astimezone(pytz.utc)

        # Insert the reminder
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('''
                INSERT INTO reminders (
                    guild_id, channel_id, user_id, target_ids, target_type,
                    message, interval, time_unit, last_ping, next_ping,
                    dm, recurring, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                interaction.guild_id, 
                channel_id,
                interaction.user.id,
                ','.join(map(str, target_ids)),
                target_type,
                message,
                interval,
                time_unit,
                now.astimezone(pytz.utc).isoformat(),  # Store in UTC
                next_ping.isoformat(),
                dm,
                True,  # Always recurring for interval-based pings
                True
            ))
            await db.commit()
            
            # Get the ID of the inserted reminder
            cursor = await db.execute('SELECT last_insert_rowid()')
            row = await cursor.fetchone()
            reminder_id = row[0]

        # Get targets for display
        targets_display = []
        for tid in target_ids:
            if target_type == 'user':
                target = interaction.guild.get_member(int(tid))
            else:
                target = interaction.guild.get_role(int(tid))
            if target:
                targets_display.append(target.mention)

        # Get channel for display
        channel_display = interaction.guild.get_channel(channel_id) if channel_id else None

        # Get location string
        if dm:
            location = "📱 DM"
        else:
            location = "📢 Unknown"
            if channel_display:
                location = f"📢 {channel_display.mention}"

        embed = discord.Embed(
            title="✅ New Ping Created",
            description=f"**ID:** #{reminder_id}\n" +
                       f"**Interval:** Every {interval} {time_unit}\n" +
                       f"**To:** {', '.join(targets_display) or 'No targets'}\n" +
                       f"**Where:** {location}\n" +
                       f"**Message:** {message}",
            color=discord.Color.green()
        )
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in add_ping command: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred while creating the ping. Please try again later.",
            ephemeral=True
        )

@bot.tree.command(name="addreminder", description="Add a time-based reminder (e.g., daily at 3pm)")
@app_commands.describe(
    targets="Users/Roles to remind (mention them or use IDs)",
    time="When to send the reminder (e.g., '3pm', '15:00', 'tomorrow 3pm')",
    message="Message to send with the reminder",
    repeat="How often to repeat the reminder",
    dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send reminder (optional, uses current channel if not specified)"
)
async def add_reminder(
    interaction: discord.Interaction,
    targets: str,
    time: str,
    message: str,
    repeat: Literal['never', 'daily', 'weekly'] = 'never',  # Default to one-time reminder
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None
):
    try:
        await interaction.response.defer()
        
        # Get server timezone
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT timezone FROM guild_settings WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                result = await cursor.fetchone()
                timezone = result[0] if result else 'UTC'

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Parse the time string
        target_time = parse_time(time, tz)
        if not target_time:
            await interaction.followup.send(
                "❌ Invalid time format! Examples:\n" +
                "• `3pm` - At 3 PM\n" +
                "• `15:00` - At 3 PM (24h format)\n" +
                "• `tomorrow 3pm` - Tomorrow at 3 PM",
                ephemeral=True
            )
            return

        # Parse targets (users and roles)
        target_ids = []
        target_type = None
        
        for word in targets.split():
            target_id = None
            is_role = False

            if word.startswith('<@&'):  # Role mention
                try:
                    target_id = int(word[3:-1])
                    is_role = True
                except ValueError as e:
                    logger.error(f"Failed to parse role ID from {word}: {str(e)}")
                    continue
            elif word.startswith('<@'):  # User mention
                try:
                    target_id = int(word[2:-1].replace('!', ''))
                except ValueError as e:
                    logger.error(f"Failed to parse user ID from {word}: {str(e)}")
                    continue
            else:  # Raw ID
                try:
                    target_id = int(word)
                    # Check if it's a role ID
                    role = interaction.guild.get_role(target_id)
                    if role:
                        is_role = True
                    else:
                        # Check if it's a valid user ID
                        member = interaction.guild.get_member(target_id)
                        if not member:
                            logger.error(f"Could not find member or role with ID {target_id}")
                            continue
                except ValueError as e:
                    logger.error(f"Failed to parse ID from {word}: {str(e)}")
                    continue

            if target_id:
                if not target_type:
                    target_type = 'role' if is_role else 'user'
                elif (target_type == 'role') != is_role:
                    await interaction.followup.send(
                        "Cannot mix users and roles in the same reminder!",
                        ephemeral=True
                    )
                    return
                target_ids.append(target_id)

        if not target_ids:
            await interaction.followup.send(
                "No valid targets found! Please mention users/roles or use their IDs.",
                ephemeral=True
            )
            return

        # Verify all targets exist
        invalid_ids = []
        for tid in target_ids:
            if target_type == 'user':
                if not interaction.guild.get_member(tid):
                    invalid_ids.append(str(tid))
            else:
                if not interaction.guild.get_role(tid):
                    invalid_ids.append(str(tid))
        
        if invalid_ids:
            await interaction.followup.send(
                f"Some {target_type}s were not found: {', '.join(invalid_ids)}",
                ephemeral=True
            )
            return

        if target_type == 'role' and dm:
            await interaction.followup.send(
                "Cannot send DMs to roles! Please use channel mentions for roles.",
                ephemeral=True
            )
            return

        # Get channel ID
        if channel:
            channel_id = channel.id
        elif dm:
            channel_id = None
        else:
            # First try to use the current channel
            channel_id = interaction.channel_id
            if not channel_id:
                # If not in a channel, try to use the default channel
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute('SELECT default_channel_id FROM guild_settings WHERE guild_id = ?', 
                                        (interaction.guild_id,)) as cursor:
                        result = await cursor.fetchone()
                        channel_id = result[0] if result else None

                if not channel_id:
                    await interaction.followup.send(
                        "No channel specified and no default channel set! Please specify a channel or use /setchannel to set a default.",
                        ephemeral=True
                    )
                    return

        # Set interval based on repeat type
        if repeat == 'never':
            interval = int((target_time - now).total_seconds() / 60)
            recurring = False
        elif repeat == 'daily':
            interval = 1440  # 24 hours in minutes
            recurring = True
        else:  # weekly
            interval = 10080  # 7 days in minutes
            recurring = True

        # Insert the reminder
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('''
                INSERT INTO reminders (
                    guild_id, channel_id, user_id, target_ids, target_type,
                    message, interval, time_unit, last_ping, next_ping,
                    dm, recurring, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                interaction.guild_id, 
                channel_id,
                interaction.user.id,
                ','.join(map(str, target_ids)),
                target_type,
                message,
                interval,
                'minutes',
                now.isoformat(),
                target_time.isoformat(),
                dm,
                recurring,
                True
            ))
            await db.commit()

            # Get the ID of the inserted reminder
            cursor = await db.execute('SELECT last_insert_rowid()')
            row = await cursor.fetchone()
            reminder_id = row[0]

            # Fetch the newly created reminder
            async with db.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,)) as cursor:
                reminder = await cursor.fetchone()

        embed = await create_reminder_embed(interaction, reminder)
        embed.title = "✅ New Reminder Created"
        embed.add_field(
            name="🕒 Schedule",
            value=f"At {target_time.strftime('%I:%M %p')} {timezone}\nRepeat: {repeat}",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in add_reminder command: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred while creating the reminder. Please try again later.",
            ephemeral=True
        )

@bot.tree.command(name="savetemplate", description="Save a reminder as a template")
@app_commands.describe(
    name="Name for the template",
    message="Message for the template",
    time="Default time for the template (optional)",
    targets="Default targets for the template (optional)"
)
async def save_template(
    interaction: discord.Interaction,
    name: str,
    message: str,
    time: Optional[str] = None,
    targets: Optional[str] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute('''
                INSERT INTO reminder_templates (guild_id, name, message, time, targets)
                VALUES (?, ?, ?, ?, ?)
            ''', (interaction.guild_id, name, message, time, targets))
            await db.commit()

            embed = discord.Embed(
                title="✅ Template Saved",
                description=f"Template `{name}` has been saved",
                color=discord.Color.green()
            )
            embed.add_field(name="Message", value=message, inline=False)
            if time:
                embed.add_field(name="Default Time", value=time, inline=True)
            if targets:
                embed.add_field(name="Default Targets", value=targets, inline=True)

            await interaction.response.send_message(embed=embed)
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                f"❌ A template named `{name}` already exists!",
                ephemeral=True
            )

@bot.tree.command(name="usetemplate", description="Create a reminder from a template")
@app_commands.describe(
    template_name="Name of the template to use",
    time="Override the default time (optional)",
    targets="Override the default targets (optional)",
    dm="Send as DM instead of channel message",
    channel="Override the default channel"
)
async def use_template(
    interaction: discord.Interaction,
    template_name: str,
    time: Optional[str] = None,
    targets: Optional[str] = None,
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT * FROM reminder_templates WHERE guild_id = ? AND name = ?',
            (interaction.guild_id, template_name)
        ) as cursor:
            template = await cursor.fetchone()

        if not template:
            await interaction.response.send_message(
                f"❌ Template `{template_name}` not found!",
                ephemeral=True
            )
            return

        # Use template values or overrides
        final_time = time or template[3]
        final_targets = targets or template[4]
        
        if not final_time:
            await interaction.response.send_message(
                "❌ No time specified! Please provide a time.",
                ephemeral=True
            )
            return

        if not final_targets:
            await interaction.response.send_message(
                "❌ No targets specified! Please provide targets.",
                ephemeral=True
            )
            return

        # Create the reminder using the template
        await add_reminder(
            interaction=interaction,
            targets=final_targets,
            time=final_time,
            repeat=template[5],
            message=template[2],  # template message
            dm=dm,
            channel=channel
        )

@bot.tree.command(name="listtemplates", description="List all saved reminder templates")
async def list_templates(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT * FROM reminder_templates WHERE guild_id = ?',
            (interaction.guild_id,)
        ) as cursor:
            templates = await cursor.fetchall()

    if not templates:
        await interaction.response.send_message(
            "❌ No templates found! Create one with `/savetemplate`",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📋 Reminder Templates",
        color=discord.Color.blue()
    )

    for template in templates:
        name = template[2]
        message = template[3]
        time = template[4] or "Not set"
        targets = template[5] or "Not set"

        embed.add_field(
            name=f"📝 {name}",
            value=f"Message: {message}\nTime: {time}\nTargets: {targets}",
            inline=False
        )

    await interaction.response.send_message(embed=embed)

class ListView(discord.ui.View):
    def __init__(self, reminders, timezone, type):
        super().__init__(timeout=300)
        self.reminders = reminders
        self.timezone = timezone
        self.type = type
        self.page = 0
        self.max_pages = math.ceil(len(reminders) / ITEMS_PER_PAGE)
        self.update_button_states()

    def update_button_states(self):
        # Update Previous button state
        self.prev_button.disabled = self.page <= 0
        # Update Next button state
        self.next_button.disabled = self.page >= self.max_pages - 1

    def get_embed(self) -> discord.Embed:
        start_idx = self.page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, len(self.reminders))
        current_reminders = self.reminders[start_idx:end_idx]

        embed = discord.Embed(
            title=f"📋 {'Pings' if self.type == 'pings' else 'Reminders'} List",
            description=f"Page {self.page + 1}/{self.max_pages}",
            color=discord.Color.blue()
        )

        for reminder in current_reminders:
            rid, guild_id, channel_id, user_id, target_ids, target_type, msg, interval, time_unit, last_ping, next_ping, dm, active, recurring, created_at = reminder
            
            # Format the next ping time
            next_ping_dt = datetime.fromisoformat(next_ping)
            next_ping_str = f"<t:{int(next_ping_dt.timestamp())}:R>"

            # Format the interval
            if recurring:
                interval_str = f"Every {interval} {time_unit}"
            else:
                interval_str = "One-time"

            # Format status
            status = "🟢 Active" if active else "🔴 Inactive"

            embed.add_field(
                name=f"#{rid} - {status}",
                value=f"⏰ Next: {next_ping_str}\n📅 {interval_str}\n💬 {msg[:100]}{'...' if len(msg) > 100 else ''}",
                inline=False
            )

        if not current_reminders:
            embed.description = f"No {'pings' if self.type == 'pings' else 'reminders'} found."

        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray, emoji="◀️")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.update_button_states()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray, emoji="▶️")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages - 1, self.page + 1)
        self.update_button_states()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

@bot.tree.command(name="list", description="View upcoming reminders in chronological order")
@app_commands.describe(
    type="Type of items to list (pings or reminders)"
)
async def list_reminders(
    interaction: discord.Interaction,
    type: Literal['pings', 'reminders'] = 'pings'
):
    try:
        await interaction.response.defer()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Get timezone
            async with db.execute('SELECT timezone FROM guild_settings WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                result = await cursor.fetchone()
                timezone = result[0] if result else 'UTC'

            # Get reminders
            query = '''
                SELECT *
                FROM reminders
                WHERE guild_id = ? AND recurring = ?
                ORDER BY next_ping ASC
            '''
            
            async with db.execute(query, (interaction.guild_id, type == 'pings')) as cursor:
                reminders = await cursor.fetchall()

        if not reminders:
            await interaction.followup.send(
                f"❌ No {type} found!",
                ephemeral=True
            )
            return

        view = ListView(reminders, timezone, type)
        await interaction.followup.send(embed=view.get_embed(), view=view)
    except Exception as e:
        logger.error(f"Error in list command: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred while fetching items. Please try again later.",
            ephemeral=True
        )

@tasks.loop(seconds=30)  # Check more frequently for accuracy
async def check_reminders():
    """Check and send reminders"""
    try:
        now = datetime.now(pytz.utc)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('''
                SELECT * FROM reminders 
                WHERE active = 1 AND next_ping <= ?
            ''', (now.isoformat(),)) as cursor:
                reminders = await cursor.fetchall()

            for reminder in reminders:
                try:
                    # Properly unpack all fields including ghost_ping
                    (id, guild_id, channel_id, user_id, target_ids_str, target_type, 
                     message, interval, time_unit, last_ping, next_ping, is_dm, active, 
                     recurring, ghost_ping, created_at) = reminder
                    
                    # Ensure ghost_ping is a boolean and has a default value
                    ghost_ping = bool(ghost_ping) if ghost_ping is not None else False
                    
                    guild = bot.get_guild(guild_id)
                    if not guild:
                        logger.error(f'Could not find guild {guild_id} for reminder {id}')
                        continue

                    # Get targets
                    target_ids = [int(tid) for tid in target_ids_str.split(',')]
                    targets = []
                    for tid in target_ids:
                        if target_type == 'user':
                            target = guild.get_member(tid)
                        else:
                            target = guild.get_role(tid)
                        if target:
                            targets.append(target)

                    if not targets:
                        logger.error(f'No valid targets found for reminder {id} in guild {guild.name}')
                        continue

                    try:
                        if is_dm and target_type == 'user':
                            for target in targets:
                                await target.send(f'{message}')
                        else:
                            channel = guild.get_channel(channel_id)
                            if channel:
                                mentions = ' '.join(target.mention for target in targets)
                                sent_message = await channel.send(f'{mentions} {message}')
                                
                                # Only delete if this is explicitly a ghost ping
                                if ghost_ping and sent_message:
                                    try:
                                        await asyncio.sleep(0.1)  # Brief delay to ensure the ping goes through
                                        await sent_message.delete()
                                        logger.info(f"Successfully deleted ghost ping message for reminder {id}")
                                    except Exception as e:
                                        logger.error(f'Failed to delete ghost ping message for reminder {id}: {str(e)}')

                        # Update last ping and next ping times
                        if recurring:
                            interval_minutes = interval * TIME_UNITS[time_unit]
                            # Calculate next ping time, ensuring it's in the future
                            next_ping_time = now
                            while next_ping_time <= now:
                                next_ping_time += timedelta(minutes=interval_minutes)
                            
                            await db.execute('''
                                UPDATE reminders 
                                SET last_ping = ?, next_ping = ?
                                WHERE id = ?
                            ''', (now.isoformat(), next_ping_time.isoformat(), id))
                        else:
                            # For one-time reminders, deactivate after sending
                            await db.execute('''
                                UPDATE reminders 
                                SET active = 0, last_ping = ?
                                WHERE id = ?
                            ''', (now.isoformat(), id))
                        
                        await db.commit()
                        logger.info(f"Successfully processed reminder {id} for guild {guild.name}")
                    except discord.Forbidden:
                        logger.error(f'Failed to send message to targets in guild {guild.name} - Missing permissions')
                    except Exception as e:
                        logger.error(f'Error processing reminder {id}: {str(e)}')
                        traceback.print_exc()
                except Exception as e:
                    logger.error(f'Error processing reminder {id}: {str(e)}')
                    traceback.print_exc()
                    continue

    except Exception as e:
        logger.error(f"Error in check_reminders: {str(e)}")
        traceback.print_exc()

@check_reminders.before_loop
async def before_check_reminders():
    """Wait for the bot to be ready before starting the reminder check loop"""
    await bot.wait_until_ready()
    logger.info("Starting reminder check loop...")

@check_reminders.after_loop
async def after_check_reminders():
    """Log when the reminder check loop stops"""
    if check_reminders.is_being_cancelled():
        logger.info("Reminder check loop was cancelled")
    else:
        logger.error("Reminder check loop stopped unexpectedly!")
        if check_reminders.failed():
            logger.error(f"Loop failed with exception: {check_reminders.get_task().exception()}")

# Add error handler for the bot
@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"Error in event {event}: {traceback.format_exc()}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"This command is on cooldown. Try again in {error.retry_after:.1f}s",
            ephemeral=True
        )
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You don't have permission to use this command!",
            ephemeral=True
        )
    else:
        logger.error(f"Command error in {interaction.command.name}: {str(error)}")
        traceback.print_exc()
        await interaction.response.send_message(
            "An error occurred while processing your command. Please try again later.",
            ephemeral=True
        )

@bot.tree.command(name="help", description="Show detailed help information")
@app_commands.describe(
    command="Get detailed help for a specific command"
)
async def help_command(
    interaction: discord.Interaction,
    command: Optional[Literal[
        'addping', 'editping', 'removeping', 'list',
        'setchannel', 'settimezone', 'savetemplate', 'usetemplate',
        'pauseping', 'pauseall', 'resumeping', 'ghostping',
        'setstatus', 'setnick', 'setavatar', 'setbio'
    ]] = None
):
    if command:
        # Detailed help for specific command
        embeds = {
            'addping': discord.Embed(
                title="📌 Add Ping Command",
                description="Create an interval-based ping that repeats at fixed intervals",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value=(
                    "`/addping targets:@users/roles interval:number time_unit:[minutes/hours/days] "
                    "message:your message`\n"
                    "Optional: `dm:True/False channel:#channel`"
                ),
                inline=False
            ).add_field(
                name="Examples",
                value=(
                    "1. `/addping targets:@user interval:30 time_unit:minutes "
                    "message:Time for a break!`\n"
                    "2. `/addping targets:@role interval:2 time_unit:hours "
                    "message:Status update? channel:#team-chat`\n"
                    "3. `/addping targets:@user interval:1 time_unit:days "
                    "dm:true message:Daily medication reminder`"
                ),
                inline=False
            ),
            
            'ghostping': discord.Embed(
                title="👻 Ghost Ping Command",
                description="Create a ping that deletes itself immediately after sending (Owner only)",
                color=discord.Color.purple()
            ).add_field(
                name="Usage",
                value=(
                    "`/ghostping targets:@users/roles interval:number time_unit:[minutes/hours/days] "
                    "message:your message`\n"
                    "Optional: `channel:#channel`"
                ),
                inline=False
            ).add_field(
                name="Notes",
                value=(
                    "- Only available to the bot owner\n"
                    "- Message is deleted immediately after sending\n"
                    "- DMs are not supported for ghost pings\n"
                    "- Can target both users and roles"
                ),
                inline=False
            ),
            
            'list': discord.Embed(
                title="📋 List Command",
                description="View all active reminders in chronological order",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value=(
                    "`/list`\n"
                    "Optional: `show_all:True` to include inactive reminders"
                ),
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Shows upcoming reminders in order\n"
                    "- Displays time until next ping\n"
                    "- Shows reminder status and type\n"
                    "- Navigate pages with buttons"
                ),
                inline=False
            ),
            # ... keep other command help entries ...
        }

        if command in embeds:
            await interaction.response.send_message(embed=embeds[command])
            return

    # General help
    embed = discord.Embed(
        title="🤖 Pingur Bot Commands",
        color=discord.Color.blue()
    )

    # Commands Overview
    embed.add_field(
        name="⚡ Available Commands",
        value=(
            "`/addping` - Create interval-based ping\n"
            "`/addreminder` - Create time-based reminder\n"
            "`/list` - View all reminders\n"
            "`/editping` - Modify reminder\n"
            "`/removeping` - Delete reminder\n"
            "`/pauseping` - Pause a reminder\n"
            "`/resumeping` - Resume a reminder\n"
            "`/pauseall` - Pause all reminders\n"
            "`/setchannel` - Set default channel\n"
            "`/settimezone` - Set server timezone\n"
            "`/savetemplate` - Save reminder template\n"
            "`/usetemplate` - Use saved template"
        ),
        inline=False
    )

    # Check if user is owner and show owner commands
    app_info = await interaction.client.application_info()
    if interaction.user.id == app_info.owner.id:
        embed.add_field(
            name="🔧 Owner Commands",
            value=(
                "`/ghostping` - Create self-deleting pings\n"
                "`/setstatus` - Set bot's activity status\n"
                "`/setnick` - Change bot's nickname\n"
                "`/setavatar` - Update bot's profile picture\n"
                "`/setbio` - Update bot's 'About Me' description"
            ),
            inline=False
        )

    # Get detailed help
    embed.add_field(
        name="📚 Detailed Help",
        value="Use `/help command:<command>` for detailed information about a specific command",
        inline=False
    )

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="settimezone", description="Set the timezone for this server")
@app_commands.describe(
    timezone="The timezone (e.g., 'US/Pacific', 'Europe/London', 'Asia/Tokyo')"
)
async def set_timezone(
    interaction: discord.Interaction,
    timezone: str
):
    try:
        # Validate timezone
        pytz.timezone(timezone)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('''
                INSERT INTO guild_settings (guild_id, timezone)
                VALUES (?, ?)
                ON CONFLICT(guild_id) 
                DO UPDATE SET timezone = excluded.timezone
            ''', (interaction.guild_id, timezone))
            await db.commit()

        embed = discord.Embed(
            title="✅ Timezone Set",
            description=f"Server timezone set to `{timezone}`",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    except pytz.exceptions.UnknownTimeZoneError:
        embed = discord.Embed(
            title="❌ Invalid Timezone",
            description="Please use a valid timezone name. Examples:\n" +
                       "• `US/Pacific`\n• `US/Eastern`\n• `Europe/London`\n" +
                       "• `Asia/Tokyo`\n• `Australia/Sydney`",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="pauseping", description="Pause a reminder temporarily")
@app_commands.describe(
    reminder_id="ID of the reminder to pause"
)
async def pause_ping(
    interaction: discord.Interaction,
    reminder_id: Optional[int] = None
):
    if reminder_id is None:
        # Show reminder selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT * FROM reminders WHERE guild_id = ? AND active = 1', 
                                (interaction.guild_id,)) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.response.send_message('❌ No active reminders found!', ephemeral=True)
            return

        view = ReminderSelectView(reminders, "pause")
        await interaction.response.send_message(
            "Select a reminder to pause:",
            view=view,
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Check if reminder exists and is active
        async with db.execute('SELECT * FROM reminders WHERE id = ? AND guild_id = ?', 
                            (reminder_id, interaction.guild_id)) as cursor:
            reminder = await cursor.fetchone()

        if not reminder:
            await interaction.response.send_message('❌ Reminder not found!', ephemeral=True)
            return

        if not reminder[12]:  # active status
            await interaction.response.send_message('❌ Reminder is already paused!', ephemeral=True)
            return

        # Pause the reminder
        await db.execute('UPDATE reminders SET active = 0 WHERE id = ?', (reminder_id,))
        await db.commit()

        embed = await create_reminder_embed(interaction, reminder)
        embed.title = "⏸️ Reminder Paused"
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="pauseall", description="Pause all reminders in this server")
async def pause_all(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        # Get count of active reminders
        async with db.execute('SELECT COUNT(*) FROM reminders WHERE guild_id = ? AND active = 1', 
                            (interaction.guild_id,)) as cursor:
            count = (await cursor.fetchone())[0]

        if count == 0:
            await interaction.response.send_message('❌ No active reminders found!', ephemeral=True)
            return

        # Create confirmation view
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)

            @discord.ui.button(label=f"Pause {count} Reminders", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute('UPDATE reminders SET active = 0 WHERE guild_id = ? AND active = 1',
                                   (interaction.guild_id,))
                    await db.commit()

                embed = discord.Embed(
                    title="⏸️ All Reminders Paused",
                    description=f"Paused {count} reminders",
                    color=discord.Color.orange()
                )
                await interaction.response.edit_message(embed=embed, view=None)

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                embed = discord.Embed(
                    title="❌ Operation Cancelled",
                    description="No reminders were paused",
                    color=discord.Color.red()
                )
                await interaction.response.edit_message(embed=embed, view=None)

        embed = discord.Embed(
            title="⚠️ Confirm Action",
            description=f"Are you sure you want to pause all {count} active reminders?",
            color=discord.Color.yellow()
        )
        await interaction.response.send_message(embed=embed, view=ConfirmView())

@bot.tree.command(name="resumeping", description="Resume a paused reminder")
@app_commands.describe(
    reminder_id="ID of the reminder to resume"
)
async def resume_ping(
    interaction: discord.Interaction,
    reminder_id: Optional[int] = None
):
    if reminder_id is None:
        # Show reminder selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT * FROM reminders WHERE guild_id = ? AND active = 0', 
                                (interaction.guild_id,)) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.response.send_message('❌ No paused reminders found!', ephemeral=True)
            return

        view = ReminderSelectView(reminders, "resume")
        await interaction.response.send_message(
            "Select a reminder to resume:",
            view=view,
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Check if reminder exists and is paused
        async with db.execute('SELECT * FROM reminders WHERE id = ? AND guild_id = ?', 
                            (reminder_id, interaction.guild_id)) as cursor:
            reminder = await cursor.fetchone()

        if not reminder:
            await interaction.response.send_message('❌ Reminder not found!', ephemeral=True)
            return

        if reminder[12]:  # active status
            await interaction.response.send_message('❌ Reminder is already active!', ephemeral=True)
            return

        # Calculate next ping time
        now = datetime.now()
        interval_minutes = reminder[7] * TIME_UNITS[reminder[8]]  # interval * unit multiplier
        next_ping = now + timedelta(minutes=interval_minutes)

        # Resume the reminder
        await db.execute('''
            UPDATE reminders 
            SET active = 1, next_ping = ? 
            WHERE id = ?
        ''', (next_ping.isoformat(), reminder_id))
        await db.commit()

        embed = await create_reminder_embed(interaction, reminder)
        embed.title = "▶️ Reminder Resumed"
        await interaction.response.send_message(embed=embed)

class ReminderSelectView(discord.ui.View):
    def __init__(self, reminders, action):
        super().__init__(timeout=60)
        self.reminders = reminders
        self.action = action
        
        # Create select menu with reminders
        select = discord.ui.Select(
            placeholder=f"Choose a reminder to {action}",
            options=[
                discord.SelectOption(
                    label=f"Reminder #{r[0]}",
                    description=f"{r[6][:50]}...",  # First 50 chars of message
                    value=str(r[0])
                ) for r in reminders[:25]  # Discord limit of 25 options
            ]
        )
        
        async def select_callback(interaction: discord.Interaction):
            reminder_id = int(select.values[0])
            if self.action == "pause":
                await pause_ping(interaction, reminder_id)
            else:  # resume
                await resume_ping(interaction, reminder_id)
        
        select.callback = select_callback
        self.add_item(select)

@bot.tree.command(name="removereminder", description="Delete a one-time reminder")
async def remove_reminder(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        # Show reminder selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                'SELECT * FROM reminders WHERE guild_id = ? AND recurring = 0', 
                (interaction.guild_id,)
            ) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.followup.send('❌ No reminders found!', ephemeral=True)
            return

        # Create select menu with reminders
        select = discord.ui.Select(
            placeholder="Choose a reminder to delete",
            options=[
                discord.SelectOption(
                    label=f"Reminder #{r[0]}",
                    description=f"{r[6][:50]}...",  # First 50 chars of message
                    value=str(r[0])
                ) for r in reminders[:25]  # Discord limit of 25 options
            ]
        )
        
        class DeleteView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.add_item(select)
            
            async def handle_delete(self, interaction: discord.Interaction, rid: int):
                async with aiosqlite.connect(DB_PATH) as db:
                    # Get reminder details first
                    async with db.execute('SELECT * FROM reminders WHERE id = ? AND guild_id = ?', 
                                        (rid, interaction.guild_id)) as cursor:
                        reminder = await cursor.fetchone()
                        
                    if not reminder:
                        await interaction.response.send_message('❌ Reminder not found!', ephemeral=True)
                        return
                    
                    # Delete the reminder
                    await db.execute('DELETE FROM reminders WHERE id = ?', (rid,))
                    await db.commit()
                    
                    embed = discord.Embed(
                        title="✅ Reminder Deleted",
                        description=f"Reminder #{rid} has been deleted",
                        color=discord.Color.red()
                    )
                    await interaction.response.edit_message(embed=embed, view=None)

            @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                rid = int(select.values[0])
                await self.handle_delete(interaction, rid)
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                embed = discord.Embed(
                    title="❌ Operation Cancelled",
                    description="No reminders were deleted",
                    color=discord.Color.green()
                )
                await interaction.response.edit_message(embed=embed, view=None)

        view = DeleteView()
        await interaction.followup.send(
            "Select a reminder to delete:",
            view=view,
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Error in remove_reminder: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred. Please try again later.",
            ephemeral=True
        )

@bot.tree.command(name="removeping", description="Delete an interval-based ping")
async def remove_ping(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        
        # Show ping selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                'SELECT * FROM reminders WHERE guild_id = ? AND recurring = 1', 
                (interaction.guild_id,)
            ) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.followup.send('❌ No pings found!', ephemeral=True)
            return

        # Create select menu with pings
        select = discord.ui.Select(
            placeholder="Choose a ping to delete",
            options=[
                discord.SelectOption(
                    label=f"Ping #{r[0]}",
                    description=f"{r[6][:50]}...",  # First 50 chars of message
                    value=str(r[0])
                ) for r in reminders[:25]  # Discord limit of 25 options
            ]
        )
        
        class DeleteView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.add_item(select)
            
            async def handle_delete(self, interaction: discord.Interaction, rid: int):
                async with aiosqlite.connect(DB_PATH) as db:
                    # Get ping details first
                    async with db.execute('SELECT * FROM reminders WHERE id = ? AND guild_id = ?', 
                                        (rid, interaction.guild_id)) as cursor:
                        reminder = await cursor.fetchone()
                        
                    if not reminder:
                        await interaction.followup.send('❌ Ping not found!', ephemeral=True)
                        return
                    
                    # Delete the ping
                    await db.execute('DELETE FROM reminders WHERE id = ?', (rid,))
                    await db.commit()
                    
                    embed = discord.Embed(
                        title="✅ Ping Deleted",
                        description=f"Ping #{rid} has been deleted",
                        color=discord.Color.red()
                    )
                    await interaction.response.edit_message(embed=embed, view=None)

            @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                rid = int(select.values[0])
                await self.handle_delete(interaction, rid)
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                embed = discord.Embed(
                    title="❌ Operation Cancelled",
                    description="No pings were deleted",
                    color=discord.Color.green()
                )
                await interaction.response.edit_message(embed=embed, view=None)

        view = DeleteView()
        await interaction.followup.send(
            "Select a ping to delete:",
            view=view,
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Error in remove_ping: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred. Please try again later.",
            ephemeral=True
        )

def is_bot_owner():
    async def predicate(interaction: discord.Interaction):
        app_info = await interaction.client.application_info()
        if interaction.user.id != app_info.owner.id:
            raise app_commands.CheckFailure("This command is only available to the bot owner.")
        return True
    return app_commands.check(predicate)

@bot.tree.command(name="setstatus", description="Set the bot's status (Owner only)")
@app_commands.describe(
    status_type="The type of status to set",
    activity="What the bot is doing",
    url="URL for streaming status (optional)"
)
@is_bot_owner()
async def set_status(
    interaction: discord.Interaction,
    status_type: Literal['playing', 'watching', 'listening', 'streaming'],
    activity: str,
    url: Optional[str] = None
):
    try:
        if status_type == 'playing':
            activity_type = discord.ActivityType.playing
        elif status_type == 'watching':
            activity_type = discord.ActivityType.watching
        elif status_type == 'listening':
            activity_type = discord.ActivityType.listening
        else:  # streaming
            activity_type = discord.ActivityType.streaming

        if status_type == 'streaming' and not url:
            await interaction.response.send_message(
                "❌ URL is required for streaming status!",
                ephemeral=True
            )
            return

        game = discord.Activity(
            type=activity_type,
            name=activity,
            url=url if status_type == 'streaming' else None
        )
        await bot.change_presence(activity=game)

        embed = discord.Embed(
            title="✅ Status Updated",
            description=f"Status set to: {status_type.title()} {activity}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error in set_status: {str(e)}")
        await interaction.response.send_message(
            "❌ Failed to update status!",
            ephemeral=True
        )

@bot.tree.command(name="setnick", description="Set the bot's nickname in the current server (Owner only)")
@app_commands.describe(
    nickname="New nickname for the bot (leave empty to reset)"
)
@is_bot_owner()
async def set_nickname(
    interaction: discord.Interaction,
    nickname: Optional[str] = None
):
    try:
        await interaction.guild.me.edit(nick=nickname)
        embed = discord.Embed(
            title="✅ Nickname Updated",
            description=f"Nickname {'reset' if nickname is None else f'set to: {nickname}'}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to change my nickname!",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Error in set_nickname: {str(e)}")
        await interaction.response.send_message(
            "❌ Failed to update nickname!",
            ephemeral=True
        )

@bot.tree.command(name="setavatar", description="Set the bot's avatar (Owner only)")
@app_commands.describe(
    url="URL of the new avatar image"
)
@is_bot_owner()
async def set_avatar(
    interaction: discord.Interaction,
    url: str
):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await interaction.response.send_message(
                        "❌ Failed to download image!",
                        ephemeral=True
                    )
                    return
                
                avatar_bytes = await response.read()
                
        await bot.user.edit(avatar=avatar_bytes)
        embed = discord.Embed(
            title="✅ Avatar Updated",
            description="Bot's avatar has been updated!",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=url)
        await interaction.response.send_message(embed=embed)
    except discord.HTTPException as e:
        error_msg = "❌ Failed to update avatar! "
        if e.code == 50035:
            error_msg += "Invalid image format (must be PNG, JPG, or GIF)"
        elif e.code == 50138:
            error_msg += "Image file is too large (max 8MB)"
        else:
            error_msg += str(e)
        await interaction.response.send_message(error_msg, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in set_avatar: {str(e)}")
        await interaction.response.send_message(
            "❌ Failed to update avatar!",
            ephemeral=True
        )

@bot.tree.command(name="setbio", description="Set the bot's 'About Me' description (Owner only)")
@app_commands.describe(
    bio="New 'About Me' text for the bot"
)
@is_bot_owner()
async def set_bio(
    interaction: discord.Interaction,
    bio: str
):
    try:
        await bot.user.edit(bio=bio)
        embed = discord.Embed(
            title="✅ Bio Updated",
            description=f"Bot's bio has been updated to:\n\n{bio}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    except discord.HTTPException as e:
        error_msg = "❌ Failed to update bio! "
        if e.code == 50035:
            error_msg += "Bio must be 190 characters or less"
        else:
            error_msg += str(e)
        await interaction.response.send_message(error_msg, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in set_bio: {str(e)}")
        await interaction.response.send_message(
            "❌ Failed to update bio!",
            ephemeral=True
        )

@bot.tree.command(name="ghostping", description="Create a ghost ping that deletes itself right after pinging (Owner only)")
@app_commands.describe(
    targets="Users/Roles to remind (mention them, use IDs, or separate multiple with spaces)",
    time_unit="Time unit (minutes/hours/days)",
    interval="Number of time units between pings",
    message="Message to send with the ping",
    channel="Channel to send ping (optional, uses current channel if not specified)"
)
async def ghost_ping(
    interaction: discord.Interaction,
    targets: str,
    time_unit: Literal['minutes', 'hours', 'days'],
    interval: int,
    message: str,
    channel: Optional[discord.TextChannel] = None
):
    try:
        # Check if user is the bot owner
        app_info = await bot.application_info()
        if interaction.user.id != app_info.owner.id:
            await interaction.response.send_message("❌ This command is only available to the bot owner!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        
        # Get server timezone
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT timezone FROM guild_settings WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                result = await cursor.fetchone()
                timezone = result[0] if result else 'UTC'

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Parse targets (users and roles)
        target_ids = []
        target_type = None
        
        for word in targets.split():
            target_id = None
            is_role = False

            if word.startswith('<@&'):  # Role mention
                try:
                    target_id = int(word[3:-1])
                    is_role = True
                except ValueError as e:
                    logger.error(f"Failed to parse role ID from {word}: {str(e)}")
                    continue
            elif word.startswith('<@'):  # User mention
                try:
                    target_id = int(word[2:-1].replace('!', ''))
                except ValueError as e:
                    logger.error(f"Failed to parse user ID from {word}: {str(e)}")
                    continue
            else:  # Raw ID
                try:
                    target_id = int(word)
                    # Check if it's a role ID
                    role = interaction.guild.get_role(target_id)
                    if role:
                        is_role = True
                    else:
                        # Check if it's a valid user ID
                        member = interaction.guild.get_member(target_id)
                        if not member:
                            logger.error(f"Could not find member or role with ID {target_id}")
                            continue
                except ValueError as e:
                    logger.error(f"Failed to parse ID from {word}: {str(e)}")
                    continue

            if target_id:
                if not target_type:
                    target_type = 'role' if is_role else 'user'
                elif (target_type == 'role') != is_role:
                    await interaction.followup.send(
                        "Cannot mix users and roles in the same ping!",
                        ephemeral=True
                    )
                    return
                target_ids.append(target_id)

        if not target_ids:
            await interaction.followup.send(
                "No valid targets found! Please mention users/roles or use their IDs.",
                ephemeral=True
            )
            return

        # Verify all targets exist
        invalid_ids = []
        for tid in target_ids:
            if target_type == 'user':
                if not interaction.guild.get_member(tid):
                    invalid_ids.append(str(tid))
            else:
                if not interaction.guild.get_role(tid):
                    invalid_ids.append(str(tid))
        
        if invalid_ids:
            await interaction.followup.send(
                f"Some {target_type}s were not found: {', '.join(invalid_ids)}",
                ephemeral=True
            )
            return

        # Get channel ID
        if channel:
            channel_id = channel.id
        else:
            # First try to use the current channel
            channel_id = interaction.channel_id
            if not channel_id:
                # If not in a channel, try to use the default channel
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute('SELECT default_channel_id FROM guild_settings WHERE guild_id = ?', 
                                        (interaction.guild_id,)) as cursor:
                        result = await cursor.fetchone()
                        channel_id = result[0] if result else None

                if not channel_id:
                    await interaction.followup.send(
                        "No channel specified and no default channel set! Please specify a channel or use /setchannel to set a default.",
                        ephemeral=True
                    )
                    return

        # Calculate next ping time
        interval_minutes = interval * TIME_UNITS[time_unit]
        next_ping = now + timedelta(minutes=interval_minutes)
        
        # Ensure timezone-aware datetime storage
        if next_ping.tzinfo is None:
            next_ping = tz.localize(next_ping)
        next_ping = next_ping.astimezone(pytz.utc)

        # Insert the reminder with ghost flag
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('''
                INSERT INTO reminders (
                    guild_id, channel_id, user_id, target_ids, target_type,
                    message, interval, time_unit, last_ping, next_ping,
                    dm, recurring, active, ghost_ping
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                interaction.guild_id, 
                channel_id,
                interaction.user.id,
                ','.join(map(str, target_ids)),
                target_type,
                message,
                interval,
                time_unit,
                now.astimezone(pytz.utc).isoformat(),  # Store in UTC
                next_ping.isoformat(),
                False,  # DM not allowed for ghost pings
                True,  # Always recurring for interval-based pings
                True,
                True  # This is a ghost ping
            ))
            await db.commit()
            
            # Get the ID of the inserted reminder
            cursor = await db.execute('SELECT last_insert_rowid()')
            row = await cursor.fetchone()
            reminder_id = row[0]

        # Get targets for display
        targets_display = []
        for tid in target_ids:
            if target_type == 'user':
                target = interaction.guild.get_member(int(tid))
            else:
                target = interaction.guild.get_role(int(tid))
            if target:
                targets_display.append(target.mention)

        # Get channel for display
        channel_display = interaction.guild.get_channel(channel_id)

        embed = discord.Embed(
            title="👻 New Ghost Ping Created",
            description=f"**ID:** #{reminder_id}\n" +
                       f"**Interval:** Every {interval} {time_unit}\n" +
                       f"**To:** {', '.join(targets_display) or 'No targets'}\n" +
                       f"**Where:** 📢 {channel_display.mention if channel_display else 'Unknown'}\n" +
                       f"**Message:** {message}",
            color=discord.Color.purple()
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in ghost_ping command: {str(e)}")
        traceback.print_exc()
        await interaction.followup.send(
            "An error occurred while creating the ghost ping. Please try again later.",
            ephemeral=True
        )

# Move the bot run to a main function with proper error handling
def main():
    try:
        logger.info("Starting Pingur bot...")
        bot.run(TOKEN, log_handler=None)  # Disable default discord.py logging
    except discord.LoginFailure:
        logger.error("Failed to login! Check your Discord token.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main() 