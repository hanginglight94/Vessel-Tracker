# Telegram Vessel Tracker Bot

An interactive Telegram bot that tracks maritime vessels dynamically using their MMSI (Maritime Mobile Service Identity) number via VesselFinder scraping.

---

## Features
- **Interactive Commands**: `/start`, `/track <MMSI>`, `/status`, `/stop`, `/help`.
- **Live Vessel Telemetry**: Tracks vessel position (lat/lon), speed (SOG), course (COG), navigational status, destination, and ETA.
- **Automated Alerts**: Dispatches updates with Google Maps links whenever new AIS telemetry is received.
- **Session Management**: Independent per-chat tracking with asynchronous background loops.

---

## Setup & Running

### 1. Requirements
Python 3.11 is installed, along with the required dependencies in `requirements.txt`:
```bash
pip install -r requirements.txt
```

### 2. Configure Telegram Token
Create `.env` from `.env.example`:
```bash
cp .env.example .env
```
Open `.env` and set your `TELEGRAM_BOT_TOKEN` obtained from [@BotFather](https://t.me/BotFather):
```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
POLL_INTERVAL=60
```

### 3. Run the Bot
```bash
python bot.py
```
