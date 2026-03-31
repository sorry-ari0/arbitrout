"""Shared test configuration."""
import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run async test functions with asyncio.run()")


def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if not inspect.iscoroutinefunction(testfunction):
        return None

    funcargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(testfunction(**funcargs))
    return True
