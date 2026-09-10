"""Gate 3 (spec §7): every cross-package relationship() resolves.

Splitting 34 models across context packages breaks string-form relationships
if a class is missing from the app.models aggregator. Without this gate that
failure surfaces at first query in production rather than at import.
"""

import pytest
from sqlalchemy.orm import configure_mappers

import app.models

EXPECTED_MODEL_COUNT = 34


@pytest.mark.smoke
def test_all_mappers_configure() -> None:
    configure_mappers()


@pytest.mark.smoke
def test_aggregator_exports_every_model() -> None:
    """Counted off the module namespace, not off a Base registry.

    During Phase 2 models live under two declarative bases at once — the
    legacy ``declarative_base()`` and the new ``app.core.db.Base`` — so
    asserting against either registry alone would fail at every intermediate
    point. Any class carrying ``__tablename__`` counts, whichever base it
    currently inherits from.
    """
    exported = [
        name
        for name in dir(app.models)
        if not name.startswith("_") and hasattr(getattr(app.models, name), "__tablename__")
    ]
    assert len(exported) == EXPECTED_MODEL_COUNT, (
        f"expected {EXPECTED_MODEL_COUNT} mapped classes, found {len(exported)}: {sorted(exported)}"
    )
