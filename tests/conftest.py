import os
import sys
import tempfile
from pathlib import Path

# Изолированное окружение: config читает эти переменные при импорте.
_HOME = tempfile.mkdtemp(prefix="coolprint_test_")
os.environ["COOLPRINT_HOME"] = _HOME
os.environ["COOLPRINT_SIMULATE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


def make_pdf(path: Path, pages: int) -> Path:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595.28, height=841.89)
    with open(path, "wb") as f:
        w.write(f)
    return path


@pytest.fixture
def pdf(tmp_path):
    def _mk(pages=10, name="doc.pdf"):
        return make_pdf(tmp_path / name, pages)
    return _mk


# Без pytest-asyncio: корутинные тесты запускаем сами.
import asyncio  # noqa: E402
import inspect  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        args = {a: pyfuncitem.funcargs[a] for a in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(asyncio.wait_for(pyfuncitem.obj(**args), timeout=30))
        return True
