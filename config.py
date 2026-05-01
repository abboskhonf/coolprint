"""
config.py
"""
import configparser
from pathlib import Path
from snmp_manager import SNMPConfig

CONFIG_FILE = "config.ini"

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg

CFG = load_config()

PRINTER_IP      = CFG.get("printer", "ip",   fallback="192.168.1.100")
PRINTER_NAME    = CFG.get("printer", "name", fallback="Canon iR2425")
BOT_TOKEN       = CFG.get("telegram", "token",   fallback="").strip()
ALLOWED_CHAT_ID = CFG.get("telegram", "chat_id", fallback="").strip()
DOWNLOAD_TIMEOUT = CFG.getint("telegram", "download_timeout", fallback=300)

SNMP_CFG = SNMPConfig(
    community = CFG.get("snmp", "community", fallback="public"),
    port      = CFG.getint("snmp", "port",   fallback=161),
    timeout   = CFG.getfloat("snmp", "timeout", fallback=3.0),
    retries   = CFG.getint("snmp", "retries",   fallback=2),
)
WAKE_TIMEOUT = CFG.getint("snmp", "wake_timeout_sec",   fallback=360)
WATCHDOG_MIN = CFG.getint("snmp", "print_watchdog_min", fallback=40)

DEFAULT_BATCH    = CFG.getint("print", "batch_size",        fallback=64)
DEFAULT_COOLDOWN = CFG.getint("print", "cooldown_minutes",  fallback=25)
DEFAULT_DUPLEX   = CFG.get("print",   "duplex",             fallback="long")
DEFAULT_COPIES   = CFG.getint("print", "copies",            fallback=1)
CHUNKS_PER_BATCH = CFG.getint("print", "chunks_per_batch",  fallback=1)

DOWNLOADS_DIR = Path("bot_downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)