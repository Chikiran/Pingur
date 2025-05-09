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

@bot.tree.command(name="addping", description="Add a new ping reminder")
@app_commands.describe(
    targets="Users/Roles to remind (mention them or use IDs)",
    time="When to send the reminder (e.g., '3pm', '15:00', 'tomorrow 2pm')",
    message="Message to send with the reminder",
    dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send reminder (optional)",
    recurring="Whether this reminder should repeat daily"
)
async def add_ping(
    interaction: discord.Interaction,
    targets: str,
    time: str,
    message: str,
    dm: bool = False,
    channel: Optional[discord.TextChannel] = None,
    recurring: bool = False
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
        if time.lower() == 'tomorrow':
            target_time = now.replace(hour=9, minute=0) + timedelta(days=1)
        elif 'tomorrow' in time.lower():
            time_part = time.lower().replace('tomorrow', '').strip()
            parsed_time = datetime.strptime(time_part, '%I%p').time() if 'm' in time_part.lower() else datetime.strptime(time_part, '%H:%M').time()
            target_time = now.replace(hour=parsed_time.hour, minute=parsed_time.minute) + timedelta(days=1)
        else:
            parsed_time = datetime.strptime(time, '%I%p').time() if 'm' in time.lower() else datetime.strptime(time, '%H:%M').time()
            target_time = now.replace(hour=parsed_time.hour, minute=parsed_time.minute)
            if target_time < now:
                target_time += timedelta(days=1)
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid time format! Examples:\n" +
            "• `3pm` - Today at 3 PM\n" +
            "• `15:00` - Today at 3 PM (24h format)\n" +
            "• `tomorrow 2pm` - Tomorrow at 2 PM",
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

    # Calculate interval if recurring
    if recurring:
        interval = 1440  # 24 hours in minutes
        time_unit = 'minutes'
    else:
        # Calculate minutes until target time
        delta = target_time - now
        interval = int(delta.total_seconds() / 60)
        time_unit = 'minutes'

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
            time_unit,
            now.isoformat(),
            target_time.isoformat(),
            dm,
            recurring,
            True
        ))
        reminder_id = (await db.execute('SELECT last_insert_rowid()')).fetchone()[0]
        await db.commit()

        # Fetch the newly created reminder
        async with db.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,)) as cursor:
            reminder = await cursor.fetchone()

    embed = await create_reminder_embed(interaction, reminder)
    embed.title = "✅ New Reminder Created"
    embed.add_field(
        name="🕒 Timezone Info",
        value=f"Server timezone: {timezone}\nNext reminder: {target_time.strftime('%I:%M %p %Z')}",
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
        await add_ping(
            interaction=interaction,
            targets=final_targets,
            time=final_time,
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

@tasks.loop(seconds=30)  # Check more frequently for accuracy
async def check_reminders():
    """Check and send reminders"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Get all active reminders that need to be triggered
        async with db.execute('''
            SELECT r.*, g.timezone 
            FROM reminders r
            LEFT JOIN guild_settings g ON r.guild_id = g.guild_id
            WHERE r.active = 1 
            AND r.next_ping <= datetime('now')
        ''') as cursor:
            reminders = await cursor.fetchall()
            
        for reminder in reminders:
            id, guild_id, channel_id, user_id, target_ids, target_type, message, interval, time_unit, last_ping, next_ping, is_dm, active, is_recurring, created_at, timezone = reminder
            
            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            # Get targets (users or roles)
            targets = []
            for target_id in target_ids.split(','):
                if target_type == 'user':
                    target = guild.get_member(int(target_id))
                    if target:
                        targets.append(target)
                else:
                    role = guild.get_role(int(target_id))
                    if role:
                        targets.append(role)

            if not targets:
                continue

            try:
                if is_dm and target_type == 'user':
                    for target in targets:
                        await target.send(f'{message}')
                else:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        mentions = ' '.join(target.mention for target in targets)
                        await channel.send(f'{mentions} {message}')

                # Update last ping and next ping times
                now = datetime.now()
                if is_recurring:
                    interval_minutes = interval * TIME_UNITS[time_unit]
                    next_ping = now + timedelta(minutes=interval_minutes)
                    await db.execute('''
                        UPDATE reminders 
                        SET last_ping = ?, next_ping = ?
                        WHERE id = ?
                    ''', (now.isoformat(), next_ping.isoformat(), id))
                else:
                    # For one-time reminders, deactivate after sending
                    await db.execute('''
                        UPDATE reminders 
                        SET active = 0, last_ping = ?
                        WHERE id = ?
                    ''', (now.isoformat(), id))
                
                await db.commit()
            except discord.Forbidden:
                print(f'Failed to send message to targets in guild {guild.name}')

@bot.tree.command(name="help", description="Show detailed help information")
@app_commands.describe(
    command="Get detailed help for a specific command"
)
async def help_command(
    interaction: discord.Interaction,
    command: Optional[Literal[
        'addping', 'editping', 'removeping', 'listpings',
        'setchannel', 'settimezone', 'savetemplate', 'usetemplate',
        'pauseping', 'pauseall', 'resumeping', 'schedule'
    ]] = None
):
    if command:
        # Detailed help for specific command
        embeds = {
            'addping': discord.Embed(
                title="📌 Add Ping Command",
                description="Create a new reminder to ping users or roles",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value=(
                    "`/addping targets:@users/roles time:3pm "
                    "message:your message`\n"
                    "Optional: `dm:True/False channel:#channel "
                    "recurring:True/False`"
                ),
                inline=False
            ).add_field(
                name="Examples",
                value=(
                    "1. `/addping targets:@user time:3pm "
                    "message:Time for a break!`\n"
                    "2. `/addping targets:@role time:9am "
                    "message:Daily reminder channel:#announcements recurring:true`\n"
                    "3. `/addping targets:@user time:2pm "
                    "dm:true message:Take your medicine`"
                ),
                inline=False
            ),
            
            'editping': discord.Embed(
                title="✏️ Edit Ping Command",
                description="Modify an existing reminder",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value=(
                    "`/editping reminder_id:<number>`\n"
                    "Optional parameters to change:\n"
                    "- `time`\n"
                    "- `message`\n"
                    "- `channel`\n"
                    "- `dm`\n"
                    "- `recurring`"
                ),
                inline=False
            ).add_field(
                name="Examples",
                value=(
                    "1. `/editping reminder_id:1 time:3:45pm`\n"
                    "2. `/editping reminder_id:2 recurring:false`\n"
                    "3. `/editping reminder_id:3 message:New reminder message`"
                ),
                inline=False
            ),

            'listpings': discord.Embed(
                title="📋 List Pings Command",
                description="View all reminders with optional filters",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value=(
                    "`/listpings`\n"
                    "Optional filters:\n"
                    "- `show_inactive:True/False`\n"
                    "- `target:@user/@role`"
                ),
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Page through reminders with buttons\n"
                    "- Filter by target\n"
                    "- View active/inactive reminders\n"
                    "- Detailed view of each reminder"
                ),
                inline=False
            ),

            'removeping': discord.Embed(
                title="🗑️ Remove Ping Command",
                description="Delete a reminder with confirmation",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value="`/removeping reminder_id:<number>`",
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Shows reminder details before deletion\n"
                    "- Confirmation button to prevent accidents\n"
                    "- Option to cancel deletion"
                ),
                inline=False
            ),

            'pauseping': discord.Embed(
                title="⏸️ Pause Ping Command",
                description="Temporarily pause a reminder",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value="`/pauseping [reminder_id:<number>]`",
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Can select reminder from a list if ID not provided\n"
                    "- Keeps the reminder but stops it from triggering\n"
                    "- Can be resumed later with /resumeping"
                ),
                inline=False
            ),

            'resumeping': discord.Embed(
                title="▶️ Resume Ping Command",
                description="Resume a paused reminder",
                color=discord.Color.blue()
            ).add_field(
                name="Usage",
                value="`/resumeping [reminder_id:<number>]`",
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Can select reminder from a list if ID not provided\n"
                    "- Recalculates next ping time based on interval\n"
                    "- Restores normal reminder operation"
                ),
                inline=False
            )
        }

        if command in embeds:
            await interaction.response.send_message(embed=embeds[command])
            return

    # General help
    embed = discord.Embed(
        title="🤖 Pingur Bot Help",
        color=discord.Color.blue()
    )

    # Core Features
    embed.add_field(
        name="📌 Core Features",
        value=(
            "• Ping users or roles at specified times\n"
            "• One-time or recurring reminders\n"
            "• DM or channel messages\n"
            "• Flexible time scheduling\n"
            "• Template system"
        ),
        inline=False
    )

    # Commands Overview
    embed.add_field(
        name="⚡ Quick Command Guide",
        value=(
            "`/addping` - Create new reminder\n"
            "`/listpings` - View all reminders\n"
            "`/editping` - Modify reminder\n"
            "`/removeping` - Delete reminder\n"
            "`/pauseping` - Pause a reminder\n"
            "`/resumeping` - Resume a reminder\n"
            "`/pauseall` - Pause all reminders\n"
            "`/schedule` - View upcoming reminders\n"
            "`/setchannel` - Set default channel\n"
            "`/settimezone` - Set server timezone\n"
            "`/savetemplate` - Save reminder template\n"
            "`/usetemplate` - Use saved template"
        ),
        inline=False
    )

    # Tips
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Use `/addping` with time like '3pm' or 'tomorrow 2pm'\n"
            "• Create templates for common reminders\n"
            "• Set a default channel for convenience\n"
            "• Use the timezone setting for accurate timing"
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

@bot.tree.command(name="schedule", description="View upcoming reminders in chronological order")
@app_commands.describe(
    show_all="Show all reminders including inactive ones"
)
async def schedule(
    interaction: discord.Interaction,
    show_all: bool = False
):
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
    class ScheduleView(discord.ui.View):
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
            # Update button states
            self.prev_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= self.max_pages - 1

            # Create embed for current page
            start_idx = self.page * ITEMS_PER_PAGE
            page_reminders = reminders[start_idx:start_idx + ITEMS_PER_PAGE]

            embed = discord.Embed(
                title="📅 Upcoming Reminders",
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
                
                # Format next ping time
                next_time = datetime.fromisoformat(next_ping)
                if isinstance(next_time, str):
                    next_time = datetime.fromisoformat(next_time)
                next_time = pytz.utc.localize(next_time).astimezone(tz)
                
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

    view = ScheduleView()
    await view.update_view(interaction)

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