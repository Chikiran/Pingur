import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta
import asyncio
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot setup
intents = discord.Intents.default()
intents.members = True

class PingurBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.tree_sync_flag = False

    async def setup_hook(self):
        if not self.tree_sync_flag:
            await self.tree.sync()
            self.tree_sync_flag = True

bot = PingurBot()

# Database setup
DB_PATH = 'reminders.db'

async def setup_database():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                target_id INTEGER,
                message TEXT,
                interval INTEGER,
                last_ping TIMESTAMP,
                is_dm BOOLEAN
            )
        ''')
        await db.commit()

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    await setup_database()
    check_reminders.start()

def has_admin_permission():
    async def predicate(interaction: discord.Interaction):
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

@bot.tree.command(name="addping", description="Add a new ping reminder")
@has_admin_permission()
async def add_ping(
    interaction: discord.Interaction,
    target: discord.Member,
    interval: int,
    is_dm: bool,
    message: str
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO reminders (guild_id, channel_id, user_id, target_id, message, interval, last_ping, is_dm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (interaction.guild_id, interaction.channel_id, interaction.user.id, target.id, message, interval, datetime.now(), is_dm))
        await db.commit()
    
    await interaction.response.send_message(
        f'Reminder set! Will ping {target.mention} every {interval} minutes with: "{message}"'
    )

@bot.tree.command(name="listpings", description="List all active ping reminders")
@has_admin_permission()
async def list_pings(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT id, target_id, message, interval, is_dm FROM reminders
            WHERE guild_id = ?
        ''', (interaction.guild_id,)) as cursor:
            reminders = await cursor.fetchall()
    
    if not reminders:
        await interaction.response.send_message('No active reminders!')
        return

    content = '**Active Reminders:**\n'
    for r in reminders:
        target = interaction.guild.get_member(r[1])
        if target:
            content += f'ID: {r[0]} | Target: {target.mention} | Message: "{r[2]}" | Interval: {r[3]}min | DM: {r[4]}\n'
    
    await interaction.response.send_message(content)

@bot.tree.command(name="removeping", description="Remove a ping reminder by its ID")
@has_admin_permission()
async def remove_ping(interaction: discord.Interaction, reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM reminders WHERE id = ? AND guild_id = ?', (reminder_id, interaction.guild_id))
        await db.commit()
    
    await interaction.response.send_message(f'Reminder {reminder_id} has been removed!')

@bot.tree.command(name="editping", description="Edit an existing ping reminder")
@has_admin_permission()
async def edit_ping(
    interaction: discord.Interaction,
    reminder_id: int,
    interval: int = None,
    is_dm: bool = None,
    message: str = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT interval, is_dm, message FROM reminders WHERE id = ? AND guild_id = ?', 
                            (reminder_id, interaction.guild_id)) as cursor:
            current = await cursor.fetchone()
            
        if not current:
            await interaction.response.send_message('Reminder not found!')
            return
            
        new_interval = interval if interval is not None else current[0]
        new_is_dm = is_dm if is_dm is not None else current[1]
        new_message = message if message is not None else current[2]
        
        await db.execute('''
            UPDATE reminders 
            SET interval = ?, is_dm = ?, message = ?
            WHERE id = ? AND guild_id = ?
        ''', (new_interval, new_is_dm, new_message, reminder_id, interaction.guild_id))
        await db.commit()
    
    await interaction.response.send_message(f'Reminder {reminder_id} has been updated!')

@tasks.loop(minutes=1)
async def check_reminders():
    """Check and send reminders"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT * FROM reminders') as cursor:
            reminders = await cursor.fetchall()
            
        for reminder in reminders:
            id, guild_id, channel_id, user_id, target_id, message, interval, last_ping, is_dm = reminder
            last_ping = datetime.fromisoformat(last_ping)
            
            if datetime.now() - last_ping >= timedelta(minutes=interval):
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                    
                target = guild.get_member(target_id)
                if not target:
                    continue
                
                try:
                    if is_dm:
                        await target.send(f'{message}')
                    else:
                        channel = guild.get_channel(channel_id)
                        if channel:
                            await channel.send(f'{target.mention} {message}')
                            
                    await db.execute('''
                        UPDATE reminders 
                        SET last_ping = ? 
                        WHERE id = ?
                    ''', (datetime.now(), id))
                    await db.commit()
                except discord.Forbidden:
                    print(f'Failed to send message to {target}')

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
    else:
        await interaction.response.send_message(f"An error occurred: {str(error)}", ephemeral=True)

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN')) 