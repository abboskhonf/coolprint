"""
snmp_manager.py — Canon iR2425 SNMP интерфейс (v6.0 Async)
============================================================
Требует: pysnmp-lextudio >= 7.1.0
Установка: pip install pysnmp-lextudio

Все публичные функции — async def.
Вызывать через await из async-контекста.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any

try:
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine,
        CommunityData,
        UdpTransportTarget,
        ContextData,
        ObjectType,
        ObjectIdentity,
        get_cmd,
    )
    SNMP_AVAILABLE = True
except ImportError:
    SNMP_AVAILABLE = False

log = logging.getLogger(__name__)


# ── OID справочник ────────────────────────────────────────────────────────────
class OID:
    PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"    # hrPrinterStatus
    DEVICE_STATUS  = "1.3.6.1.2.1.25.3.2.1.5.1"    # hrDeviceStatus
    PAGE_COUNTER   = "1.3.6.1.2.1.43.10.2.1.4.1.1"  # prtMarkerLifeCount
    ERROR_STATE    = "1.3.6.1.2.1.25.3.5.1.2.1"    # hrPrinterDetectedErrorState


class PrinterStatus(Enum):
    IDLE     = "idle"
    PRINTING = "printing"
    WARMUP   = "warmup"
    WARNING  = "warning"
    ERROR    = "error"
    UNKNOWN  = "unknown"
    OFFLINE  = "offline"
    SLEEP = "sleep"


HR_STATUS_MAP = {
    1: PrinterStatus.SLEEP,  # Трактуем "Сон" как "Готов проснуться"
    2: PrinterStatus.UNKNOWN,
    3: PrinterStatus.IDLE,
    4: PrinterStatus.PRINTING,
    5: PrinterStatus.WARMUP,
}

@dataclass
class PrinterState:
    status: PrinterStatus
    page_count: int
    error_msg: str = ""


@dataclass
class SNMPConfig:
    community: str  = "public"
    port: int       = 161
    timeout: float  = 3.0
    retries: int    = 2


# Один движок на весь процесс (создаётся лениво при первом вызове)
_engine: Optional[Any] = None

def _get_engine() -> Any:
    global _engine
    if _engine is None:
        if not SNMP_AVAILABLE:
            raise RuntimeError("pysnmp-lextudio не установлен: pip install pysnmp-lextudio")
        _engine = SnmpEngine()
    return _engine


# ── Низкоуровневый async GET ──────────────────────────────────────────────────

async def _snmp_get(ip: str, oid: str, cfg: SNMPConfig) -> tuple[Any, Optional[str]]:
    """
    Возвращает (value, error_str).
    value=None при любой ошибке.
    """
    if not SNMP_AVAILABLE:
        return None, "pysnmp-lextudio не установлен"

    try:
        # В pysnmp 7.x UdpTransportTarget создаётся через await
        transport = await UdpTransportTarget.create(
            (ip, cfg.port),
            timeout=cfg.timeout,
            retries=cfg.retries,
        )

        # mpModel=0 → SNMP v1 (Canon iR2425 работает именно на v1)
        error_ind, error_status, _, var_binds = await get_cmd(
            _get_engine(),
            CommunityData(cfg.community, mpModel=0),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )

        if error_ind:
            return None, str(error_ind)
        if error_status:
            return None, str(error_status)

        return var_binds[0][1], None

    except Exception as e:
        return None, str(e)


# ── Публичные async функции ───────────────────────────────────────────────────

async def get_printer_state(ip: str, cfg: SNMPConfig) -> PrinterState:
    """Один снимок состояния принтера."""
    val, err = await _snmp_get(ip, OID.PRINTER_STATUS, cfg)

    if err:
        log.debug(f"SNMP недоступен [{ip}]: {err}")
        return PrinterState(PrinterStatus.OFFLINE, 0, err)

    try:
        status = HR_STATUS_MAP.get(int(val), PrinterStatus.UNKNOWN)
    except (ValueError, TypeError):
        status = PrinterStatus.UNKNOWN

    cnt_val, cnt_err = await _snmp_get(ip, OID.PAGE_COUNTER, cfg)
    page_count = 0
    if not cnt_err and cnt_val is not None:
        try:
            page_count = int(cnt_val)
        except (ValueError, TypeError):
            pass

    return PrinterState(status, page_count)


async def wait_until_idle(
    ip: str,
    cfg: SNMPConfig,
    timeout_sec: int = 360,
    stop_event: Optional[asyncio.Event] = None,
    on_offline_callback=None
) -> bool:
    """Ждёт перехода принтера в статус IDLE. Замораживает таймер при оффлайне/ошибках."""
    deadline = time.monotonic() + timeout_sec
    offline_notified = False

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            return False

        state = await get_printer_state(ip, cfg)

        if state.status == PrinterStatus.IDLE:
            return True

        # 🔻 МАГИЯ ЗАМОРОЗКИ ВРЕМЕНИ 🔻
        # Если нет сети или физическая проблема (бумага/замятие) - мы ждем бесконечно.
        if state.status in (PrinterStatus.OFFLINE, PrinterStatus.ERROR, PrinterStatus.WARNING):
            deadline = time.monotonic() + timeout_sec  # Сдвигаем дедлайн вперед!
            
            if state.status == PrinterStatus.OFFLINE:
                if not offline_notified and on_offline_callback:
                    try:
                        on_offline_callback() # Уведомляем Telegram 1 раз
                    except Exception:
                        pass
                    offline_notified = True
                await asyncio.sleep(15)
            else:
                offline_notified = False
                await asyncio.sleep(5)
            continue
        # 🔺 ------------------------- 🔺

        offline_notified = False
        await asyncio.sleep(5)

    log.error(f"Принтер не вышел в IDLE за {timeout_sec} сек")
    return False


async def wait_for_print_cycle(
    ip: str,
    cfg: SNMPConfig,
    start_count: int,
    expected_pages: int,
    wake_timeout_sec: int = 300,
    print_timeout_min: int = 40,
    stop_event: Optional[asyncio.Event] = None,
) -> tuple[bool, str, int]:
    """
    Конечный автомат: IDLE → warmup/printing → IDLE → верификация счётчика.

    Возвращает (success, message, end_page_count).
    """

    async def interruptible_sleep(sec: float) -> bool:
        """True если stop_event сработал, False если просто таймаут."""
        if stop_event:
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=sec,
                )
                return True
            except asyncio.TimeoutError:
                return False
        await asyncio.sleep(sec)
        return False

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # ── Фаза 1: Пробуждение ───────────────────────────────────────────────────
    log.info("  [SNMP] Фаза 1/3: ожидание пробуждения принтера...")
    wake_deadline = time.monotonic() + wake_timeout_sec
    woke_up = False

    while time.monotonic() < wake_deadline:
        if stopped():
            st = await get_printer_state(ip, cfg)
            return False, "Прервано пользователем", st.page_count

        state = await get_printer_state(ip, cfg)

        # Принтер начал готовиться
        if state.status in (PrinterStatus.WARMUP, PrinterStatus.PRINTING):
            log.info(f"  [SNMP] Принтер активен [{state.status.value}]")
            woke_up = True
            break

        # «Быстрая» печать — файл напечатался раньше цикла опроса
        # Засчитываем только если прирост >= ожидаемого (защита от ложных +)
        threshold = max(expected_pages - 1, 1)
        if state.page_count >= start_count + threshold:
            delta = state.page_count - start_count
            log.info(f"  [SNMP] Быстрая печать (delta={delta})")
            return True, f"Напечатано {delta} стр.", state.page_count

        if await interruptible_sleep(5):
            st = await get_printer_state(ip, cfg)
            return False, "Прервано пользователем", st.page_count

    if not woke_up:
        end = await get_printer_state(ip, cfg)
        threshold = max(expected_pages - 1, 1)
        if end.page_count >= start_count + threshold:
            delta = end.page_count - start_count
            return True, f"Напечатано {delta} стр.", end.page_count
        return False, (
            f"Таймаут пробуждения ({wake_timeout_sec}с). "
            f"Счётчик: {end.page_count}, ожидали +{expected_pages}"
        ), end.page_count

    # ── Фаза 2: Watchdog — ждём возврата в IDLE ───────────────────────────────
    log.info("  [SNMP] Фаза 2/3: ожидание завершения печати (watchdog)...")
    watchdog_deadline = time.monotonic() + (print_timeout_min * 60)

    while time.monotonic() < watchdog_deadline:
        if stopped():
            st = await get_printer_state(ip, cfg)
            return False, "Прервано пользователем", st.page_count

        state = await get_printer_state(ip, cfg)

        if state.status == PrinterStatus.IDLE:
            break

        if state.status in (PrinterStatus.ERROR, PrinterStatus.WARNING):
            return False, f"Аппаратная ошибка: {state.status.value}", state.page_count

        if await interruptible_sleep(8):
            st = await get_printer_state(ip, cfg)
            return False, "Прервано пользователем", st.page_count
    else:
        cnt = (await get_printer_state(ip, cfg)).page_count
        return False, f"Watchdog: печать не завершилась за {print_timeout_min} мин", cnt

    # ── Фаза 3: Верификация счётчика ─────────────────────────────────────────
    log.info("  [SNMP] Фаза 3/3: верификация счётчика...")
    await asyncio.sleep(5)  # Canon обновляет счётчик с небольшой задержкой

    end_state = await get_printer_state(ip, cfg)
    delta = end_state.page_count - start_count
    threshold = max(expected_pages - 1, 1)

    if delta >= threshold:
        return (
            True,
            f"Напечатано {delta} стр. (ожидалось ≥{threshold})",
            end_state.page_count,
        )

    return False, (
        f"Верификация провалена: счётчик +{delta}, ожидалось ≥{threshold}. "
        f"Задание отменено или зависло в спулере?"
    ), end_state.page_count
