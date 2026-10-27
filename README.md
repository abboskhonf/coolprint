# 🖨 CoolPrint v8

Print queue for a **Canon iR2425** (any SNMP-capable Canon) that prints in batches with **cooldown pauses** so the fuser does not overheat. It runs as a **Windows service** and is controlled from a **web interface** and a **Telegram bot**.

## Features

- **Persistent queue** (`data/queue.json`): upload PDFs, drag to reorder (or ↑ ↓ / top), hold, cancel, retry, edit. Reordering takes effect at the next batch boundary, so it also works while printing.
- **Paper A4 / A3** per job, plus duplex (long/short/none), copies, page range, booklet, batch size and cooldown. Defaults can be changed live.
- **Shared cooldown** between batches of *all* jobs; survives a service restart; can be skipped or extended (+5 min).
- **Smart recovery**: after a jam / paper-out / network loss the exact number of printed sheets is read over SNMP and skipped on retry. A job interrupted by a restart mid-batch is put on **hold**, never reprinted blindly.
- **Printer watchdog**: OFFLINE / ERROR / WARNING freezes timeouts and waits for a human; a batch with no counter growth for `lost_job_min` is failed.
- **Emergency stop** (pauses the queue, cancels the job, purges the Windows spooler), history (`data/print_history.csv`), ETA per job.
- **Telegram** with admin and viewer roles, live status, notifications, full queue control.
- **Simulator mode** (`simulate = true`) to try everything without a printer.

## Requirements

- Windows 10/11 or Server (for the real service). macOS/Linux work for development, simulator mode and `lp` printing.
- **Python 3.11 – 3.13 recommended** (64-bit). Newer versions may lack `pywin32` wheels.
- [`PDFtoPrinter.exe`](http://www.columbia.edu/~em36/pdftoprinter.html) in the project folder (Windows only; not stored in git).
- Canon reachable over the network with **SNMP enabled** (community `public` by default).

## Installation (Windows)

1. **Get the code**
   ```bat
   git clone git@github.com:abboskhonf/coolprint.git C:\coolprint
   cd C:\coolprint
   ```
   (or `git pull` in an existing checkout). Put `PDFtoPrinter.exe` into `C:\coolprint`.

2. **Create the logical printers** (Control Panel → Devices and Printers). They must be **machine-wide**: the service runs in session 0 and cannot see per-user printers.

   | Printer name | Paper | Duplex |
   | :--- | :--- | :--- |
   | `Canon_Long` | A4 | two-sided, long edge |
   | `Canon_Short` | A4 | two-sided, short edge |
   | `Canon_None` | A4 | one-sided |
   | `Canon_A3_Long` / `_Short` / `_None` | A3 | same three modes (only if you print A3) |

   Names are up to you (e.g. `Canon A3 Short`), but they must match `config.ini` **exactly**. A4-only? Then you need just `Canon_Long`/`Canon_Short`/`Canon_None` and `available = A4`. **A3 needs its own printers**: the paper size comes from each printer's defaults, so an A4 printer cannot print A3. CoolPrint checks the names against Windows and tells you which one is missing.

   Point every one at the printer's IP, set the paper size and duplex under *Printing Defaults*, and set the port to **LPR** with **Byte Counting Enabled**.

3. **Configure**: copy `config.example.ini` to `config.ini` and edit it (see [Configuration](#configuration)). No `config.ini` is needed for a simulator trial: set `simulate = true`.

4. **Try it in a console first** (optional but recommended):
   ```bat
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python coolprint_app.py
   ```
   Open `http://localhost:8765/`, enter the token from `data\web_token.txt`, stop with Ctrl+C.

5. **Install the service** — right-click `install_service.bat` → *Run as administrator*. It creates the venv, installs dependencies, registers the `CoolPrint` service (auto-start, restart on failure), opens TCP 8765 for the *private* firewall profile and starts it.

   Manual control: `.venv\Scripts\python windows_service.py start | stop | restart | remove`, or `services.msc`.

   Debug a service problem in a console: `.venv\Scripts\python windows_service.py debug`.

6. **Open the UI**: `http://<pc-address>:8765/`. The access token is `[web] token` from `config.ini`, or the generated `data\web_token.txt`. Logs: `data\coolprint.log`.

### macOS / Linux (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
COOLPRINT_SIMULATE=1 .venv/bin/python coolprint_app.py
.venv/bin/python -m pytest
```

## Configuration

`config.ini` (see `config.example.ini` for every option). The important parts:

```ini
[printer]
ip = 192.168.1.50
name_long = Canon_Long        ; A4 logical printers (Windows)
name_short = Canon_Short
name_none = Canon_None
; simulate = true             ; no real printer

[paper]
available = A4,A3
a3_long = Canon_A3_Long
a3_short = Canon_A3_Short
a3_none = Canon_A3_None
a3_counter_weight = 1         ; SNMP counter clicks per A3 side (often 2 on Canon)
fit_to_paper = true

[print]                       ; defaults for new jobs
batch_size = 16
cooldown_minutes = 6
duplex = long

[telegram]
token = <from @BotFather>
chat_id = 111111111           ; admins (comma separated)
viewer_ids = 222222222        ; read-only

[web]
host = 0.0.0.0
port = 8765
token =                       ; empty = generated in data/web_token.txt

[service]
on_failure = halt             ; halt = pause queue after a failed job; continue = next job
```

`config.ini` holds secrets: never commit it, and revoke any bot token that has been committed.

## Using it

**Web UI**: drop a PDF on the upload area, choose paper / duplex / copies / pages / booklet, then drag jobs to reorder. Pause/resume the queue, skip or extend the cooldown, emergency stop, finished jobs and log tabs. RU/EN and dark mode.

**Telegram**: send a PDF → choose options → *add to end* or *front of queue*.

| Command | Action |
| :--- | :--- |
| `/status` | live status: printer, cooldown, current batch (admin gets buttons) |
| `/queue` | queue with ⬆ ⬇ reorder, hold, cancel, retry, per-job paper |
| `/pause`, `/resume` | pause the queue (current batch finishes) / resume |
| `/skip` | skip the cooldown (asks for confirmation) |
| `/stop` | emergency stop (asks for confirmation) |
| `/settings` | defaults for new jobs |
| `/quiet` | no per-batch messages |

Telegram's bot API cannot download files over 20 MB: use the web upload for those. Do not run another bot on the same token.

## REST API

All `/api/*` calls need the token (`X-Token: …`, `Authorization: Bearer …` or `?token=`).

`GET /api/status` · `GET /api/events` (SSE) · `POST /api/jobs` (multipart: `file`, optional `paper duplex copies booklet batch_size cooldown page_from page_to position`) · `PATCH /api/jobs/{id}` · `POST /api/jobs/{id}/{up|down|top|bottom|move|hold|release|cancel|retry|delete}` · `POST /api/queue/{pause|resume}` · `POST /api/cooldown/{skip|add}` · `POST /api/emergency-stop` · `GET|PUT /api/defaults` · `GET /api/history`

## Security notes

- The web token travels over plain HTTP. Keep the port inside the LAN/VPN (firewall rule is for the private profile only).
- Telegram chats not listed in `chat_id` / `viewer_ids` are ignored.

## Troubleshooting

- **Service won't start**: run `windows_service.py debug`; check `data\coolprint.log` and the Windows Event Log.
- **"Принтер Windows «…» не найден"**: the printer named in `config.ini` does not exist (check spelling/spaces with `Get-Printer` in PowerShell, or create it as a machine-wide printer). Remove the `a3_*` keys and set `available = A4` if you don't print A3.
- **Prints nothing / wrong queue**: names in `config.ini` must match the Windows printer names exactly, and the printers must be machine-wide.
- **A3 comes out scaled wrong or the counter jumps by 2**: tune the A3 printer defaults and `a3_counter_weight`.
- **Telegram silent**: check DNS/internet on the server (the bot retries with backoff) and that your ID is in `chat_id`.

## Project layout

| File | Role |
| :--- | :--- |
| `coolprint_app.py` | entry point: engine + web + Telegram |
| `windows_service.py`, `install_service.bat` | Windows service wrapper / installer |
| `engine.py` | scheduler: batches, cooldown, watchdog, recovery |
| `jobs.py` | job model, persistent queue, event hub |
| `printer_backend.py` | real printer (SNMP + spooler) and simulator |
| `web_api.py`, `static/index.html` | REST/SSE API and web UI |
| `telegram_ui.py` | Telegram bot |
| `config.py` | settings, paths (anchored to the app folder) |
| `pdf_processor.py`, `print_service.py`, `snmp_manager.py`, `state_manager.py` | PDF splitting/booklet, sending, SNMP, audit log |
| `main.py` | original standalone CLI (still works) |
| `archive/legacy_bot/` | the old v7.6 Telegram bot, kept for reference |
| `tests/` | pytest suite (simulator based) |
