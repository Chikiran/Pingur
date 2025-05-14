# Pingur Discord Bot

Pingurrr

## Commands

### General Commands
- `/addping` - Create interval-based ping
- `/addreminder` - Create time-based reminder
- `/list` - View all reminders/pings
- `/editping` - Modify reminder
- `/removeping` - Delete ping
- `/removereminder` - Delete reminder
- `/pauseping` - Pause a reminder
- `/resumeping` - Resume a reminder
- `/pauseall` - Pause all reminders

### Server Settings
- `/setchannel` - Set default channel
- `/settimezone` - Set server timezone

### Templates
- `/savetemplate` - Save reminder template
- `/usetemplate` - Use saved template
- `/listtemplates` - View all templates

### Owner Commands
- `/setstatus` - Set bot's activity status
- `/setnick` - Change bot's nickname
- `/setavatar` - Update bot's profile picture
- `/setbio` - Update bot's 'About Me'

## Deployment

1. Clone the repository:
```bash
git clone https://github.com/chikiran/pingur.git
cd pingur
```

2. Create and activate virtual environment:
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

3. Install requirements:
```bash
pip install -r requirements.txt
```

4. Create `.env` file:
```
DISCORD_TOKEN=your_bot_token_here
```

5. Run the bot:
```bash
python bot.py
```

## Requirements
- Python 3.10+
- discord.py
- python-dotenv
- aiosqlite
- pytz 