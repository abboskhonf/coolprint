"""
state_manager.py — Управление состоянием и аудит (v7.2)
========================================================
Двойная система учета:
 1. state.json внутри папки задания — для восстановления при краше.
 2. print_history.csv в корне — Append-only журнал аудита.
"""

import json
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)

CSV_AUDIT_FILE = Path("print_history.csv")
CSV_HEADERS = [
    "Timestamp", 
    "Chat_ID", 
    "File_Name", 
    "Total_Pages", 
    "Copies", 
    "Batches_Done", 
    "Total_Batches", 
    "Status", 
    "SNMP_Start", 
    "SNMP_End", 
    "Duration_Min",
    "Notes"
]

class Phase:
    INIT = "init"
    PRINTING = "printing"
    COOLING = "cooling"
    PAUSED = "paused"
    FAILED = "failed"
    DONE = "done"

# ══════════════════════════════════════════════════════════════════════════════
# Журнал аудита (CSV Append-Only Log)
# ══════════════════════════════════════════════════════════════════════════════

class AuditLog:
    @staticmethod
    def _ensure_file_exists():
        if not CSV_AUDIT_FILE.exists():
            with open(CSV_AUDIT_FILE, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(CSV_HEADERS)

    @staticmethod
    def append(
        chat_id: int,
        file_name: str,
        total_pages: int,
        copies: int,
        batches_done: int,
        total_batches: int,
        status: str,
        snmp_start: int,
        snmp_end: int,
        duration_min: int = 0,
        notes: str = ""
    ):
        """Добавляет запись в конец CSV-файла."""
        try:
            AuditLog._ensure_file_exists()
            with open(CSV_AUDIT_FILE, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    chat_id,
                    file_name,
                    total_pages,
                    copies,
                    batches_done,
                    total_batches,
                    status,
                    snmp_start,
                    snmp_end,
                    duration_min,
                    notes
                ])
        except Exception as e:
            log.error(f"Ошибка записи в аудит CSV: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# Состояние задания (JSON внутри папки batches)
# ══════════════════════════════════════════════════════════════════════════════

class JobState:
    def __init__(self, batches_dir: Path):
        self.batches_dir = Path(batches_dir)
        self.state_file = self.batches_dir / "state.json"

    def save(self, data: Dict[str, Any]):
        """Сохраняет текущее состояние в state.json"""
        # Добавляем метку времени последнего обновления
        data["last_updated"] = datetime.now().isoformat()
        
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            log.error(f"Ошибка сохранения state.json: {e}")

    def load(self) -> Optional[Dict[str, Any]]:
        """Загружает состояние из state.json, если оно существует."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Ошибка чтения state.json: {e}")
            return None

    def update_phase(self, phase: str, **kwargs):
        """Точечное обновление фазы и дополнительных параметров."""
        state = self.load() or {}
        state["phase"] = phase
        state.update(kwargs)
        self.save(state)

    def is_done(self) -> bool:
        state = self.load()
        return state is not None and state.get("phase") == Phase.DONE

    @classmethod
    def find_interrupted_jobs(cls, parent_dir: Path) -> list[Dict[str, Any]]:
        """
        Сканирует папку загрузок и ищет задания, которые не завершены.
        Используется при старте бота для Recovery Mode.
        """
        interrupted = []
        if not parent_dir.exists():
            return interrupted

        for batch_dir in parent_dir.glob("canon_batches_*"):
            if batch_dir.is_dir():
                state_mgr = cls(batch_dir)
                state = state_mgr.load()
                # Подхватываем ЛЮБОЕ задание, которое не было успешно завершено (DONE)
                if state and state.get("phase") != Phase.DONE:
                    state["_batches_dir"] = str(batch_dir) # Сохраняем путь для бота
                    interrupted.append(state)
                    
        return interrupted