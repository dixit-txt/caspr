# caspr-core Rule Book Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure `caspr-core` from a layer-first layout into the domain-first layout mandated by R-STRUCT-1, adding the tooling and application-factory scaffolding the standard requires, without changing any business logic.

**Architecture:** Four phases. Phase 0 freezes the HTTP contract as a machine-checkable baseline. Phase 1 builds `src/app/` alongside the existing tree and moves cross-cutting modules into `core/`, `adapters/`, `observability/`, and `research/`. Phase 2 migrates 13 bounded contexts one at a time, leaving re-export shims in the old modules so untouched code keeps importing what it always did. Phase 3 deletes the old tree and the shims. Phase 4 runs every gate.

**Tech Stack:** Python 3.14.5 · uv 0.12.x · FastAPI 0.141.1 · Starlette 1.6.0 · SQLAlchemy 2.0 (async, asyncpg) · Alembic · Redis · pytest · ruff · mypy

**Spec:** `docs/superpowers/specs/2026-09-03-caspr-core-rulebook-alignment-design.md`

## Global Constraints

- **Never commit.** No `git commit`, no `git push`, at any point. All work is left in the working tree for review. Where the writing-plans template calls for a commit step, this plan substitutes a verification step.
- **No logic changes.** Function bodies move verbatim. The only permitted edits are import statements, plus the five changes enumerated in spec §4: the application factory (§4.2), `Annotated` dependency aliases (§4.3), the health endpoint split (§4.4), `NAMING_CONVENTION` (§4.5), and tooling/Dockerfile (§4.6, §4.7).
- **Scope is `caspr-core` only.** Do not modify `ask-caspr`, `file-handling`, or `render-report`.
- **The route inventory must never regress.** `uv run python tests/smoke/route_inventory.py` must report `OK: N operations match baseline` after every task — `N` is 132 up to Task 8, and 134 from Task 9 onward. The health endpoint split in Task 9 is the **only** sanctioned baseline change in the whole plan, and it must add exactly `GET /health/live` and `GET /health/ready` and nothing else. Any other diff is a defect, never a reason to re-baseline.
- **`/api/v1/health` must keep working.** `render-report` probes it for readiness. It stays as a deprecated alias.
- Python floor `>=3.14.5`; `.python-version` is `3.14.5`; ruff `target-version = "py314"`; mypy `python_version = "3.14"`.
- Line length 100. Ruff owns formatting; `E501` is ignored.
- Every shim carries the exact comment `# TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)` so it is greppable.

---

## Phase 0 — Freeze the contract

### Task 1: Route inventory baseline ✅ COMPLETE

**Files:**
- Create: `tests/smoke/route_inventory.py`
- Create: `tests/smoke/baseline_routes.json`

**Interfaces:**
- Produces: `build_inventory(app) -> dict`, `load_baseline() -> dict`, `diff(baseline, current) -> list[str]`, `BASELINE: Path`

This task is already done. It is recorded here because every later task depends on it and a re-runner needs to know why the module is shaped the way it is.

The inventory is built from `app.openapi()`, **not** by walking `app.routes`. Under FastAPI 0.141 / Starlette 1.6 an included router is stored as a single opaque `fastapi.routing._IncludedRouter` rather than being flattened, so `len(app.routes)` is 10 for this service, not 132. A gate built on `app.routes` would compare 10 against 10 and report success while checking nothing.

- [x] **Step 1: Write the inventory module** — done, at `tests/smoke/route_inventory.py`.
- [x] **Step 2: Write the baseline** — `uv run python tests/smoke/route_inventory.py --write-baseline` → `baseline written: 132 operations`.
- [x] **Step 3: Verify it passes against itself** — `uv run python tests/smoke/route_inventory.py` → `OK: 132 operations match baseline`.
- [x] **Step 4: Negative control** — deleting one operation and adding a bogus one produced `REMOVED DELETE /internal/db/grep/vector-store-files` and `ADDED POST /api/v1/bogus`, confirming the gate detects drift rather than passing vacuously.

---

## Phase 1 — Skeleton

### Task 2: Development tooling ✅ COMPLETE

**Files:**
- Modify: `pyproject.toml`
- Create: `.pre-commit-config.yaml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/pull_request_template.md`
- Create: `tests/smoke/test_route_inventory.py`
- Create: `tests/__init__.py`, `tests/smoke/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `route_inventory.build_inventory`, `load_baseline`, `diff`, `_load_app` from Task 1.
- Produces: working `uv run ruff check .`, `uv run mypy src`, `uv run pytest`.

- [x] **Step 1: Add dev dependencies**

```bash
uv add --group dev pytest 'pytest-asyncio>=1.0' pytest-cov httpx asgi-lifespan 'ruff>=0.16' mypy
```

- [x] **Step 2: Add tool configuration to `pyproject.toml`**

Append these blocks. `select` is explicit so a ruff upgrade cannot silently change what CI enforces (R-TOOL-1).

```toml
[tool.ruff]
line-length = 100
target-version = "py314"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM", "ASYNC", "S", "T20", "RUF"]
ignore = ["E501"]  # the formatter owns line length

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106"]
"db_migrations/**" = ["E501", "I001"]
# Debt register (R-TOOL-1). Each entry is a module needing a logic pass the
# structural migration is not allowed to make. Shrink this list, never extend it.
"src/app/core/constants.py" = ["S105", "S106"]
"src/app/research/**" = ["S", "B", "SIM", "ASYNC", "T20"]
"src/app/observability/**" = ["S", "B", "SIM", "T20"]

[tool.mypy]
python_version = "3.14"
strict = true
plugins = ["pydantic.mypy"]
warn_unused_ignores = true
warn_redundant_casts = true
no_implicit_reexport = true
exclude = ["db_migrations/versions/"]

[tool.pydantic-mypy]
init_typed = true
init_forbid_extra = true

# Debt register (R-TOOL-2). strict stays a real gate for everything not listed.
[[tool.mypy.overrides]]
module = ["app.research.*", "app.observability.*", "app.adapters.*", "app.core.constants"]
ignore_errors = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "-q --strict-markers"
markers = ["unit", "integration", "e2e", "smoke"]
testpaths = ["tests"]
```

- [x] **Step 3: Write the pytest wrapper for gate 1**

```python
# tests/smoke/test_route_inventory.py
"""Gate 1 (spec §7): the HTTP contract must survive the restructure."""

import pytest

from tests.smoke.route_inventory import _load_app, build_inventory, diff, load_baseline


@pytest.mark.smoke
def test_route_inventory_matches_baseline() -> None:
    problems = diff(load_baseline(), build_inventory(_load_app()))
    assert not problems, "HTTP contract changed:\n" + "\n".join(problems)
```

- [x] **Step 4: Create `tests/conftest.py`**

Present so the repository root is importable when pytest is invoked from a
subdirectory, and as the home for shared fixtures added later.

```python
"""Shared pytest configuration.

Kept deliberately thin. The application-level fixtures the standard's
R-TEST-3 describes (AsyncClient + ASGITransport + LifespanManager) arrive
with the real test suite; the structural migration only needs the smoke
gates, which construct the app themselves.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [x] **Step 5: Run it and verify it passes**

Run: `uv run pytest tests/smoke/test_route_inventory.py -v`
Expected: PASS.

- [x] **Step 6: Create `.pre-commit-config.yaml`**

Local hooks, so versions come from `uv.lock` alone and cannot drift from CI (R-TOOL-3).

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: uv run ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format
        entry: uv run ruff format
        language: system
        types: [python]
      - id: mypy
        name: mypy
        entry: uv run mypy src
        language: system
        pass_filenames: false
      - id: route-inventory
        name: route inventory unchanged
        entry: uv run python tests/smoke/route_inventory.py
        language: system
        pass_filenames: false
```

- [x] **Step 7: Create `.github/workflows/ci.yml`**

```yaml
name: CI
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v8
        with: { enable-cache: true }
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run pytest -m "unit or integration or smoke"
```

- [x] **Step 8: Create `.github/pull_request_template.md`**

Copy the checklist from the standard's Section 14 verbatim, unchanged.

- [x] **Step 9: Verify the gates run**

Run: `uv run ruff check . ; uv run pytest -m smoke -v ; uv run python tests/smoke/route_inventory.py`
Expected: pytest PASS; inventory `OK: 132 operations match baseline`. Ruff will report findings against the *old* tree — record the count, do not fix them here; they disappear as files move and the per-file-ignores land.

---

### Task 3: Package identity and the `src/app` skeleton ✅ COMPLETE

**Files:**
- Modify: `pyproject.toml`
- Create: `src/app/__init__.py`, `src/app/core/__init__.py`, `src/app/adapters/__init__.py`, `src/app/observability/__init__.py`, `src/app/research/__init__.py`
- Create: `src/app/<context>/__init__.py` for all 13 contexts
- Create: `src/app/core/db.py`
- Create: `src/app/models.py`

**Interfaces:**
- Produces: `app.core.db.Base`, `app.core.db.NAMING_CONVENTION`, `app.core.db.build_engine(dsn) -> AsyncEngine`, `app.core.db.SessionFactory`, `app.core.db.get_session`, `app.core.db.SessionDep`; `app.models` re-exporting all 34 ORM classes.

- [x] **Step 1: Make the project an installed package**

In `pyproject.toml` replace `package = false`:

```toml
[tool.uv]
package = true

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/app"]
```

- [x] **Step 2: Create the package directories**

```bash
mkdir -p src/app/{core,adapters,observability,research} \
         src/app/{auth,chats,reports,cards,deliverables,leads,dashboard,wallet,billing,referrals,onboarding,admin,internal}
find src/app -type d -exec touch {}/__init__.py \;
```

- [x] **Step 3: Write `src/app/core/db.py`**

The engine construction is lifted verbatim from `src/db/db_utils.py:23-40`; only its placement changes, from module import time into a callable (R-DB-3). `NAMING_CONVENTION` is new (R-DB-7).

```python
"""Database engine, session factory, and the declarative Base.

The engine is built by ``build_engine`` and owned by the application
lifespan (R-DB-3), not constructed at import time as it was in the
pre-migration ``src/db/db_utils.py``. That change is what makes this module
importable without a reachable database, which gate 2 depends on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# R-DB-7. Alembic autogenerate cannot see unnamed constraints, so this must be
# in place before any further constraint is added.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(dsn: str) -> AsyncEngine:
    """Engine settings carried over verbatim from src/db/db_utils.py."""
    return create_async_engine(
        dsn,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=60,
        connect_args={"command_timeout": 120, "timeout": 30},
    )


SessionFactory = async_sessionmaker(expire_on_commit=False, class_=AsyncSession)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with SessionFactory(bind=request.app.state.engine) as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise  # R-DB-5: must re-raise or the global handlers never fire
        finally:
            await session.close()


SessionDep = Annotated[AsyncSession, Depends(get_session)]  # R-FA-2
```

- [x] **Step 4: Write `src/app/models.py` as an aggregator**

At this point no models have moved, so it re-exports from the old module. Each context task replaces one line here.

```python
"""Single import site for every ORM class (spec §3.2).

SQLAlchemy resolves the string form of ``relationship("Wallet")`` against the
declarative registry at mapper-configuration time. Once models are split
across context packages, a class that has not been imported by then fails
resolution at first query rather than at import. Importing this module
guarantees every mapper is registered.

``app.main`` and ``db_migrations/env.py`` both import it.
"""

from src.db.database import (  # noqa: F401  # TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)
    AskCasprChat,
    CallBooking,
    Card,
    CardVersion,
    ChatFile,
    CostTracker,
    FileVersion,
    Message,
    Publish,
    Referral,
    RefinementHistory,
    Report,
    ReportVersion,
    ReportVersionCard,
    Request,
    ResearchInterest,
    Subscriber,
    Subscription,
    SubscriptionInterval,
    Table,
    TokenBatch,
    TokenTransaction,
    University,
    UploadedFile,
    UploadedFileChunk,
    User,
    UserResearchInterest,
    UserRole,
    UserVectorStore,
    VectorStoreFile,
    Wallet,
    WebSearchCitation,
    WebSearchEvent,
    WebSearchRawResponse,
)
```

- [x] **Step 5: Verify the package installs and the aggregator imports**

Run: `uv sync && uv run python -c "import app.models, app.core.db; print('ok', app.core.db.Base)"`
Expected: `ok <class 'app.core.db.Base'>`

- [x] **Step 6: Verify the contract is untouched**

Run: `uv run python tests/smoke/route_inventory.py`
Expected: `OK: 132 operations match baseline`

---

### Task 4: Move cross-cutting modules into `core/` ✅ COMPLETE

**Files:**
- Create: `src/app/core/{constants.py,logging.py,errors.py,enums.py,redis.py,sanitize.py,utils.py,pagination.py}`
- Create: `src/app/core/assets/` (from `src/config/assets/`, plus `llm_model_pricing.json`)
- Modify (shim): `src/config/{constants.py,log_helper.py}`, `src/db/{enums.py,db_utils.py}`, `src/resources/exceptions.py`, `src/core/common/utils.py`, `src/core/integrations/redis_utils.py`

**Interfaces:**
- Produces: `app.core.constants` (every name previously in `src/config/constants.py`), `app.core.logging.setup_logging`, `app.core.errors.http_exception_handler`, `app.core.errors.validation_exception_handler`, `app.core.enums.*`, `app.core.redis.get_redis_instance`, `app.core.sanitize.{sanitize_string,sanitize_json,sanitize_content_field,sanitize_subsections_field,sanitize_card_data}`, `app.core.pagination.{Page,MAX_PAGE_SIZE}`.

- [x] **Step 1: Move the files with `git mv`, preserving history**

```bash
git mv src/config/constants.py    src/app/core/constants.py
git mv src/config/log_helper.py   src/app/core/logging.py
git mv src/db/enums.py            src/app/core/enums.py
git mv src/resources/exceptions.py src/app/core/errors.py
git mv src/core/common/utils.py   src/app/core/utils.py
git mv src/core/integrations/redis_utils.py src/app/core/redis.py
git mv src/config/assets          src/app/core/assets
git mv src/config/llm_model_pricing.json src/app/core/assets/llm_model_pricing.json
```

- [x] **Step 2: Split `src/db/db_utils.py`**

The engine and `async_session_scope` are already represented in `app/core/db.py` from Task 3. Move only the five `sanitize_*` functions, verbatim, into `src/app/core/sanitize.py`, and add `async_session_scope` to `app/core/db.py` unchanged apart from importing `SessionFactory` locally.

- [x] **Step 3: Rewrite imports inside the moved files**

Within `src/app/`, every `from src.X` becomes `from app.Y` per spec §3.6. Mechanical pass:

```bash
grep -rln "from src\.\|import src\." src/app/ | while read -r f; do
  sed -i \
    -e 's/from src\.config\.constants/from app.core.constants/g' \
    -e 's/from src\.config\.log_helper/from app.core.logging/g' \
    -e 's/from src\.db\.enums/from app.core.enums/g' \
    -e 's/from src\.core\.common\.utils/from app.core.utils/g' \
    -e 's/from src\.core\.integrations\.redis_utils/from app.core.redis/g' \
    -e 's/from src\.db\.db_utils/from app.core.db/g' \
    "$f"
done
```

- [x] **Step 4: Add `src/app/core/pagination.py`** (new, per R-FA-11)

```python
"""Server-enforced pagination bounds (R-FA-11, R-SEC-3).

Not yet wired into any endpoint. Existing list endpoints keep their current
behaviour; adopting this is a logic change and is deferred.
"""

from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 100


class Page(BaseModel):
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
```

- [x] **Step 5: Leave re-export shims in the old locations**

Each old path becomes a one-line re-export so the ~60 modules still importing it keep working:

```python
# src/config/constants.py
# TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)
from app.core.constants import *  # noqa: F401,F403
```

Apply the same to `src/config/log_helper.py` (→ `app.core.logging`), `src/db/enums.py` (→ `app.core.enums`), `src/resources/exceptions.py` (→ `app.core.errors`), `src/core/common/utils.py` (→ `app.core.utils`), `src/core/integrations/redis_utils.py` (→ `app.core.redis`), and `src/db/db_utils.py` (→ `app.core.db` and `app.core.sanitize`).

`import *` is acceptable here **only** because these modules are deleted in Phase 3; do not use the pattern in new code.

- [x] **Step 6: Verify**

Run: `uv run python -c "import main" && uv run python tests/smoke/route_inventory.py`
Expected: import succeeds, `OK: 132 operations match baseline`.

---

### Task 5: Move outbound clients into `adapters/` ✅ COMPLETE

**Files:**
- Create: `src/app/adapters/{s3.py,email.py,openai_files.py,cloudwatch.py,grep_service.py,ask_caspr_service.py}`
- Modify (shim): the corresponding `src/core/integrations/*` and `src/config/cloudwatch_helper.py`

**Interfaces:**
- Produces: `app.adapters.cloudwatch.CloudwatchInstance`, `app.adapters.s3.*`, `app.adapters.email.{send_report_notification_email,send_password_reset_email,send_signup_verification_email,send_subscribe_confirmation_email,send_request_confirmation_email,send_book_call_email}`, `app.adapters.openai_files.ensure_report_file_id`, `app.adapters.grep_service.get_or_create_grep_session`.

- [x] **Step 1: Move**

```bash
git mv src/core/integrations/s3_utils.py         src/app/adapters/s3.py
git mv src/core/integrations/email_utils.py      src/app/adapters/email.py
git mv src/core/integrations/openai_file_utils.py src/app/adapters/openai_files.py
git mv src/config/cloudwatch_helper.py           src/app/adapters/cloudwatch.py
git mv src/core/grep_agent_2                     src/app/adapters/grep_agent_2
git mv src/core/ask_caspr                        src/app/adapters/ask_caspr
```

`cloudwatch_helper.py` (the `CloudwatchInstance` AWS client) and `observability/cloudwatch_utils.py` (app-level instrumentation that queries the database) are **separate modules and must not be merged** — merging them would be a logic change. `cloudwatch_utils.py` moves in Task 6.

- [x] **Step 2: Rewrite imports** using the same sed pass as Task 4 Step 3, extended with the new mappings.

- [x] **Step 3: Leave re-export shims** at each old path, carrying the standard `TEMPORARY` comment.

- [x] **Step 4: Verify**

Run: `uv run python -c "import main" && uv run python tests/smoke/route_inventory.py`
Expected: `OK: 132 operations match baseline`.

---

### Task 6: Move instrumentation into `observability/` ✅ COMPLETE

**Files:**
- Create: `src/app/observability/{cloudwatch_utils.py,error_alerter.py,functionality_context.py,llm_cost_calculator.py,llm_response_logger.py,web_search_analytics.py}`
- Modify (shim): `src/core/observability/*`

**Interfaces:**
- Produces: `app.observability.error_alerter.{ErrorAlertManager,capture_exception_for_alerter,_init_request_context,_consume_alert_traceback,_was_alert_already_queued,set_alert_request_context}`, `app.observability.cloudwatch_utils.{insert_cloudwatch_logs,insert_cloudwatch_logs_for_caspr_page}`, `app.observability.functionality_context.{Functionality,set_functionality}`, `app.observability.web_search_analytics.{enrich_terminal_search_analytics,log_scheduled_analytics_batch,search_analytics_schedule_kwargs}`.

`observability/` is a **top-level package, a sibling of `core/`, not a subpackage of it** (spec §3.5). Five of these six modules import from `src/db/`, and R-LAYER-1 forbids `core/*` from importing a bounded context. Placing them outside `core/` is what keeps `core/` compliant without a signature change.

- [x] **Step 1: Move**

```bash
git mv src/core/observability/cloudwatch_utils.py     src/app/observability/cloudwatch_utils.py
git mv src/core/observability/error_alerter.py        src/app/observability/error_alerter.py
git mv src/core/observability/functionality_context.py src/app/observability/functionality_context.py
git mv src/core/observability/llm_cost_calculator.py  src/app/observability/llm_cost_calculator.py
git mv src/core/observability/llm_response_logger.py  src/app/observability/llm_response_logger.py
git mv src/core/observability/web_search_analytics.py src/app/observability/web_search_analytics.py
```

- [x] **Step 2: Rewrite imports; leave shims; verify** as in Task 5 Steps 2–4.

- [x] **Step 3: Assert `core/` purity**

Run: `grep -rn "from app\.\(auth\|chats\|reports\|cards\|deliverables\|leads\|dashboard\|wallet\|billing\|referrals\|onboarding\|admin\|internal\)" src/app/core/`
Expected: **no output.** Any hit is an R-LAYER-1 violation and must be relocated, not suppressed.

---

### Task 7: Move LLM orchestration into `research/` ✅ COMPLETE

**Files:**
- Create: `src/app/research/{agent,domains,prompts,refine,visualization,infographics}/`
- Modify (shim): `src/core/{agent,domains,prompts,refine,visualization,infographics}/`

- [x] **Step 1: Move**

```bash
for pkg in agent domains prompts refine visualization infographics; do
  git mv "src/core/$pkg" "src/app/research/$pkg"
done
```

- [x] **Step 2: Rewrite imports; leave shims; verify** as in Task 5 Steps 2–4.

`research/` is a top-level package, not a bounded context — it has no HTTP surface and no models. It may import context repositories; `core/` may not.

---

### Task 8: Application factory and lifespan ✅ COMPLETE

**Files:**
- Create: `src/app/main.py`, `src/app/api.py`, `src/app/core/middleware.py`
- Modify: `main.py` (reduced to a shim), `Dockerfile`, `../docker-compose.yml` (command only)
- Modify: `db_migrations/env.py`

**Interfaces:**
- Produces: `app.main.create_app() -> FastAPI`, `app.api.api_router: APIRouter`, `app.core.middleware.error_alert_middleware`.

This is spec §4.2. The three module-scope side effects in the current `main.py` — `get_redis_instance()`, `ErrorAlertManager(...)`, and the engine built at import time inside `db_utils.py` — move into `lifespan`. That removal is what makes gate 2 (import smoke) possible at all; today importing the app opens a Redis client and a database engine.

- [x] **Step 1: Write `src/app/api.py`**

```python
"""Top-level router. Contexts are mounted here, nothing else."""

from fastapi import APIRouter

from src.resources.routers import (  # TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)
    admin_api,
    api as legacy_api,
    internal_db_api,
    onboarding_api,
    wallet_api,
)

api_router = APIRouter()
api_router.include_router(legacy_api.router, prefix="/api/v1", tags=["Chat"])
api_router.include_router(wallet_api.router, prefix="/api/v1", tags=["Wallet"])
api_router.include_router(onboarding_api.router, prefix="/api/v1", tags=["Onboarding"])
api_router.include_router(admin_api.router, prefix="/api/v1", tags=["Admin Dashboard"])
api_router.include_router(internal_db_api.router, tags=["Internal DB (service-to-service)"])
```

Each context task in Phase 2 replaces one of these legacy includes with the context's own router.

- [x] **Step 2: Move the error-alert middleware verbatim** from `main.py` into `src/app/core/middleware.py`, preserving its body exactly. It stays a `BaseHTTPMiddleware`-style function; converting it to pure ASGI (R-FA-7) is deferred per spec §5.

- [x] **Step 3: Write `src/app/main.py`**

```python
"""Application factory and lifespan (R-FA-1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401  # registers every ORM mapper — spec §3.2
from app.adapters.cloudwatch import CloudwatchInstance
from app.api import api_router
from app.core.constants import ALLOWED_ORIGINS, DB_CONNECTION_LINK, ENVIRONMENT
from app.core.db import build_engine
from app.core.errors import http_exception_handler
from app.core.logging import setup_logging
from app.core.middleware import error_alert_middleware
from app.core.redis import get_redis_instance
from app.observability.error_alerter import ErrorAlertManager

logger = setup_logging(__file__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up...")
    fastapi_app.state.engine = build_engine(DB_CONNECTION_LINK)
    fastapi_app.state.redis = get_redis_instance()
    fastapi_app.state.error_alerter = ErrorAlertManager(
        redis_client=fastapi_app.state.redis.redis_client
    )
    await CloudwatchInstance.connect()
    logger.info("Cloudwatch connected...")
    digest_task = asyncio.create_task(fastapi_app.state.error_alerter.start_digest_loop())
    logger.info("Error digest loop started...")
    try:
        yield
    finally:
        logger.info("Shutting down...")
        digest_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass
        await CloudwatchInstance.close()
        await fastapi_app.state.engine.dispose()
        logger.info("Cloudwatch closed...")


def create_app() -> FastAPI:
    is_production = ENVIRONMENT == "PROD"
    fastapi_app = FastAPI(
        title="Casper API",
        description="Casper - Report AI Search Assistant",
        version="1.0.0",
        lifespan=lifespan,
        # R-FA-9: /openapi.json is a complete map of the attack surface.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    fastapi_app.add_exception_handler(HTTPException, http_exception_handler)
    fastapi_app.add_exception_handler(RequestValidationError, _validation_handler)

    logger.info(f"Configuring CORS for {ENVIRONMENT} environment with origins: {ALLOWED_ORIGINS}")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "Origin",
            "X-Requested-With",
            "X-API-Key",
        ],
    )
    fastapi_app.middleware("http")(error_alert_middleware)

    fastapi_app.include_router(api_router)
    _register_health(fastapi_app)
    return fastapi_app
```

`_validation_handler` is the existing `validation_exception_handler` body from `main.py`, moved verbatim into `app/core/errors.py`. `_register_health` is written in Task 9.

- [x] **Step 4: Reduce the root `main.py` to a shim**

```python
# TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)
from app.main import create_app

app = create_app()
```

Keeping `main:app` working means the Dockerfile, compose file, and any operator muscle memory do not break mid-migration.

- [x] **Step 5: Retarget `db_migrations/env.py`**

Replace its `from src.db.database import Base` with:

```python
import app.models  # noqa: F401  # registers every mapper before autogenerate
from app.core.db import Base

target_metadata = Base.metadata
```

- [x] **Step 6: Verify the factory boots and the contract holds**

Run: `uv run python -c "from app.main import create_app; a = create_app(); print('ok')" && uv run python tests/smoke/route_inventory.py`
Expected: `ok`, then `OK: 132 operations match baseline`.

`route_inventory._load_app()` prefers `app.main.create_app` and will now exercise the factory path rather than `main.app`.

---

### Task 9: Health endpoints and gates 2 and 3 ✅ COMPLETE

**Files:**
- Modify: `src/app/main.py`
- Create: `tests/smoke/test_import_smoke.py`, `tests/smoke/test_mappers.py`
- Modify: `tests/smoke/baseline_routes.json` (re-baselined once, deliberately)

**Interfaces:**
- Produces: `GET /health/live`, `GET /health/ready`, and the retained `GET /api/v1/health`.

- [x] **Step 1: Add `_register_health` to `src/app/main.py`**

```python
def _register_health(fastapi_app: FastAPI) -> None:
    """R-FA-10: liveness must not touch anything external.

    If liveness checked the database, a 30-second blip would make the
    orchestrator restart-loop every healthy pod.
    """

    @fastapi_app.get("/health/live", tags=["Health"])
    async def health_live() -> dict[str, str]:
        return {"status": "healthy"}

    @fastapi_app.get("/health/ready", tags=["Health"])
    async def health_ready(request: Request) -> dict[str, object]:
        checks: dict[str, str] = {}
        try:
            async with request.app.state.engine.connect() as conn:
                await asyncio.wait_for(conn.exec_driver_sql("SELECT 1"), timeout=3)
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "unreachable"
        try:
            await asyncio.wait_for(request.app.state.redis.redis_client.ping(), timeout=3)
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unreachable"
        ready = all(v == "ok" for v in checks.values())
        return {"status": "ready" if ready else "degraded", "checks": checks}

    # Deprecated alias. render-report probes this for readiness and has a test
    # asserting it; remove only after that service migrates. Spec §4.4.
    @fastapi_app.get("/api/v1/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        return {"status": "healthy"}
```

- [x] **Step 2: Confirm the inventory reports exactly the two additions**

Run: `uv run python tests/smoke/route_inventory.py`
Expected: FAIL, listing precisely `ADDED GET /health/live` and `ADDED GET /health/ready`, and nothing else. **If any `REMOVED` or `CHANGED` line appears, stop and fix — this is the only point in the plan where a diff is expected, and it must contain only those two lines.**

- [x] **Step 3: Re-baseline**

Run: `uv run python tests/smoke/route_inventory.py --write-baseline`
Expected: `baseline written: 134 operations`. Every subsequent task compares against 134.

- [x] **Step 4: Write gate 2, the import smoke test**

```python
# tests/smoke/test_import_smoke.py
"""Gate 2 (spec §7): every module under app/ imports cleanly.

Catches circular imports introduced by the context split and names a shim
failed to re-export. Only possible because Task 8 moved the Redis client and
database engine out of module scope and into lifespan.
"""

import importlib
import pkgutil

import pytest

import app


@pytest.mark.smoke
def test_every_module_imports() -> None:
    failures: list[str] = []
    for info in pkgutil.walk_packages(app.__path__, prefix="app."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - reporting every failure at once
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)
```

- [x] **Step 5: Write gate 3, the mapper configuration test**

```python
# tests/smoke/test_mappers.py
"""Gate 3 (spec §7): every cross-package relationship() resolves.

Splitting 34 models across context packages breaks string-form relationships
if a class is missing from the app.models aggregator. Without this test that
failure surfaces at first query in production, not at import.
"""

import pytest
from sqlalchemy.orm import configure_mappers

import app.models


@pytest.mark.smoke
def test_all_mappers_configure() -> None:
    configure_mappers()


EXPECTED_MODEL_COUNT = 34


@pytest.mark.smoke
def test_aggregator_exports_every_model() -> None:
    """Counted off the module namespace, not off a Base registry.

    During Phase 2 models live under two different declarative bases at once
    — the legacy ``declarative_base()`` in ``src/db/database.py`` and the new
    ``app.core.db.Base`` — so asserting against either registry alone would
    fail at every intermediate point. Any class carrying ``__tablename__``
    counts, regardless of which base it currently inherits from.
    """
    exported = [
        name
        for name in dir(app.models)
        if not name.startswith("_") and hasattr(getattr(app.models, name), "__tablename__")
    ]
    assert len(exported) == EXPECTED_MODEL_COUNT, (
        f"expected {EXPECTED_MODEL_COUNT} mapped classes, found {len(exported)}: {sorted(exported)}"
    )
```

- [x] **Step 6: Run all three gates**

Run: `uv run pytest -m smoke -v`
Expected: 4 passed.

---

### Task 10: Production Dockerfile ✅ COMPLETE

**Files:**
- Modify: `Dockerfile`
- Modify: `../docker-compose.yml` (the `caspr-core` service command only)

- [x] **Step 1: Rewrite `Dockerfile` as multi-stage (R-TOOL-5)**

```dockerfile
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.14-slim AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so editing source does not invalidate the slow layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim AS runtime
# WeasyPrint runtime libraries: app/adapters/openai_files.py imports weasyprint
# at module scope. Pango is all WeasyPrint 69 needs.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libffi-dev \
        shared-mime-info curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --chown=app:app src /app/src
COPY --chown=app:app db_migrations /app/db_migrations
COPY --chown=app:app alembic.ini /app/alembic.ini
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

The runtime stage contains no `uv`, copies only `.venv` plus source, and runs as a non-root user.

- [x] **Step 2: Verify the image builds and boots**

```bash
docker build -t caspr-core:restructure .
docker run --rm -d --name caspr-smoke -p 8001:8000 --env-file .env caspr-core:restructure
sleep 10
curl -sf localhost:8001/health/live && curl -sf localhost:8001/api/v1/health
docker rm -f caspr-smoke
```

Expected: both return `{"status":"healthy"}`.

- [x] **Step 3: Confirm no `uv` in the runtime image**

Run: `docker run --rm caspr-core:restructure sh -c "command -v uv || echo ABSENT"`
Expected: `ABSENT`.

---

## Phase 2 — Context migration

### The context migration procedure

Every context task below follows these seven steps. They are written out once here; each task supplies its own exact inventory. Do not deviate from the order — models before repository before router is what keeps the aggregator valid at every intermediate point.

1. **Models.** Cut the context's ORM classes out of `src/db/database.py` into `src/app/<context>/models.py`, verbatim, changing only `Base` to import from `app.core.db`. Add `from src.db.database import <names>` to the *shim* list and switch the corresponding line in `src/app/models.py` to import from the new location.
2. **Repository.** Cut the listed functions out of `src/db/async_db_functions.py` (and the context's `*_db.py`) into `src/app/<context>/repository.py`, verbatim. Add a re-export line at the top of the old module.
3. **Schemas.** Move the context's classes from `src/resources/schemas/*.py` into `src/app/<context>/schemas.py`.
4. **Dependencies.** Create `src/app/<context>/dependencies.py` with the `Annotated` aliases replacing this context's inline `Depends(...)` calls (R-FA-2).
5. **Router.** Cut the route handlers into the listed router files, verbatim. Replace the legacy include in `src/app/api.py` with the new router. Where a handler holds business logic inline, extract it to `service.py` **only by moving the block unchanged** — no rewriting.
6. **Shim.** Confirm the old module still re-exports everything it used to expose, with the `TEMPORARY` comment.
7. **Gate.** Run `uv run pytest -m smoke -v` and `uv run python tests/smoke/route_inventory.py`. Both must be clean before starting the next context. A `REMOVED` or `CHANGED` line means a route was lost in the split — fix before proceeding.

Order is simplest-first, so the shim mechanism is proven on `auth` before the 42-route `internal` context relies on it.

---

### Task 11: `auth` context ✅ COMPLETE

**Files:**
- Create: `src/app/auth/{models.py,repository.py,schemas.py,token.py,dependencies.py,router_session.py,router_password.py,router_verification.py,router_profile.py}`
- Modify: `src/db/database.py`, `src/db/async_db_functions.py`, `src/resources/routers/api.py`, `src/resources/schemas/auth.py`, `src/core/auth/token_auth.py`, `src/app/api.py`, `src/app/models.py`

**Interfaces:**
- Consumes: `app.core.db.SessionDep`, `app.core.db.async_session_scope`, `app.core.constants.*`.
- Produces: `app.auth.models.User`; `app.auth.dependencies.CurrentUserDep`; repository functions `get_user_details`, `check_user_by_id`, `verify_user_credentials`, `create_user`, `check_user_exists_by_email`, `check_user_exists_by_email_or_phone`, `update_user_password`, `update_user_verification_status`, `update_user_tc_verified`, `get_user_walkover_status`, `mark_walkover_completed`.

**Inventory:**

- **Models:** `User` (`src/db/database.py:99-186`).
- **Repository functions** from `src/db/async_db_functions.py`: `get_user_details` (L106), `check_user_by_id` (L410), `verify_user_credentials` (L478), `create_user` (L594), `check_user_exists_by_email_or_phone` (L645), `check_user_exists_by_email` (L945), `update_user_password` (L1010), `update_user_verification_status` (L1072), `update_user_tc_verified` (L1133), `get_user_walkover_status` (L1210), `mark_walkover_completed` (L1272).
- **Schemas** from `src/resources/schemas/auth.py`: `SignupRequest`, `SignupResponse`, `LoginRequest`, `LoginResponse`, `GoogleLoginRequest`, `GoogleLoginResponse`, `LogoutRequest`, `LogoutResponse`, `TokenData`, `ForgotPasswordRequest`, `ForgotPasswordResponse`, `VerifyResetTokenResponse`, `ResetPasswordRequest`, `ResetPasswordResponse`, `SendVerificationLinkRequest`, `SendVerificationLinkResponse`, `VerifyAccountTokenRequest`, `VerifyAccountTokenResponse`, `UpdateTCVerifiedRequest`, `UpdateTCVerifiedResponse`, `GetTCVerifiedResponse`, `GetUserTokensResponse`, `GetWalkoverStatusResponse`, `CompleteWalkoverResponse`.
  `ErrorResponse` stays in a shared location — move it to `src/app/core/errors.py` and import it from every context, since all 13 reference it in `responses=`.
- **Token helpers** from `src/core/auth/token_auth.py` → `src/app/auth/token.py`, verbatim: `create_access_token`, `create_reset_password_token`, `create_verification_token`, `create_refresh_token`, `verify_token`, `get_current_active_user`.

  This refines spec §3.6, which maps `token_auth.py` wholesale onto
  `dependencies.py`. Five of its six functions mint or verify tokens and are
  not FastAPI dependencies at all; only `get_current_active_user` is. Keeping
  `dependencies.py` to the DI aliases is what R-CORE-2 (S) asks for — the file
  changes when the wiring changes, not when the token format does.
- **Routers:**
  - `router_session.py` — `POST /signup` (api.py:240), `POST /login` (570), `POST /logout` (734), `POST /google-login` (6095)
  - `router_password.py` — `POST /forgot-password` (6357), `GET /verify-reset-token` (6510), `POST /reset-password` (6613)
  - `router_verification.py` — `POST /send-verification-link` (6774), `POST /verify-account-token` (6903), `GET /get-signup-token` (915)
  - `router_profile.py` — `PATCH /update-tc-verified` (802), `GET /get-tc-verified` (870), `GET /get-walkover-status` (~940), `POST /complete-walkover` (972)

- [x] **Step 1: Move models** per procedure step 1.
- [x] **Step 2: Move repository functions** per procedure step 2.
- [x] **Step 3: Move schemas** per procedure step 3.
- [x] **Step 4: Write `dependencies.py`**

```python
"""FastAPI dependencies for the auth context (R-FA-2)."""

from typing import Annotated

from fastapi import Depends

from app.auth.token import get_current_active_user

CurrentUserDep = Annotated[str, Depends(get_current_active_user)]
```

- [x] **Step 5: Move routers** per procedure step 5, and replace the `legacy_api` include in `src/app/api.py` with the four new routers, each mounted at `prefix="/api/v1"` with `tags=["Auth"]`.
- [x] **Step 6: Confirm shims** per procedure step 6.
- [x] **Step 7: Gate**

Run: `uv run pytest -m smoke -v && uv run python tests/smoke/route_inventory.py`
Expected: 4 passed; `OK: 134 operations match baseline`.

---

### Tasks 12–23: remaining contexts

Each follows the identical seven-step procedure. Inventories:

| # | Context | Models (from `database.py`) | Repository functions (from `async_db_functions.py` unless noted) | Schemas source | Routers |
|---|---|---|---|---|---|
| 12 | `onboarding` | `UserRole`, `ResearchInterest`, `University`, `UserResearchInterest` | all of `src/db/onboarding_db.py` | `schemas/onboarding.py` | `router.py` (8 routes from `onboarding_api.py`) |
| 13 | `leads` | `Subscriber`, `Request`, `CallBooking` | `create_subscriber` (4418), `create_request` (4474), `create_call_booking` (4549) | `schemas/chat.py` (subset) | `router.py` — `POST /subscribe` (1082), `POST /request` (1154), `POST /book-call` (1246), `GET /live-sources` (1339) |
| 14 | `referrals` | `Referral` | `generate_unique_referral_code` (76); all of `src/db/referral_functions.py` | `schemas/referral.py` | `router.py` — `GET /referral` |
| 15 | `dashboard` | — | `get_dashboard_stats` (4770), `list_all_categories` (5077), `_latest_report_per_chat_subq` (5098), `search_chats_and_reports` (5139) | `schemas/dashboard.py` | `router.py` — `GET /dashboard-stats`, `GET /dashboard-info`, `GET /categories`, `GET /home-search`, `POST /user-logs` (6036) |
| 16 | `billing` | `Subscription`, `SubscriptionInterval` | subscription half of `src/db/wallet_functions.py`; `src/services/subscription_update_validator.py` → `service_validation.py` | `schemas/subscription.py` | `router.py` — 7 subscription routes from `wallet_api.py` |
| 17 | `wallet` | `Wallet`, `TokenBatch`, `TokenTransaction` | wallet half of `src/db/wallet_functions.py`; `src/services/wallet_service.py` → `service.py` (split by role group, R-STRUCT-3); `src/services/payment_service.py` → `service_payment.py` | `schemas/wallet.py` | `router.py` — 6 wallet routes from `wallet_api.py` |
| 18 | `chats` | `Message`, `AskCasprChat` | `get_user_chat` (180), `insert_chats` (265), `get_user_chats` (707), `rename_chat` (3992), `mark_chat_deleted` (4048), `get_draft_chats_grouped` (5296), `get_latest_ask_caspr_version` (6019), `insert_ask_caspr_chat_entry` (6066) | `schemas/chat.py` | `router_sessions.py`, `router_temp.py`, `router_management.py` |
| 19 | `reports` | `Report`, `ReportVersion`, `ReportVersionCard`, `Publish` | `get_chat_reports` (798), `get_file_s3_path` (881), `create_report` (1338), `update_report` (1431), `update_specific_version_s3_uri` (1627), `update_report_status_by_chat_or_report_id` (1693), `update_report_status_if_needed` (1746), `get_report_details` (1803), `verify_report_ownership` (1872), `link_card_to_report_version` (2200), `finalize_report_version` (2240), `create_new_report_version` (2355), `detect_modified_cards` (2478), `get_active_report_version` (2675), `get_chat_reports_and_cards` (2926), `insert_publish_details` (3033), `get_all_version_outputs_list` (5378), `get_version_file_for_download` (5480), `get_report_version_history_data` (5549), `get_report_info_by_version` (5801), `get_report_domain_summary` (4867), `get_reports_by_domain` (4928), `_to_external_domain` (5060), `_to_internal_domain` (5070) | `schemas/chat.py` (subset) | `router.py`, `router_versions.py`, `router_domains.py` |
| 20 | `cards` | `Card`, `CardVersion`, `Table`, `RefinementHistory` | `insert_table` (1933), `insert_card` (2054), `get_tables_for_card` (2742), `get_report_cards` (2816), `get_table_id_markdown_map` (3131), `get_report_in_cards_format` (3271), `insert_card_version` (3361), `refine_card_in_db` (3421), `insert_tables_from_updated_card` (3560), `delete_card_or_subsection` (3714), `delete_visualization` (4112), `revert_card_to_version` (4295), `get_latest_card_version` (4727), `get_cards_for_version` (5708), `get_refinement_history` (5934), `update_refinement_history` (5974), `_normalize_table_title_value` (33), `_normalize_tables_for_response` (44), `_normalize_subsections_for_response` (60); plus `src/core/cards/*` → `service_*.py` | `schemas/chat.py` (subset) | `router_refine.py`, `router_visualization.py`, `router_summary.py` |
| 21 | `deliverables` | — | — | `schemas/chat.py` (subset) | `router.py` — `POST /generate-presentation` (7078), `POST /generate-infographic` (7643); `src/core/report_util/*` → `service_*.py` |
| 22 | `admin` | `CostTracker`, `WebSearchEvent`, `WebSearchCitation`, `WebSearchRawResponse` | `insert_cost_tracker` (4623); all of `src/db/admin_db.py`; `src/db/web_search_db.py` → `repository_web_search.py` | `schemas/dashboard.py` (subset) | `router.py`, `router_costs.py` (16 routes from `admin_api.py`); `src/core/auth/admin_auth.py` → `dependencies.py` |
| 23 | `internal` | `UserVectorStore`, `UploadedFile`, `UploadedFileChunk`, `FileVersion`, `VectorStoreFile`, `ChatFile` | `src/db/grep_db.py` + `src/db/upload_db.py` → `repository_grep.py`; `src/db/ask_caspr_db.py` → `repository_ask_caspr.py` | inline | `router_grep.py`, `router_ask_caspr.py`, `router_shared.py` (42 routes from `internal_db_api.py`); `src/core/common/internal_auth.py` → `dependencies.py` |

- [x] **Task 12: `onboarding`** — seven-step procedure, inventory above, then gate.
- [x] **Task 13: `leads`** — seven-step procedure, inventory above, then gate.
- [x] **Task 14: `referrals`** — seven-step procedure, inventory above, then gate.
- [x] **Task 15: `dashboard`** — seven-step procedure, inventory above, then gate.
- [x] **Task 16: `billing`** — seven-step procedure, inventory above, then gate.
- [x] **Task 17: `wallet`** — seven-step procedure, inventory above, then gate. `wallet_service.py` is 3,169 lines and must be split into role-group files under 400 lines each (R-STRUCT-3); split at existing function boundaries only.
- [x] **Task 18: `chats`** — seven-step procedure, inventory above, then gate.
- [x] **Task 19: `reports`** — seven-step procedure, inventory above, then gate.
- [x] **Task 20: `cards`** — seven-step procedure, inventory above, then gate. `card_utils.py` is 4,159 lines; split at function boundaries into `service_cards.py`, `service_citations.py`, `service_fixer.py`.
- [x] **Task 21: `deliverables`** — seven-step procedure, inventory above, then gate.
- [x] **Task 22: `admin`** — seven-step procedure, inventory above, then gate.
- [x] **Task 23: `internal`** — seven-step procedure, inventory above, then gate. Largest context at 42 routes; migrate `router_grep.py` first and gate before continuing to the other two.

---

## Phase 3 — Shim removal

### Task 24: Delete the old tree ✅ COMPLETE

**Files:**
- Delete: `src/config/`, `src/core/`, `src/db/`, `src/resources/`, `src/services/`, root `main.py`
- Modify: `Dockerfile`, `../docker-compose.yml` if either still references `main:app`

- [x] **Step 1: Confirm nothing outside the old tree still imports it**

Run: `grep -rn "from src\.\|import src\." src/app/ tests/ db_migrations/ | grep -v "TEMPORARY"`
Expected: no output. Any hit means a context migration left a dangling import — fix before deleting.

- [x] **Step 2: Delete**

```bash
git rm -r src/config src/core src/db src/resources src/services main.py
```

- [x] **Step 3: Confirm no shim survived**

Run: `grep -rn "TEMPORARY: removed in Phase 3" src/ tests/ db_migrations/`
Expected: no output.

- [x] **Step 4: Gate**

Run: `uv run pytest -m smoke -v && uv run python tests/smoke/route_inventory.py`
Expected: 4 passed; `OK: 134 operations match baseline`.

---

## Phase 4 — Final verification

### Task 25: Full gate run ✅ COMPLETE

- [x] **Step 1: Static gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

**Recorded baseline (measured at Task 2, against the pre-migration tree): 6,429 ruff findings, 5,409 auto-fixable.** The distribution is what makes the deferral safe:

| Count | Rule | Nature |
|---|---|---|
| 3,268 | `W293` blank-line-with-whitespace | formatting |
| 638 | `UP045` non-pep604-annotation-optional | annotation modernisation |
| 571 | `UP006` non-pep585-annotation | annotation modernisation |
| 465 | `RUF010` explicit-f-string-type-conversion | formatting |
| 295 | `W291` trailing-whitespace | formatting |
| 139 | `F401` unused-import | **needs care — see below** |

Roughly 3,600 are whitespace and ~1,400 are annotation modernisation; `ruff format` and `ruff check --fix` resolve those and neither is a logic change. They were deliberately **not** fixed at Task 2, because doing so across the pre-migration tree would produce a ~60,000-line diff that buries the restructure itself. Running the sweep here, after every file has reached its final home, produces the same end state as a reviewable diff.

Order matters:

```bash
uv run ruff format .
uv run ruff check --fix .          # safe fixes only; never --unsafe-fixes
uv run ruff check .                # what remains needs judgement
```

**`F401` requires care.** 139 unused imports exist, and during Phase 2 the re-export shims are *deliberately* unused imports. This sweep runs in Task 25, after Phase 3 has deleted every shim, so no `--fix` can strip a live re-export. If any shim still exists when this runs, stop — Task 24 was not completed.

Whatever survives must be either fixed or added to the `per-file-ignores` / mypy overrides debt register with a one-line reason — never suppressed silently.

- [x] **Step 2: Test suite**

Run: `uv run pytest -v`
Expected: all smoke gates pass and `tests/test_refine_persist.py` still passes.

- [x] **Step 3: Route inventory**

Run: `uv run python tests/smoke/route_inventory.py`
Expected: `OK: 134 operations match baseline`.

- [x] **Step 4: Container boot (gate 4)**

```bash
docker build -t caspr-core:restructure .
docker run --rm -d --name caspr-final -p 8001:8000 --env-file .env caspr-core:restructure
sleep 10
curl -sf localhost:8001/health/live
curl -sf localhost:8001/health/ready
curl -sf localhost:8001/api/v1/health
docker rm -f caspr-final
```

Expected: `/health/live` and `/api/v1/health` return `{"status":"healthy"}`; `/health/ready` returns its checks object.

- [x] **Step 5: R-STRUCT-3 report**

Run: `find src/app -name "*.py" | xargs wc -l | sort -rn | awk '$1 > 400'`
Expected: a short list. Every remaining file over 400 lines needs a one-line deviation note appended to the spec's Section 5, per the standard's "deviations require a written note" clause.

- [x] **Step 6: R-LAYER-1 report**

```bash
grep -rn "^from app\.\(auth\|chats\|reports\|cards\|deliverables\|leads\|dashboard\|wallet\|billing\|referrals\|onboarding\|admin\|internal\)" src/app/core/
grep -rn "import sqlalchemy\|from sqlalchemy" src/app/*/router*.py
```

Expected: no output from either. The first proves `core/` imports no bounded context; the second proves no router touches the ORM.

- [x] **Step 7: Report results**

Report the actual output of every gate, including anything that failed. Do not report completion unless steps 1–6 are green or their deviations are written into the spec.

---

## Notes for the executor

- **Do not commit.** Leave everything staged or unstaged for review.
- **Verbatim means verbatim.** If a moved function looks wrong, note it in the final report — do not fix it. Logic changes are out of scope and a "small improvement" invalidates the premise that the route inventory gate is sufficient verification.
- **The route inventory is the safety net.** Run it after every task, not only at the end. Catching a lost route one context after it happened is cheap; catching it at Task 25 is not.
- **If a task reveals hidden complexity** — a circular import that cannot be broken by moving a file, a model that genuinely belongs to two contexts — stop and report rather than improvising a redesign.
