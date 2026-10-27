"""
config.py — настройки CoolPrint.

Все пути привязаны к APP_DIR (а не к текущей папке): Windows-служба стартует
в C:\\Windows\\System32, относительные пути там ломаются.
"""
import configparser
import json
import os
import threading
from pathlib import Path

from snmp_manager import SNMPConfig

APP_DIR     = Path(os.environ.get("COOLPRINT_HOME") or Path(__file__).resolve().parent)
CONFIG_FILE = APP_DIR / "config.ini"
DATA_DIR    = APP_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
WORK_DIR    = DATA_DIR / "work"
for _d in (DATA_DIR, UPLOADS_DIR, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg

CFG = load_config()

# ── Принтер ──────────────────────────────────────────────────────────────────
PRINTER_IP      = CFG.get("printer", "ip",   fallback="192.168.1.100")
PRINTER_NAME    = CFG.get("printer", "name", fallback="Canon iR2425")
SIMULATE        = (CFG.getboolean("printer", "simulate", fallback=False)
                   or os.environ.get("COOLPRINT_SIMULATE") == "1")

# ── Telegram ─────────────────────────────────────────────────────────────────
BOT_TOKEN        = CFG.get("telegram", "token", fallback="").strip()
DOWNLOAD_TIMEOUT = CFG.getint("telegram", "download_timeout", fallback=300)

def _id_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()]

# chat_id = 111, 222  — администраторы (полный доступ)
ADMIN_CHAT_IDS  = _id_list(CFG.get("telegram", "chat_id", fallback=""))
# viewer_ids = 333    — только просмотр статуса и очереди
VIEWER_CHAT_IDS = _id_list(CFG.get("telegram", "viewer_ids", fallback=""))
ALLOWED_CHAT_ID = ",".join(map(str, ADMIN_CHAT_IDS))   # для старого кода

# ── SNMP ─────────────────────────────────────────────────────────────────────
SNMP_CFG = SNMPConfig(
    community = CFG.get("snmp", "community", fallback="public"),
    port      = CFG.getint("snmp", "port",   fallback=161),
    timeout   = CFG.getfloat("snmp", "timeout", fallback=3.0),
    retries   = CFG.getint("snmp", "retries",   fallback=2),
)
WAKE_TIMEOUT = CFG.getint("snmp", "wake_timeout_sec",   fallback=360)
WATCHDOG_MIN = CFG.getint("snmp", "print_watchdog_min", fallback=40)
# Сколько минут принтер может простаивать без прироста счётчика, прежде чем пачка
# считается потерянной (сеть/спулер). 2 часа — как в CLI-версии.
LOST_JOB_MIN = CFG.getint("snmp", "lost_job_min", fallback=120)

# ── Печать (значения по умолчанию, их можно менять в рантайме) ───────────────
DEFAULT_BATCH    = CFG.getint("print", "batch_size",        fallback=64)
DEFAULT_COOLDOWN = CFG.getint("print", "cooldown_minutes",  fallback=25)
DEFAULT_DUPLEX   = CFG.get("print",   "duplex",             fallback="long")
DEFAULT_COPIES   = CFG.getint("print", "copies",            fallback=1)
CHUNKS_PER_BATCH = CFG.getint("print", "chunks_per_batch",  fallback=1)

# ── Бумага ───────────────────────────────────────────────────────────────────
PAPER_SIZES = {          # портретная ориентация, pt (1 pt = 1/72 дюйма)
    "A4": (595.28, 841.89),
    "A3": (841.89, 1190.55),
}
DEFAULT_PAPER   = CFG.get("paper", "default", fallback="A4").upper()
FIT_TO_PAPER    = CFG.getboolean("paper", "fit_to_paper", fallback=True)

def paper_counter_weight(paper: str) -> int:
    """Сколько единиц счётчика Canon добавляет одна сторона листа этого формата.
    На многих Canon A3 считается как 2 клика — проверьте на своём аппарате."""
    return max(1, CFG.getint("paper", f"{paper.lower()}_counter_weight", fallback=1))

# ── Очередь / служба ─────────────────────────────────────────────────────────
# halt     — после сбоя задания очередь встаёт на паузу (принтер, вероятно, в беде)
# continue — сразу переходить к следующему заданию
ON_FAILURE   = CFG.get("service", "on_failure", fallback="halt").lower()
HISTORY_DAYS = CFG.getint("service", "history_days", fallback=14)

WEB_ENABLED = CFG.getboolean("web", "enabled", fallback=True)
WEB_HOST    = CFG.get("web", "host", fallback="0.0.0.0")
WEB_PORT    = CFG.getint("web", "port", fallback=8765)
_WEB_TOKEN_CFG = CFG.get("web", "token", fallback="").strip()

def web_token() -> str:
    """Токен веб-интерфейса: из config.ini, иначе генерируется один раз и хранится в data/."""
    if _WEB_TOKEN_CFG:
        return _WEB_TOKEN_CFG
    f = DATA_DIR / "web_token.txt"
    if not f.exists():
        import secrets
        f.write_text(secrets.token_urlsafe(18), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


# ── Маршрутизация на логические принтеры Windows ─────────────────────────────
def printer_queue(paper: str, duplex: str) -> str | None:
    """Имя логического принтера Windows для (формат, дуплекс) или None, если не настроен.

    [paper] a3_long = Canon_A3_Long      — для любого формата
    [printer] name_long = Canon_Long     — запасной вариант для A4 (старые конфиги)
    """
    name = CFG.get("paper", f"{paper.lower()}_{duplex}", fallback="").strip()
    if not name and paper == "A4":
        name = CFG.get("printer", f"name_{duplex}", fallback="").strip()
    return name or None

def supported_papers() -> list[str]:
    """Форматы, которые реально можно печатать на этой машине."""
    import platform
    listed = [p.strip().upper() for p in CFG.get("paper", "available", fallback="A4,A3").split(",") if p.strip()]
    listed = [p for p in listed if p in PAPER_SIZES] or ["A4"]
    if platform.system() != "Windows" or SIMULATE:
        return listed
    return [p for p in listed if any(printer_queue(p, d) for d in ("long", "short", "none"))] or ["A4"]


# ── Настройки, меняемые на лету (веб/Telegram) ───────────────────────────────
_SETTINGS_FILE = DATA_DIR / "settings.json"
_settings_lock = threading.Lock()
_EDITABLE = {
    "batch_size": (int, 2, 400), "cooldown": (int, 0, 240),
    "copies": (int, 1, 99),
}

def defaults() -> dict:
    """Параметры нового задания: config.ini, поверх них — data/settings.json."""
    d = {"batch_size": DEFAULT_BATCH, "cooldown": DEFAULT_COOLDOWN, "duplex": DEFAULT_DUPLEX,
         "copies": DEFAULT_COPIES, "paper": DEFAULT_PAPER, "booklet": CFG.get("print", "booklet", fallback="none")}
    try:
        d.update(json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return d

def update_defaults(patch: dict) -> dict:
    with _settings_lock:
        cur = defaults()
        for k, v in patch.items():
            if k in _EDITABLE:
                typ, lo, hi = _EDITABLE[k]
                cur[k] = max(lo, min(hi, typ(v)))
            elif k == "duplex" and v in ("long", "short", "none"):
                cur[k] = v
            elif k == "paper" and str(v).upper() in PAPER_SIZES:
                cur[k] = str(v).upper()
            elif k == "booklet" and v in ("none", "left", "right"):
                cur[k] = v
        tmp = _SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        os.replace(tmp, _SETTINGS_FILE)
        return cur

# Старый бот (handlers.py / bot.py)
DOWNLOADS_DIR = UPLOADS_DIR
