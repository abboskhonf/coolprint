# Handoff: CoolPrint (continue on the Windows machine)

Written for: a Claude Code session started on the Windows PC where CoolPrint is installed (`C:\coolprint`). Reply to the user in English by default; they also read and write Russian.

## Project
CoolPrint v8: print queue for a Canon iR2425 (192.168.1.50) that prints in batches with cooldown pauses so the fuser does not overheat. Runs as a Windows service `CoolPrint`; controlled via a web UI (`http://<pc>:8765/`, token in `data\web_token.txt` or `[web] token`) and a Telegram bot. Full docs: `README.md`. Repo: `git@github.com:abboskhonf/coolprint.git`, branch `main`.

The code was written and tested on a Mac with the simulator (20 pytest tests pass). **Everything that touches real Windows, the real printer, PDFtoPrinter or the Telegram API was untested until the user started live testing.** Treat Windows behaviour as unverified unless the user confirms it.

## State right now
- Installed on Windows, web UI opens and accepts uploads. Test job: `jfi_section_1_booklet.pdf`, A3, duplex short, 36 pages, batch 8, cooldown 8 min.
- Windows printers were created with `create_printers.bat`; the SYSTEM read-back printed `[OK]` for all six:
  `Canon_Long`, `Canon_Short`, `Canon_None` (A4) and `Canon_A3_Long`, `Canon_A3_Short`, `Canon_A3_None` (A3). Driver is **Canon Office XPS Class Driver**, port `192.168.1.50` (RAW). An LPR port `LPR_Canon` also exists but is not used by these.
- **Open problem:** the job sits in the UI as "в очереди" (queued) and printing never starts.
  - A job the engine picked up is shown as "печатается" with a phase (Подготовка / Ждём принтер / Охлаждение), so QUEUED means the engine has not taken it.
  - Leading hypothesis: the queue is **paused**. `[service] on_failure = halt` pauses the queue after any failed job (e.g. the earlier "A3 printer not found" failures), and the pause is persisted in `data\queue.json`, so it survives restarts. Not yet confirmed; the user has not reported the result.
  - Other possibility: the service/engine is not running or crashed at startup.

## First things to do
1. Check the pause state and engine health:
   ```bat
   powershell -NoProfile -Command "$t=(gc C:\coolprint\data\web_token.txt).Trim(); $s=irm http://localhost:8765/api/status -Headers @{'X-Token'=$t}; $s | select phase,paused,backend; $s.printer"
   powershell -NoProfile -Command "Get-Content C:\coolprint\data\coolprint.log -Tail 60"
   ```
   If `paused` is True: `POST /api/queue/resume` or the "Продолжить" button in the UI. Then find out *why* it paused (the log shows the failed job and error).
2. If the service is not running: `.venv\Scripts\python windows_service.py debug` in an admin console shows the traceback (the service wrapper `windows_service.py` has never been run before this deployment).
3. Once a batch actually prints, check on the real hardware:
   - the page comes out on **A3** with correct scaling (PDFtoPrinter -> XPS class driver is unverified),
   - duplex edge is right,
   - the **SNMP counter weight for A3**: if the counter rises by 2 per A3 side, set `[paper] a3_counter_weight = 2` in `config.ini` (otherwise batches look "lost" or finish early),
   - cooldown starts after the batch and the next batch waits for it.

## How printing works (so you can debug quickly)
- Paper size and duplex are **not** passed to PDFtoPrinter (it has no such option). They come from the Windows logical printer's *Printing Defaults*. `config.printer_queue(paper, duplex)` maps (A4|A3, long|short|none) to a printer name from `config.ini`: `[paper] a3_long/a3_short/a3_none`, A4 from `[printer] name_long/short/none`. Names must match `Get-Printer` output exactly. `print_service.check_queue` reports a missing printer by name before sending.
- The service runs as LocalSystem in session 0: printers must be machine-wide, and paths are anchored to the app folder (`APP_DIR`, env `COOLPRINT_HOME`), not the CWD.
- Engine (`engine.py`): picks the first QUEUED job, prepares PDF batches (`pdf_processor.py`), waits for cooldown, waits for printer IDLE/SLEEP over SNMP, sends chunks via `print_service`, then polls the SNMP page counter until the expected count is reached. A watchdog freezes on OFFLINE/ERROR/WARNING; a batch with no counter growth for `lost_job_min` fails the job (and `halt` pauses the queue).
- Queue persisted in `data\queue.json`; cooldown end time in `data\engine_state.json`; history in `data\print_history.csv`; log in `data\coolprint.log`. A job in flight when the service restarts becomes HELD (never reprinted blindly).

## File map
`coolprint_app.py` entry (engine + web + Telegram) · `windows_service.py` + `install_service.bat` service wrapper/installer · `engine.py` scheduler · `jobs.py` Job/JobQueue/EventHub · `printer_backend.py` real (SNMP + spooler) and simulated backends · `web_api.py` + `static/index.html` REST/SSE + UI · `telegram_ui.py` bot · `config.py` settings · `print_service.py` / `pdf_processor.py` / `snmp_manager.py` / `state_manager.py` low level · `create_printers.bat/.ps1` clone and configure Windows printers · `tests/` pytest (simulator) · `archive/legacy_bot/` old v7.6 bot · `main.py` original CLI (untouched).

## Constraints and warnings
- **Do not print or commit `config.ini` contents.** It holds Telegram bot tokens. Earlier, commented-out tokens were exposed in tool output and the file has been tracked in git; the user was told to **revoke all of them in BotFather** (confirm they did). `config.ini` stays uncommitted; `config.example.ini` is the safe template.
- Do not run the legacy bot (`archive/legacy_bot`) and the new bot on the same token.
- Windows Python: 3.11-3.13 recommended (pywin32 wheels). `PDFtoPrinter.exe` lives in the project folder and is not in git.
- Windows-side tests: `.venv\Scripts\pip install -r requirements-dev.txt` then `.venv\Scripts\python -m pytest -q`.
- Commit only when the user asks. Trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Git
`main` is pushed up to `cde92af` (create_printers scripts). Uncommitted locally on the Mac: `config.ini`, `.DS_Store` (do not commit) and this file until the user pushes it.
