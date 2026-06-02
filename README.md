# Lore Event Relay

Discord bot that mirrors scheduled events from one master server to multiple target servers. Runs on a Raspberry Pi.

## How it works

1. You create an event in the **master server**
2. The bot automatically creates a copy of that event in every **target server**
3. If you edit or delete the event on the master, all copies are updated/deleted too
4. 30 minutes before the event starts, the bot sends a reminder in a configured channel on each target server, mentioning everyone who marked interest
5. A background repair pass checks for missing relays and recreates them automatically

## Setup

### 1. Discord Developer Portal

1. Go to [discord.com/developers](https://discord.com/developers) → New Application → Bot
2. Copy the bot token
3. Under **Privileged Gateway Intents**, enable:
   - `Server Members Intent`
   - `Guild Scheduled Events Intent`
4. Generate an invite link with scopes `bot` + `applications.commands` and permissions `Manage Events` + `Send Messages`
5. Invite the bot to the master server **and** every target server

### 2. Configuration

**`.env`**

```
BOT_TOKEN=your_token_here
MASTER_GUILD_ID=your_master_server_id
```

**`config.yaml`**

```yaml
target_guilds:
  - guild_id: "111111111"
    reminder_channel_id: "222222222"
  - guild_id: "333333333"
    reminder_channel_id: "444444444"

event_name_prefix: "[PD] - "
reminder_minutes_before: 30
reminder_message: "The event **{event_name}** starts in {minutes} minutes! {mentions}"
log_level: "INFO"
```

> `reminder_channel_id` is the text channel where the 30-min alert is sent. If omitted, no reminder is sent to that server.
> `reminder_message` supports `{event_name}`, `{mentions}`, and `{minutes}`.

### 3. Install & run

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

### 4. Raspberry Pi — run as a service

```bash
# Edit lore-relay.service and set the correct User and WorkingDirectory
sudo cp lore-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lore-relay

# View logs
sudo journalctl -u lore-relay -f
```

## Slash commands

All commands require **Manage Server** permission and are only visible to you.

| Command                       | Description                                                       |
| ----------------------------- | ----------------------------------------------------------------- |
| `/relay info`                 | Show current config and connected guilds                          |
| `/relay status`               | List all active relay mappings in the database                    |
| `/relay sync`                 | Create missing relays (use after the bot was offline)             |
| `/relay sync dry_run:true`    | Preview missing relays without creating them                      |
| `/relay repair`               | Verify relay copies still exist and recreate missing ones         |
| `/relay repair dry_run:true`  | Preview repairs without changing anything                         |
| `/relay cleanup`              | Remove database entries for events that no longer exist           |
| `/relay cleanup dry_run:true` | Preview cleanup without deleting records                          |
| `/relay health`               | Show bot, guild, and database health                              |
| `/relay audit`                | Show recent relay audit entries                                   |
| `/relay remind_test <id>`     | Force-send the 30-min reminder for a specific event (for testing) |

## Project structure

```
bot.py          — entry point, bot class, startup sync
config.py       — loads .env and config.yaml
database.py     — SQLite operations (aiosqlite)
config.yaml     — target guilds and settings
cogs/
  relay.py      — listens to event create/update/delete on master
  reminders.py  — background task, checks every minute for events starting in ~30 min
  admin.py      — slash commands
events.db       — auto-created, stores master↔relay event mappings
bot.log         — auto-created, full log output
```

## Notes

- The bot needs `Manage Events` permission in **every** server it is in
- If a target relay channel cannot be used, the bot falls back to an external event and logs the exact permission state
- The bot writes an audit trail for relay creation, repair, update, deletion, and reminder delivery
- The database now includes an `audit_log` table for recent operational history
- To get a server or channel ID: enable Developer Mode in Discord settings → right-click → Copy ID
- Relay events are prefixed with `[RELAY] ` by default (configurable)
- If the bot was offline when an event was created, run `/relay sync` or restart the bot (startup sync runs automatically once after login)
- If a target relay was deleted manually, run `/relay repair`
