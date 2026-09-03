"""Gate 2 (spec §7): every module under app/ imports cleanly.

Catches circular imports introduced by the context split, and names that a
re-export shim failed to carry across. This test is only possible because the
application factory moved the Redis client, the error alerter, and the
database engine out of module scope — before that, importing the tree opened
network connections.
"""

import importlib
import pkgutil

import pytest

import app

#: Modules that raise on import *by design*, mapped to the reason. These are
#: tombstones left where functionality moved to a sibling service: importing
#: one is meant to fail loudly with a pointer, rather than silently succeed
#: and return stale behaviour. Add an entry only for a deliberate guard —
#: never to silence a genuine import error.
INTENTIONALLY_UNIMPORTABLE = {
    "app.adapters.ask_caspr": "Ask Caspr Q&A moved to ask-caspr-service",
}


@pytest.mark.smoke
def test_every_module_imports() -> None:
    failures: list[str] = []
    for info in pkgutil.walk_packages(app.__path__, prefix="app."):
        if info.name in INTENTIONALLY_UNIMPORTABLE:
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - report every failure at once
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


@pytest.mark.smoke
def test_tombstones_still_raise() -> None:
    """The allow-list must not become a place where real breakage hides.

    If one of these modules starts importing cleanly, either the tombstone was
    removed on purpose — in which case delete the entry — or something has
    shadowed it.
    """
    for name, reason in INTENTIONALLY_UNIMPORTABLE.items():
        with pytest.raises(ImportError):
            importlib.import_module(name)
        assert reason  # documents why the exemption exists
