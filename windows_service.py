"""
windows_service.py — служба Windows «CoolPrint» (pywin32).

    python windows_service.py install     (от администратора; см. install_service.bat)
    python windows_service.py start | stop | remove
    python windows_service.py debug       (запуск в консоли для проверки)

Служба стартует в C:\\Windows\\System32 и в сеансе 0, поэтому рабочая папка
выставляется явно, а логические принтеры должны быть общесистемными.
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

HOME = Path(__file__).resolve().parent
os.environ.setdefault("COOLPRINT_HOME", str(HOME))
os.chdir(HOME)
sys.path.insert(0, str(HOME))

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


class CoolPrintService(win32serviceutil.ServiceFramework):
    _svc_name_ = "CoolPrint"
    _svc_display_name_ = "CoolPrint — очередь печати Canon"
    _svc_description_ = "Очередь печати с паузами на охлаждение, веб-интерфейс и Telegram-бот."

    def __init__(self, args):
        super().__init__(args)
        self._halt = win32event.CreateEvent(None, 0, 0, None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: asyncio.Event | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self._loop and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)
        win32event.SetEvent(self._halt)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        done = threading.Event()

        def runner():
            import coolprint_app
            coolprint_app.setup_logging()

            async def main():
                self._loop = asyncio.get_running_loop()
                self._stop_evt = asyncio.Event()
                await coolprint_app.run_app(self._stop_evt)

            try:
                asyncio.run(main())
            except Exception:
                import logging
                logging.getLogger("coolprint").exception("Служба аварийно остановилась")
            finally:
                done.set()
                win32event.SetEvent(self._halt)

        threading.Thread(target=runner, name="coolprint", daemon=True).start()
        win32event.WaitForSingleObject(self._halt, win32event.INFINITE)
        done.wait(timeout=30)       # дать движку остановиться: печатающаяся пачка остаётся в очереди
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                              (self._svc_name_, ""))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(CoolPrintService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(CoolPrintService)
