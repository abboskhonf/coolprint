#!/usr/bin/env python3
# test_snmp.py — Диагностика Canon iR2425 (pysnmp v7.1.x)

import sys
import asyncio
import configparser
import os
import time
from pathlib import Path

# В 7.1.x используем только snake_case имена
try:
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, 
        CommunityData, 
        UdpTransportTarget, 
        ContextData, 
        ObjectType, 
        ObjectIdentity, 
        get_cmd  # <--- Теперь get_cmd вместо getCmd
    )
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.exit(1)

OIDS = {
    "Имя устройства":  "1.3.6.1.2.1.1.5.0",
    "Статус принтера": "1.3.6.1.2.1.25.3.5.1.1.1",
    "Счетчик страниц": "1.3.6.1.2.1.43.10.2.1.4.1.1",
}

STATUS_MAP = {1: 'Paper (Сон)', 3: 'IDLE (Готов)', 4: 'PRINTING (Печать)', 5: 'WARMUP (Прогрев)'}

def load_snmp_config():
    base_path = Path(__file__).parent
    config_path = base_path / "config.ini"
    
    if not config_path.exists():
        print(f"❌ Ошибка: Файл {config_path} не найден!")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    
    try:
        return {
            "ip": cfg.get("printer", "ip"),
            "community": cfg.get("snmp", "community", fallback="public"),
            "port": cfg.getint("snmp", "port", fallback=161),
            "timeout": cfg.getfloat("snmp", "timeout", fallback=3.0),
            "retries": cfg.getint("snmp", "retries", fallback=2)
        }
    except Exception as e:
        print(f"❌ Ошибка чтения config.ini: {e}")
        sys.exit(1)

async def snmp_get(snmp_engine, oid, params):
    """Асинхронный GET через современный API 7.1.x."""
    try:
        # В 7.1.x транспорт можно создавать как через .create(), так и напрямую
        transport = await UdpTransportTarget.create(
            (params["ip"], params["port"]), 
            timeout=params["timeout"], 
            retries=params["retries"]
        )

        result = await get_cmd(
            snmp_engine,
            CommunityData(params["community"], mpModel=0), # SNMP v1
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid))
        )
        
        # get_cmd возвращает (errorIndication, errorStatus, errorIndex, varBinds)
        error_indication, error_status, error_index, var_binds = result

        if error_indication or error_status:
            return None
        
        return var_binds[0][1]
    except Exception:
        return None

async def run_diagnostics():
    params = load_snmp_config()
    snmp_engine = SnmpEngine()
    
    print("="*55)
    print(f"📡 Диагностика SNMP v1 (pysnmp 7.1.23)")
    print(f"📍 IP: {params['ip']} | Port: {params['port']}")
    print("="*55)
    
    print("► Попытка подключения...")
    name = await snmp_get(snmp_engine, OIDS["Имя устройства"], params)
    
    if name is None:
        print("❌ ПРОВАЛ: Принтер не отвечает. Проверьте сеть и настройки SNMP.")
        return

    print(f"✅ Связь установлена! Устройство: {name}")

    print("\nОпрос датчиков (Ctrl+C для выхода)...")
    try:
        while True:
            # Опрашиваем данные
            status_val = await snmp_get(snmp_engine, OIDS["Статус принтера"], params)
            count_val = await snmp_get(snmp_engine, OIDS["Счетчик страниц"], params)
            
            # Обработка статуса
            if status_val is not None:
                status_str = STATUS_MAP.get(int(status_val), f"ДРУГОЕ ({status_val})")
            else:
                status_str = "ОШИБКА ОПРОСА"
                
            count_str = int(count_val) if count_val is not None else "---"
            
            print(f"[{time.strftime('%H:%M:%S')}] Статус: {status_str:<18} | Счетчик: {count_str}")
            await asyncio.sleep(2)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nДиагностика остановлена пользователем.")

if __name__ == "__main__":
    try:
        asyncio.run(run_diagnostics())
    except KeyboardInterrupt:
        pass