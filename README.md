# Samfundet Ticket Bot

An automated tool to reserve tickets on [Samfundet](https://www.samfundet.no/) via Billig and open Stripe checkout links automatically. It supports running multiple bots concurrently.

## Features

- **Parallel Bot Execution**: Run multiple bot instances targeting different events/accounts simultaneously.
- **Strictly Standard Library**: Built using standard Python library packages (`urllib`, `threading`, `concurrent.futures`, `argparse`, `webbrowser`, etc.). No external dependencies to install!
- **Scheduling**: Wait until a specific time of day (e.g. `14:00` or `14:00:00`) before beginning to poll.
- **Smart Price Selection**: Automatically filters and selects member/non-member tickets depending on preferences.
- **Graceful Termination**: Responds instantly to `Ctrl+C` interrupt signaling to clean up all threads immediately.

---

## Installation

### Prerequisites
- **Python 3.9+** is required.

No packages need to be installed. To verify your installation, run:
```bash
python3 bot.py --help
```

---

## Usage

### 1. Single Bot Mode

Run a single bot directly from the command line using arguments:

```bash
# Reserve 2 tickets for Togo event, using digital email ticket, waiting until 14:00 local time
python3 bot.py --event 5423-toga --email you@example.com --wait-until 14:00 --count 2

# Reserve a ticket using a member card, matching cheapest member tier automatically
python3 bot.py --event 5279-annika --membercard 12345678 --price-type member
```

#### Command Line Arguments
- `--event`: Event slug or URL, e.g. `5423-toga` or `5279-annika` (required unless `--config` is used).
- `--email`: Email address for digital tickets (required if `--membercard` is not provided).
- `--membercard`: Samfundet member card number (required if `--email` is not provided).
- `--price-type`: Which price group to purchase. Options: `member`, `non-member`, or `auto` (default).
- `--count`: Number of tickets to reserve (1–9). Default is `1`.
- `--wait-until`: Delay polling until local time (format: `HH:MM` or `HH:MM:SS`).
- `--poll-interval`: Seconds to wait between polls. Default is `0.25`.
- `--max-attempts`: Stop polling after $N$ attempts (default: unlimited).
- `--no-browser`: Print Stripe checkout URL instead of launching it automatically.

---

### 2. Parallel Bot Mode

To run multiple bots concurrently, specify a JSON configuration file.

```bash
python3 bot.py --config config.json
```

#### Configuration File Format (`config.json`)
The configuration must be a JSON array of bot objects. Each bot configuration has the following schema:

```json
[
  {
    "event": "5423-toga",
    "email": "you@example.com",
    "count": 2,
    "wait_until": "14:00"
  },
  {
    "event": "5279-annika",
    "membercard": "12345678",
    "price_type": "member",
    "poll_interval": 0.5
  }
]
```

#### Parallel Mode Behaviors
- **Thread-safe logging**: Output is prefixed with each bot's event ID (e.g. `[5423-toga] Waiting… 59s`) to prevent logs from interleaving.
- **Global override**: You can add `--no-browser` to the command line to prevent *all* bots in the configuration from launching browsers simultaneously when checkout is ready.
- **Ctrl+C Grace**: Main process will notify and exit all active threads immediately.

---

## 3. Streamlit App (GUI)

A browser-based UI is also available, built on top of the same bot logic in `bot.py`.

### Install and run

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

### Features
- **Simple mode**: a single-bot form (event, email/member card, price type, count, optional wait time) — the default view.
- **Parallel mode**: toggle "Run multiple bots in parallel" to switch to a spreadsheet-style table for configuring several bots at once, just like `config.json`.
- **Live log**: streams the bot's progress in real time while it polls.
- **Auto-open checkout**: opens the Stripe checkout in your browser automatically when ready (toggle under "Advanced settings" to disable and just get a link instead).
- **Stop button**: cancels running bot(s) immediately.
