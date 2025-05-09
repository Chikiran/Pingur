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
    targets="Users/Roles to remind (mention them or use IDs)",
    interval="Number of time units between pings",
    time_unit="Time unit (minutes/hours/days)",
    message="Message to send with the ping",
    dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send ping (optional)"
)
async def add_ping(
    interaction: discord.Interaction,
    targets: str,
    interval: int,
    time_unit: Literal['minutes', 'hours', 'days'],
    message: str,
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None
):
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
        if word.startswith('<@&'):  # Role mention
            try:
                role_id = int(word[3:-1])
                if not target_type:
                    target_type = 'role'
                elif target_type != 'role':
                    await interaction.response.send_message(
                        "Cannot mix users and roles in the same ping!",
                        ephemeral=True
                    )
                    return
                target_ids.append(role_id)
            except ValueError:
                continue
        elif word.startswith('<@'):  # User mention
            try:
                user_id = int(word[2:-1].replace('!', ''))
                if not target_type:
                    target_type = 'user'
                elif target_type != 'user':
                    await interaction.response.send_message(
                        "Cannot mix users and roles in the same ping!",
                        ephemeral=True
                    )
                    return
                target_ids.append(user_id)
            except ValueError:
                continue

    if not target_ids:
        await interaction.response.send_message(
            "No valid targets found! Please mention users or roles.",
            ephemeral=True
        )
        return

    if target_type == 'role' and dm:
        await interaction.response.send_message(
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
                await interaction.response.send_message(
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
    next_ping = next_ping.astimezone(pytz.utc)  # Store in UTC

    # Insert the reminder
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
        now.isoformat(),
        next_ping.isoformat(),
        dm,
        True,  # Always recurring for interval-based pings
        True
    ))
    await db.commit()
    
    # Get the ID of the inserted reminder
    cursor = await db.execute('SELECT last_insert_rowid()')
    reminder_id = (await cursor.fetchone())[0]
    
    # Get the full reminder data for the response
    cursor = await db.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,))
    reminder = await cursor.fetchone()

    embed = await create_reminder_embed(interaction, reminder)
    embed.title = "✅ New Ping Created"
    embed.add_field(
        name="⏰ Interval",
        value=f"Every {interval} {time_unit}",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addreminder", description="Add a time-based reminder (e.g., daily at 3pm)")
@app_commands.describe(
    targets="Users/Roles to remind (mention them or use IDs)",
    time="When to send the reminder (e.g., '3pm', '15:00')",
    repeat="How often to repeat the reminder",
    message="Message to send with the reminder",
    dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send reminder (optional)"
)
async def add_reminder(
    interaction: discord.Interaction,
    targets: str,
    time: str,
    repeat: Literal['never', 'daily', 'weekly'],
    message: str,
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None
):
    # Get server timezone
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT timezone FROM guild_settings WHERE guild_id = ?', 
                            (interaction.guild_id,)) as cursor:
            result = await cursor.fetchone()
            timezone = result[0] if result else 'UTC'

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)

    try:
        # Parse the time string
        if 'm' in time.lower():
            parsed_time = datetime.strptime(time, '%I%p').time()
        else:
            parsed_time = datetime.strptime(time, '%H:%M').time()
        
        # Create timezone-aware target time
        target_time = now.replace(hour=parsed_time.hour, minute=parsed_time.minute)
        if target_time < now:
            target_time += timedelta(days=1)
            
        # Ensure timezone-aware datetime
        if target_time.tzinfo is None:
            target_time = tz.localize(target_time)
        target_time = target_time.astimezone(pytz.utc)  # Store in UTC
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid time format! Examples:\n" +
            "• `3pm` - At 3 PM\n" +
            "• `15:00` - At 3 PM (24h format)",
            ephemeral=True
        )
        return

    # Parse targets (users and roles)
    target_ids = []
    target_type = None
    
    for word in targets.split():
        if word.startswith('<@&'):  # Role mention
            try:
                role_id = int(word[3:-1])
                if not target_type:
                    target_type = 'role'
                elif target_type != 'role':
                    await interaction.response.send_message(
                        "Cannot mix users and roles in the same reminder!",
                        ephemeral=True
                    )
                    return
                target_ids.append(role_id)
            except ValueError:
                continue
        elif word.startswith('<@'):  # User mention
            try:
                user_id = int(word[2:-1].replace('!', ''))
                if not target_type:
                    target_type = 'user'
                elif target_type != 'user':
                    await interaction.response.send_message(
                        "Cannot mix users and roles in the same reminder!",
                        ephemeral=True
                    )
                    return
                target_ids.append(user_id)
            except ValueError:
                continue

    if not target_ids:
        await interaction.response.send_message(
            "No valid targets found! Please mention users or roles.",
            ephemeral=True
        )
        return

    if target_type == 'role' and dm:
        await interaction.response.send_message(
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
                await interaction.response.send_message(
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

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
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
    
    await interaction.response.send_message(embed=embed)

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

@bot.tree.command(name="list", description="View upcoming reminders in chronological order")
@app_commands.describe(
    show_all="Show all reminders including inactive ones"
)
async def list_reminders(
    interaction: discord.Interaction,
    show_all: bool = False
):
    try:
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
                WHERE guild_id = ?
            '''
            if not show_all:
                query += ' AND active = 1'
            query += ' ORDER BY next_ping ASC'
            
            async with db.execute(query, (interaction.guild_id,)) as cursor:
                reminders = await cursor.fetchall()

        if not reminders:
            await interaction.response.send_message(
                "❌ No upcoming reminders found!",
                ephemeral=True
            )
            return

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Create paginated view
        class ListView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.page = 0
                self.max_pages = math.ceil(len(reminders) / ITEMS_PER_PAGE)

            @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray, emoji="◀️", disabled=True)
            async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = max(0, self.page - 1)
                await self.update_view(interaction)

            @discord.ui.button(label="Next", style=discord.ButtonStyle.gray, emoji="▶️")
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = min(self.max_pages - 1, self.page + 1)
                await self.update_view(interaction)

            async def update_view(self, interaction: discord.Interaction):
                try:
                    # Update button states
                    self.prev_button.disabled = self.page == 0
                    self.next_button.disabled = self.page >= self.max_pages - 1

                    # Create embed for current page
                    start_idx = self.page * ITEMS_PER_PAGE
                    page_reminders = reminders[start_idx:start_idx + ITEMS_PER_PAGE]

                    embed = discord.Embed(
                        title="📅 Active Reminders",
                        description=f"Page {self.page + 1} of {self.max_pages}\nServer timezone: {timezone}",
                        color=discord.Color.blue()
                    )

                    for reminder in page_reminders:
                        rid, _, channel_id, _, target_ids, target_type, msg, interval, time_unit, _, next_ping, dm, active, recurring, _ = reminder
                        
                        # Get targets
                        targets = []
                        for tid in target_ids.split(','):
                            if target_type == 'user':
                                target = interaction.guild.get_member(int(tid))
                            else:
                                target = interaction.guild.get_role(int(tid))
                            if target:
                                targets.append(target.mention)

                        # Get channel
                        channel = interaction.guild.get_channel(channel_id)
                        
                        # Format next ping time - Fixed timezone handling
                        try:
                            # Parse the ISO format string
                            next_time = datetime.fromisoformat(next_ping)
                            
                            # If the datetime is naive (no timezone), assume it's in UTC
                            if next_time.tzinfo is None:
                                next_time = pytz.utc.localize(next_time)
                            # If it already has timezone info, just use it as is
                            
                            # Convert to server timezone
                            next_time = next_time.astimezone(tz)
                        except ValueError as e:
                            logger.error(f"Error parsing datetime: {e}")
                            next_time = now  # Fallback to current time
                        
                        # Calculate time until next ping
                        time_until = next_time - now
                        if time_until.total_seconds() < 0:
                            time_str = "Overdue!"
                        else:
                            days = time_until.days
                            hours = time_until.seconds // 3600
                            minutes = (time_until.seconds % 3600) // 60
                            time_parts = []
                            if days > 0:
                                time_parts.append(f"{days}d")
                            if hours > 0:
                                time_parts.append(f"{hours}h")
                            if minutes > 0:
                                time_parts.append(f"{minutes}m")
                            time_str = f"in {' '.join(time_parts)}" if time_parts else "now"

                        status = "🟢" if active else "🔴"
                        repeat = "🔁" if recurring else "1️⃣"
                        location = "📱 DM" if dm else f"📢 {channel.mention if channel else 'Unknown channel'}"

                        embed.add_field(
                            name=f"{status} Reminder #{rid} {repeat}",
                            value=(
                                f"**Next:** {next_time.strftime('%I:%M %p')} ({time_str})\n"
                                f"**To:** {', '.join(targets) or 'No valid targets'}\n"
                                f"**Where:** {location}\n"
                                f"**Message:** {msg}"
                            ),
                            inline=False
                        )

                    await interaction.response.edit_message(embed=embed, view=self)
                except Exception as e:
                    logger.error(f"Error in list view update: {str(e)}")
                    traceback.print_exc()
                    await interaction.response.send_message(
                        "An error occurred while updating the reminder list. Please try again.",
                        ephemeral=True
                    )

        view = ListView()
        await view.update_view(interaction)
    except Exception as e:
        logger.error(f"Error in list command: {str(e)}")
        traceback.print_exc()
        await interaction.response.send_message(
            "An error occurred while fetching reminders. Please try again later.",
            ephemeral=True
        )

@tasks.loop(seconds=30)  # Check more frequently for accuracy
async def check_reminders():
    """Check and send reminders"""
    try:
        now = datetime.now(pytz.utc)  # Get current time in UTC
        logger.info("Starting reminder check...")
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Get all active reminders that need to be triggered
            async with db.execute('''
                SELECT r.*, g.timezone 
                FROM reminders r
                LEFT JOIN guild_settings g ON r.guild_id = g.guild_id
                WHERE r.active = 1 
                AND r.next_ping <= ?
            ''', (now.isoformat(),)) as cursor:
                reminders = await cursor.fetchall()
                
            logger.info(f"Found {len(reminders)} reminders to process")
            
            for reminder in reminders:
                try:
                    id, guild_id, channel_id, user_id, target_ids, target_type, message, interval, time_unit, last_ping, next_ping, is_dm, active, is_recurring, created_at, timezone = reminder
                    
                    guild = bot.get_guild(guild_id)
                    if not guild:
                        logger.warning(f"Guild {guild_id} not found for reminder {id}")
                        continue

                    # Get targets (users or roles)
                    targets = []
                    for target_id in target_ids.split(','):
                        if target_type == 'user':
                            target = guild.get_member(int(target_id))
                            if target:
                                targets.append(target)
                            else:
                                logger.warning(f"User {target_id} not found in guild {guild.name}")
                        else:
                            role = guild.get_role(int(target_id))
                            if role:
                                targets.append(role)
                            else:
                                logger.warning(f"Role {target_id} not found in guild {guild.name}")

                    if not targets:
                        logger.warning(f"No valid targets found for reminder {id}")
                        continue

                    try:
                        if is_dm and target_type == 'user':
                            for target in targets:
                                await target.send(f'{message}')
                                logger.info(f"Sent DM to {target.name} for reminder {id}")
                        else:
                            channel = guild.get_channel(channel_id)
                            if channel:
                                mentions = ' '.join(target.mention for target in targets)
                                await channel.send(f'{mentions} {message}')
                                logger.info(f"Sent message in {channel.name} for reminder {id}")
                            else:
                                logger.warning(f"Channel {channel_id} not found in guild {guild.name}")

                        # Update last ping and next ping times
                        if is_recurring:
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
                            logger.info(f"Updated recurring reminder {id}, next ping at {next_ping_time}")
                        else:
                            # For one-time reminders, deactivate after sending
                            await db.execute('''
                                UPDATE reminders 
                                SET active = 0, last_ping = ?
                                WHERE id = ?
                            ''', (now.isoformat(), id))
                            logger.info(f"Deactivated one-time reminder {id}")
                        
                        await db.commit()
                    except discord.Forbidden as e:
                        logger.error(f"Permission error for reminder {id} in guild {guild.name}: {str(e)}")
                    except Exception as e:
                        logger.error(f"Error processing reminder {id}: {str(e)}")
                        traceback.print_exc()
                except Exception as e:
                    logger.error(f"Error processing reminder data: {str(e)}")
                    traceback.print_exc()
                    continue
    except Exception as e:
        logger.error(f"Error in check_reminders task: {str(e)}")
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