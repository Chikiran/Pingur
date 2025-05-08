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

# Load environment variables
load_dotenv()

# Constants
ITEMS_PER_PAGE = 5
TIME_UNITS = {
    'minutes': 1,
    'hours': 60,
    'days': 1440
}

# Bot setup
intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Needed for reaction handling

class PingurBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.tree_sync_flag = False
        self.default_channels = {}  # Guild ID: Channel ID
        self.timezone = "UTC"
        self.reminder_pages = {}  # Store pagination info

    async def setup_hook(self):
        if not self.tree_sync_flag:
            await self.tree.sync()
            self.tree_sync_flag = True

bot = PingurBot()

# Database setup
DB_PATH = 'reminders.db'

async def setup_database():
    async with aiosqlite.connect(DB_PATH) as db:
        # Create reminders table with enhanced fields
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                target_ids TEXT,
                target_type TEXT DEFAULT 'user',
                message TEXT,
                interval INTEGER,
                time_unit TEXT DEFAULT 'minutes',
                last_ping TIMESTAMP,
                next_ping TIMESTAMP,
                is_dm BOOLEAN,
                category TEXT DEFAULT 'general',
                active BOOLEAN DEFAULT true,
                is_recurring BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create settings table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                default_channel_id INTEGER,
                timezone TEXT DEFAULT 'UTC'
            )
        ''')
        
        # Create templates table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminder_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                name TEXT,
                message TEXT,
                interval INTEGER,
                category TEXT,
                UNIQUE(guild_id, name)
            )
        ''')
        
        await db.commit()

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
    rid, guild_id, channel_id, user_id, target_ids, target_type, msg, interval, time_unit, last_ping, next_ping, is_dm, category, active, is_recurring, created_at = reminder
    
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
            f"Type: {'Recurring' if is_recurring else 'One-time'}\n"
            f"Next ping: <t:{int(datetime.fromisoformat(next_ping).timestamp())}:R>"
        ),
        inline=True
    )
    embed.add_field(
        name="📍 Location",
        value=f"Channel: {channel.mention if channel else 'Unknown'}\nDM: {is_dm}",
        inline=True
    )
    embed.add_field(
        name="ℹ️ Details",
        value=f"Category: {category}\nStatus: {'🟢 Active' if active else '🔴 Inactive'}",
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

@bot.tree.command(name="addping", description="Add a new ping reminder")
@app_commands.describe(
    targets="Users/Roles to ping (mention them or use IDs)",
    time_unit="Unit of time for interval",
    interval="Time between pings",
    message="Message to send with the ping",
    is_dm="Send as DM instead of channel message (only for users)",
    channel="Channel to send pings (optional)",
    category="Category for organizing reminders",
    is_recurring="Whether this reminder should repeat",
)
async def add_ping(
    interaction: discord.Interaction,
    targets: str,
    time_unit: Literal['minutes', 'hours', 'days'],
    interval: int,
    message: str,
    is_dm: bool = False,
    channel: Optional[discord.TextChannel] = None,
    category: str = "general",
    is_recurring: bool = True
):
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
        else:  # Try as direct ID
            try:
                user_id = int(word)
                member = interaction.guild.get_member(user_id)
                if member:
                    if not target_type:
                        target_type = 'user'
                    elif target_type != 'user':
                        await interaction.response.send_message(
                            "Cannot mix users and roles in the same reminder!",
                            ephemeral=True
                        )
                        return
                    target_ids.append(user_id)
                else:
                    role = interaction.guild.get_role(user_id)
                    if role:
                        if not target_type:
                            target_type = 'role'
                        elif target_type != 'role':
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

    if target_type == 'role' and is_dm:
        await interaction.response.send_message(
            "Cannot send DMs to roles! Please use channel mentions for roles.",
            ephemeral=True
        )
        return

    # Get channel ID
    if channel:
        channel_id = channel.id
    else:
        channel_id = bot.default_channels.get(interaction.guild_id)
        if not channel_id and not is_dm:
            await interaction.response.send_message(
                "No default channel set! Use /setchannel first or specify a channel.",
                ephemeral=True
            )
            return

    # Calculate interval in minutes and next ping time
    interval_minutes = interval * TIME_UNITS[time_unit]
    now = datetime.now()
    next_ping = now + timedelta(minutes=interval_minutes)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO reminders (
                guild_id, channel_id, user_id, target_ids, target_type,
                message, interval, time_unit, last_ping, next_ping,
                is_dm, category, is_recurring
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
            is_dm,
            category,
            is_recurring
        ))
        reminder_id = (await db.execute('SELECT last_insert_rowid()')).fetchone()[0]
        await db.commit()

        # Fetch the newly created reminder
        async with db.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,)) as cursor:
            reminder = await cursor.fetchone()

    embed = await create_reminder_embed(interaction, reminder)
    embed.title = "✅ New Reminder Created"
    
    await interaction.response.send_message(embed=embed)

class ReminderView(discord.ui.View):
    def __init__(self, reminders: list, page: int = 0):
        super().__init__(timeout=300)
        self.reminders = reminders
        self.page = page
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
        page_reminders = self.reminders[start_idx:start_idx + ITEMS_PER_PAGE]

        embed = discord.Embed(
            title="📋 Reminder List",
            description=f"Page {self.page + 1} of {self.max_pages}",
            color=discord.Color.blue()
        )

        for reminder in page_reminders:
            sub_embed = await create_reminder_embed(interaction, reminder)
            # Convert sub_embed to field in main embed
            embed.add_field(
                name=f"Reminder #{reminder[0]}",
                value=sub_embed.fields[-1].value,  # Get the message field
                inline=False
            )

        await interaction.response.edit_message(embed=embed, view=self)

@bot.tree.command(name="listpings", description="List all ping reminders")
@app_commands.describe(
    category="Filter by category",
    show_inactive="Show inactive reminders",
    target="Filter by specific user/role"
)
async def list_pings(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    show_inactive: bool = False,
    target: Optional[Union[discord.User, discord.Role]] = None
):
    query = '''
        SELECT *
        FROM reminders
        WHERE guild_id = ?
    '''
    params = [interaction.guild_id]

    if category:
        query += ' AND category = ?'
        params.append(category)

    if not show_inactive:
        query += ' AND active = 1'

    if target:
        query += ' AND target_ids LIKE ?'
        params.append(f'%{target.id}%')

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cursor:
            reminders = await cursor.fetchall()

    if not reminders:
        if category or target:
            await interaction.response.send_message(
                '❌ No reminders found matching your criteria!',
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                '❌ No reminders found!',
                ephemeral=True
            )
        return

    view = ReminderView(reminders)
    await view.update_view(interaction)

@bot.tree.command(name="editping", description="Edit a reminder")
@app_commands.describe(
    reminder_id="ID of the reminder to edit",
    interval="New interval",
    time_unit="New time unit",
    message="New message",
    channel="New channel",
    category="New category",
    is_dm="New DM setting",
    active="Set reminder active/inactive",
    is_recurring="Set if reminder should repeat"
)
async def edit_ping(
    interaction: discord.Interaction,
    reminder_id: Optional[int] = None,
    interval: Optional[int] = None,
    time_unit: Optional[Literal['minutes', 'hours', 'days']] = None,
    message: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
    category: Optional[str] = None,
    is_dm: Optional[bool] = None,
    active: Optional[bool] = None,
    is_recurring: Optional[bool] = None
):
    if reminder_id is None:
        # Show reminder selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT * FROM reminders WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.response.send_message('❌ No reminders found!', ephemeral=True)
            return

        options = []
        for r in reminders:
            rid, _, _, _, target_ids, target_type, msg, interval, time_unit, _, _, is_dm, category, active, _, _ = r
            preview = f"#{rid} - {msg[:50]}..." if len(msg) > 50 else f"#{rid} - {msg}"
            options.append(discord.SelectOption(
                label=preview,
                value=str(rid),
                description=f"{interval} {time_unit}, {'DM' if is_dm else 'Channel'}"
            ))

        class ReminderSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(
                    placeholder="Choose a reminder to edit...",
                    options=options[:25]  # Discord limits to 25 options
                )

            async def callback(self, interaction: discord.Interaction):
                selected_id = int(self.values[0])
                await edit_ping(interaction, reminder_id=selected_id)

        class ReminderSelectView(discord.ui.View):
            def __init__(self):
                super().__init__()
                self.add_item(ReminderSelect())

        await interaction.response.send_message(
            "Select a reminder to edit:",
            view=ReminderSelectView(),
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Get current values
        async with db.execute('SELECT * FROM reminders WHERE id = ? AND guild_id = ?', 
                            (reminder_id, interaction.guild_id)) as cursor:
            reminder = await cursor.fetchone()
            
        if not reminder:
            await interaction.response.send_message('❌ Reminder not found!', ephemeral=True)
            return

        # Build update query
        query = 'UPDATE reminders SET '
        params = []
        updates = []

        if interval is not None or time_unit is not None:
            current_interval = interval if interval is not None else reminder[7]
            current_unit = time_unit if time_unit is not None else reminder[8]
            minutes = current_interval * TIME_UNITS[current_unit]
            next_ping = datetime.now() + timedelta(minutes=minutes)
            
            if interval is not None:
                updates.append('interval = ?')
                params.append(interval)
            if time_unit is not None:
                updates.append('time_unit = ?')
                params.append(time_unit)
            
            updates.append('next_ping = ?')
            params.append(next_ping.isoformat())

        if message is not None:
            updates.append('message = ?')
            params.append(message)
        if channel is not None:
            updates.append('channel_id = ?')
            params.append(channel.id)
        if category is not None:
            updates.append('category = ?')
            params.append(category)
        if is_dm is not None:
            updates.append('is_dm = ?')
            params.append(is_dm)
        if active is not None:
            updates.append('active = ?')
            params.append(active)
        if is_recurring is not None:
            updates.append('is_recurring = ?')
            params.append(is_recurring)

        if not updates:
            await interaction.response.send_message('❌ No changes specified!', ephemeral=True)
            return

        query += ', '.join(updates)
        query += ' WHERE id = ? AND guild_id = ?'
        params.extend([reminder_id, interaction.guild_id])

        await db.execute(query, params)
        await db.commit()

        # Fetch updated reminder
        async with db.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,)) as cursor:
            updated_reminder = await cursor.fetchone()

    embed = await create_reminder_embed(interaction, updated_reminder)
    embed.title = "✅ Reminder Updated"
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="removeping", description="Remove a reminder")
@app_commands.describe(
    reminder_id="ID of the reminder to remove"
)
async def remove_ping(
    interaction: discord.Interaction,
    reminder_id: Optional[int] = None
):
    if reminder_id is None:
        # Show reminder selector
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT * FROM reminders WHERE guild_id = ?', 
                                (interaction.guild_id,)) as cursor:
                reminders = await cursor.fetchall()
                
        if not reminders:
            await interaction.response.send_message('❌ No reminders found!', ephemeral=True)
            return

        options = []
        for r in reminders:
            rid, _, _, _, target_ids, target_type, msg, interval, time_unit, _, _, is_dm, category, active, _, _ = r
            preview = f"#{rid} - {msg[:50]}..." if len(msg) > 50 else f"#{rid} - {msg}"
            options.append(discord.SelectOption(
                label=preview,
                value=str(rid),
                description=f"{interval} {time_unit}, {'DM' if is_dm else 'Channel'}"
            ))

        class ReminderSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(
                    placeholder="Choose a reminder to remove...",
                    options=options[:25]  # Discord limits to 25 options
                )

            async def callback(self, interaction: discord.Interaction):
                selected_id = int(self.values[0])
                await remove_ping(interaction, reminder_id=selected_id)

        class ReminderSelectView(discord.ui.View):
            def __init__(self):
                super().__init__()
                self.add_item(ReminderSelect())

        await interaction.response.send_message(
            "Select a reminder to remove:",
            view=ReminderSelectView(),
            ephemeral=True
        )
        return

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
            id, guild_id, channel_id, user_id, target_ids, target_type, message, interval, time_unit, last_ping, next_ping, is_dm, category, active, is_recurring, created_at, timezone = reminder
            
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
        'setchannel', 'settimezone', 'savetemplate', 'usetemplate'
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
                    "`/addping targets:<@users/roles> interval:<number> "
                    "time_unit:[minutes/hours/days] message:<text>`\n"
                    "Optional: `is_dm:True/False channel:#channel category:<text> "
                    "is_recurring:True/False`"
                ),
                inline=False
            ).add_field(
                name="Examples",
                value=(
                    "1. `/addping targets:@user interval:30 time_unit:minutes "
                    "message:Time for a break!`\n"
                    "2. `/addping targets:@role interval:1 time_unit:days "
                    "message:Daily reminder channel:#announcements`\n"
                    "3. `/addping targets:@user interval:2 time_unit:hours "
                    "is_dm:True message:Take your medicine`"
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
                    "- `interval` and `time_unit`\n"
                    "- `message`\n"
                    "- `channel`\n"
                    "- `category`\n"
                    "- `is_dm`\n"
                    "- `active`\n"
                    "- `is_recurring`"
                ),
                inline=False
            ).add_field(
                name="Examples",
                value=(
                    "1. `/editping reminder_id:1 interval:45 time_unit:minutes`\n"
                    "2. `/editping reminder_id:2 active:false`\n"
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
                    "- `category:<text>`\n"
                    "- `show_inactive:True/False`\n"
                    "- `target:@user/@role`"
                ),
                inline=False
            ).add_field(
                name="Features",
                value=(
                    "- Page through reminders with buttons\n"
                    "- Filter by category or target\n"
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
            "• Ping users or roles\n"
            "• One-time or recurring reminders\n"
            "• DM or channel messages\n"
            "• Flexible time intervals\n"
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
            "• Use categories to organize reminders\n"
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

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN')) 