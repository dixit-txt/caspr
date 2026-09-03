# caspr-core — Rule Book Alignment (Structural)

**Date:** 2026-09-03
**Service:** `caspr-core`
**Governing document:** `../../../../python-backend-rules.md` (103 rules, 68 marked `[P0]`)
**Status:** Implemented. See section 10 for deviations recorded at completion.

---

## 1. Purpose

Restructure `caspr-core` to satisfy the repository layout, layering, and tooling
rules of the Python Backend Engineering Standard, **without changing any
business logic**. Function bodies move verbatim. No behaviour visible to a
client of this service changes.

This is deliberately a partial adoption. Section 4 lists what is in scope and
Section 5 lists what is explicitly deferred, with the rule IDs each defers.
Deferring a `[P0]` rule requires a written note under the standard's own
"deviations require a written note" clause; Section 5 is that note.

## 2. Current state

An audit against all 103 rules found the following.

**Already compliant:** `uv` is the package manager with a committed `uv.lock`
(R-UV-1, R-UV-2); `[dependency-groups]` uses the PEP 735 table (R-UV-4);
`.python-version` (3.14.5) agrees with `requires-python` (R-UV-5); `.env` is
gitignored (R-CFG-6); the session factory sets `expire_on_commit=False`
(R-DB-4); migrations do not run on application startup (R-DB-9, R-TOOL-6); and
IDs are already generated with `uuid7` via `uuid-utils`, satisfying the
standard's closing note.

**Non-compliant, addressed by this design:**

| Area | Observed | Rules |
|---|---|---|
| Layout | Layer-first `src/{config,core,resources/{routers,schemas},db,services}`; package root is `src`; `[tool.uv] package = false` | R-STRUCT-1, R-STRUCT-2 |
| File size | `resources/routers/api.py` 10,257 lines carrying ~55 routes; `db/async_db_functions.py` 6,117; `core/cards/card_utils.py` 4,159; `core/agent/model.py` 3,838; `services/wallet_service.py` 3,169; `core/prompts/prompt_utils.py` 2,955; `db/wallet_functions.py` 2,567; `db/database.py` 2,267. About 15 files exceed 400 lines | R-STRUCT-3 |
| Layering | Routers import `async_db_functions` and ORM models directly; no repository layer; a service layer exists only for wallet, payment, and subscription validation | R-LAYER-1, R-LAYER-2, R-LAYER-3 |
| App wiring | Module-level `app = FastAPI(...)` rather than a factory; the async engine is constructed at **import time** in `db/db_utils.py` instead of in `lifespan` on `app.state`; 73 inline `Depends(...)` calls and zero `Annotated` aliases; interactive docs served unconditionally; a single `/api/v1/health` with no liveness/readiness split | R-FA-1, R-FA-2, R-FA-9, R-FA-10, R-DB-3 |

**Non-compliant, explicitly deferred** (detail in Section 5): configuration
(R-CFG-1 to R-CFG-5), error contract (R-ERR-1 to R-ERR-5), logging (R-LOG-1 to
R-LOG-5, R-OBS-1), ORM typing and loader strategy (R-DB-1, R-DB-2), middleware
form (R-FA-7), background work (R-BG-1 to R-BG-12), and test coverage (R-TEST-1
to R-TEST-11 beyond the smoke harness).

## 3. Target architecture

### 3.1 Layout

R-STRUCT-1 mandates a domain-first layout. All four services in this repository
(`caspr-core`, `ask-caspr`, `file-handling`, `render-report`) currently use a
layer-first layout, including `render-report`, which has otherwise adopted
`pydantic-settings`, `structlog`, ruff, and mypy. The decision recorded here is
to follow the rule book: `caspr-core` moves to domain-first and becomes the
reference implementation. The other three services are out of scope for this
work and are expected to diverge until they are migrated separately.

```
caspr-core/
├── pyproject.toml                  # package = true; ruff, mypy, pytest config
├── uv.lock
├── .python-version
├── .pre-commit-config.yaml
├── alembic.ini
├── Dockerfile                      # multi-stage, non-root
├── .github/
│   ├── workflows/ci.yml
│   └── pull_request_template.md
├── db_migrations/                  # location unchanged; env.py retargeted
│   ├── env.py
│   └── versions/
├── src/app/
│   ├── main.py                     # create_app() factory + lifespan
│   ├── api.py                      # top-level APIRouter, mounts /api/v1
│   ├── models.py                   # ORM aggregator — see 3.2
│   │
│   ├── core/                       # imports nothing from a bounded context
│   │   ├── constants.py            # moved verbatim from src/config/constants.py
│   │   ├── db.py                   # Base, NAMING_CONVENTION, engine, SessionDep
│   │   ├── enums.py
│   │   ├── errors.py               # moved from src/resources/exceptions.py
│   │   ├── logging.py              # moved from src/config/log_helper.py
│   │   ├── middleware.py           # error-alert middleware, moved out of main.py
│   │   ├── pagination.py           # new: Page model, MAX_PAGE_SIZE
│   │   ├── redis.py
│   │   ├── sanitize.py             # sanitize_* helpers from db/db_utils.py
│   │   ├── utils.py
│   │   └── assets/                 # fonts, templates, llm_model_pricing.json
│   │
│   ├── adapters/                   # outbound third-party and sibling-service clients
│   │   ├── s3.py                   # from core/integrations/s3_utils.py
│   │   ├── email.py                # from core/integrations/email_utils.py
│   │   ├── openai_files.py         # from core/integrations/openai_file_utils.py
│   │   ├── cloudwatch.py           # from config/cloudwatch_helper.py (AWS client only)
│   │   ├── llm.py                  # Anthropic / OpenAI / Gemini client construction
│   │   ├── opensearch.py
│   │   ├── grep_service.py         # HTTP shim to file-handling
│   │   ├── render_service.py       # HTTP shim to render-report
│   │   └── ask_caspr_service.py    # HTTP shim to ask-caspr
│   │
│   ├── observability/              # cross-cutting instrumentation that reads domain data
│   │   ├── cloudwatch_utils.py
│   │   ├── error_alerter.py
│   │   ├── functionality_context.py
│   │   ├── llm_cost_calculator.py
│   │   ├── llm_response_logger.py
│   │   └── web_search_analytics.py
│   │
│   ├── auth/  chats/  reports/  cards/  deliverables/  leads/  dashboard/
│   ├── wallet/  billing/  referrals/  onboarding/  admin/  internal/
│   │       each containing: router*.py, schemas.py, models.py,
│   │       repository.py, service.py, dependencies.py
│   │
│   └── research/                   # LLM orchestration; no HTTP surface
│       ├── agent/                  # from core/agent/
│       ├── domains/                # from core/domains/
│       ├── prompts/                # from core/prompts/
│       ├── refine/                 # from core/refine/
│       ├── visualization/          # from core/visualization/
│       └── infographics/           # from core/infographics/
│
└── tests/
    ├── conftest.py
    ├── smoke/                      # see Section 7
    └── test_refine_persist.py      # existing, unchanged
```

`research/` is not a bounded context in R-STRUCT-1's sense — it has no HTTP
surface and no models of its own. It is a sibling package holding roughly 15,000
lines of LLM orchestration that `reports/`, `cards/`, and `deliverables/` all
call into. Forcing it inside one of those contexts would create a false owner
and make the other two reach across a context boundary for their core work.

### 3.2 The shared `Base` and the ORM aggregator

The 34 ORM classes in `db/database.py` are joined by `relationship()` calls that
will span package boundaries after the split — `User` ↔ `Wallet` ↔
`Subscription` ↔ `Referral`, and `Report` ↔ `Card` ↔ `Table`. SQLAlchemy
resolves the string form of `relationship("Wallet")` against the declarative
registry at mapper-configuration time. If a model class has not been imported by
then, resolution fails with `InvalidRequestError` on first query — at runtime,
not at import.

The mechanism that makes a domain-first split safe:

1. `app/core/db.py` declares the single `Base`, carrying `MetaData` with the
   `NAMING_CONVENTION` required by R-DB-7.
2. Each context declares its own models in `app/<context>/models.py`, all
   inheriting that one `Base`.
3. `app/models.py` is a pure aggregator that imports all 34 classes and
   re-exports them. It contains no definitions.
4. `app/main.py` imports `app.models` before the first request, and
   `db_migrations/env.py` imports it as its `target_metadata` source.

Smoke gate 3 (Section 7) calls `sqlalchemy.orm.configure_mappers()` against the
aggregator, which forces resolution of every relationship string and fails loudly
if any model was missed.

### 3.3 Model ownership

| Context | Models |
|---|---|
| `auth` | `User` |
| `onboarding` | `UserRole`, `ResearchInterest`, `University`, `UserResearchInterest` |
| `chats` | `Message`, `AskCasprChat` |
| `reports` | `Report`, `ReportVersion`, `ReportVersionCard`, `Publish` |
| `cards` | `Card`, `CardVersion`, `Table`, `RefinementHistory` |
| `leads` | `Subscriber`, `Request`, `CallBooking` |
| `wallet` | `Wallet`, `TokenBatch`, `TokenTransaction` |
| `billing` | `Subscription`, `SubscriptionInterval` |
| `referrals` | `Referral` |
| `internal` | `UserVectorStore`, `UploadedFile`, `UploadedFileChunk`, `FileVersion`, `VectorStoreFile`, `ChatFile` |
| `admin` | `CostTracker`, `WebSearchEvent`, `WebSearchCitation`, `WebSearchRawResponse` |

The four analytics and cost models sit under `admin` because the only HTTP
routes that read them are the admin cost endpoints. They are written from across
the codebase via `app/observability/`, which is the accepted asymmetry: the
writer is cross-cutting infrastructure, the reader is one context.

`UserRole` is an onboarding lookup table ("What best describes you?"), not an
authorization role, and belongs to `onboarding` despite the name.

### 3.4 Route allocation

135 routes across five existing router modules map to 13 contexts. Router files
are split first by role, then by route group, so that no router file
substantially exceeds 400 lines (R-STRUCT-3).

| Context | Router files | Source |
|---|---|---|
| `auth` | `router_session.py` (signup, login, logout, google-login) · `router_password.py` (forgot-password, verify-reset-token, reset-password) · `router_verification.py` (send-verification-link, verify-account-token, get-signup-token) · `router_profile.py` (update/get-tc-verified, get-walkover-status, complete-walkover) | `api.py` |
| `chats` | `router_sessions.py` (create-session, chat-stream, chat) · `router_temp.py` (create-temp-session, temp-chat-stream, temp-chat, temp-chat/{chat_id}) · `router_management.py` (chat/{chat_id}, chat-list, chats, delete-chat, rename-chat-title, ongoing-chats) | `api.py` |
| `reports` | `router.py` (generate-report, report, presigned-urls) · `router_versions.py` (list-version-outputs, report-version-history, report-info, download-version) · `router_domains.py` (report-domains, report-domains/{domain_name}/reports) | `api.py` |
| `cards` | `router_refine.py` (refine-card, revert-card, delete-card, edit-card-content) · `router_visualization.py` (refine-visualization, delete-visualization) · `router_summary.py` (regenerate-executive-summary) | `api.py` |
| `deliverables` | `router.py` (generate-presentation, generate-infographic) | `api.py` |
| `leads` | `router.py` (subscribe, request, book-call, live-sources) | `api.py` |
| `dashboard` | `router.py` (dashboard-stats, dashboard-info, categories, home-search) | `api.py` |
| `wallet` | `router.py` (balance, transactions, topup/initiate, payment-webhook, payment-status/{payment_id}, calculate-tax) | `wallet_api.py` |
| `billing` | `router.py` (subscription, tiers, subscribe, subscription/cancel, subscription/update-payment-method, subscription/webhook, subscription/calculate-tax) | `wallet_api.py` |
| `referrals` | `router.py` (referral) | `wallet_api.py` |
| `onboarding` | `router.py` (8 routes) | `onboarding_api.py` |
| `admin` | `router.py` (me, overview, users\*) · `router_costs.py` (costs/\*) | `admin_api.py` |
| `internal` | `router_grep.py` · `router_ask_caspr.py` · `router_shared.py` (users, wallet/subscription, cost-tracker) | `internal_db_api.py` |

`user-logs` (currently `api.py`) moves to `dashboard/router.py`, as it is a
client-side telemetry sink consumed by the dashboard, not an auth concern.

### 3.5 Keeping `core/` pure under R-LAYER-1

R-LAYER-1 states that `core/*` may not import from any bounded context. Nine
modules currently under `src/core/` and `src/config/` import from `src/db/` or
`src/services/`, and would break that rule the moment they landed in
`app/core/`:

```
src/core/refine/refine_persist.py            src/core/observability/error_alerter.py
src/core/observability/llm_response_logger.py src/core/observability/cloudwatch_utils.py
src/core/agent/model.py                       src/core/integrations/openai_file_utils.py
src/core/auth/admin_auth.py                   src/core/grep_agent_2/__init__.py
src/core/domains/planner.py
```

Removing those imports means changing function signatures to accept data rather
than fetch it, which is a logic change and therefore out of scope. The
structural resolution instead is to place them where the import is legal:

- **`app/observability/`** is a top-level package, a sibling of `core/`, not a
  subpackage of it. It holds the six instrumentation modules that legitimately
  read domain data. `core/` therefore stays free of context imports and R-LAYER-1
  holds for it as written.
- **`app/research/`** is likewise top-level, so `agent/model.py`,
  `domains/planner.py`, and `refine/refine_persist.py` may import repositories.
- **`app/admin/dependencies.py`** takes `admin_auth.py`; it is a context module,
  so its repository import is the correct direction.

Two remain genuinely wrong-direction after the move: `adapters/openai_files.py`
and `adapters/grep_service.py` import repository functions, and an outbound
adapter should not reach back into a context. Fixing them requires the same
signature change, so it is deferred and recorded in Section 5.

### 3.6 Module relocation

Non-router modules move as follows. Every move is verbatim; no body is edited
except for import statements.

| From | To |
|---|---|
| `main.py` | `src/app/main.py` (rewritten as a factory — see 4.2) |
| `src/config/constants.py` | `src/app/core/constants.py` |
| `src/config/log_helper.py` | `src/app/core/logging.py` |
| `src/config/cloudwatch_helper.py` | `src/app/adapters/cloudwatch.py` (AWS client, `CloudwatchInstance`) |
| `src/config/assets/`, `llm_model_pricing.json` | `src/app/core/assets/` |
| `src/resources/exceptions.py` | `src/app/core/errors.py` |
| `src/resources/schemas/*.py` | `src/app/<context>/schemas.py` |
| `src/core/auth/token_auth.py` | `src/app/auth/dependencies.py` |
| `src/core/auth/admin_auth.py` | `src/app/admin/dependencies.py` |
| `src/core/common/internal_auth.py` | `src/app/internal/dependencies.py` |
| `src/core/common/utils.py` | `src/app/core/utils.py` |
| `src/core/integrations/s3_utils.py` | `src/app/adapters/s3.py` |
| `src/core/integrations/email_utils.py` | `src/app/adapters/email.py` |
| `src/core/integrations/openai_file_utils.py` | `src/app/adapters/openai_files.py` |
| `src/core/integrations/redis_utils.py` | `src/app/core/redis.py` |
| `src/core/observability/*` (all, including `cloudwatch_utils.py`) | `src/app/observability/` |
| `src/core/cards/*` | `src/app/cards/service*.py` |
| `src/core/report_util/pptx_utils.py`, `entry_point.py` | `src/app/deliverables/service*.py` |
| `src/core/report_util/executive_summary_updater.py` | `src/app/cards/service_summary.py` |
| `src/core/{agent,domains,prompts,refine,visualization,infographics}/` | `src/app/research/` |
| `src/core/grep_agent_2/` | `src/app/adapters/grep_service.py` |
| `src/core/ask_caspr/` | `src/app/adapters/ask_caspr_service.py` |
| `src/db/db_utils.py` | `src/app/core/db.py` + `src/app/core/sanitize.py` |
| `src/db/enums.py` | `src/app/core/enums.py` |
| `src/db/database.py` | `src/app/<context>/models.py` (per 3.3) |
| `src/db/async_db_functions.py` | `src/app/<context>/repository.py` |
| `src/db/admin_db.py` | `src/app/admin/repository.py` |
| `src/db/web_search_db.py` | `src/app/admin/repository_web_search.py` |
| `src/db/onboarding_db.py` | `src/app/onboarding/repository.py` |
| `src/db/referral_functions.py` | `src/app/referrals/repository.py` |
| `src/db/wallet_functions.py` | `src/app/wallet/repository.py` + `src/app/billing/repository.py` |
| `src/db/{grep_db,upload_db}.py` | `src/app/internal/repository_grep.py` |
| `src/db/ask_caspr_db.py` | `src/app/internal/repository_ask_caspr.py` |
| `src/services/wallet_service.py` | `src/app/wallet/service.py` (split by role group) |
| `src/services/payment_service.py` | `src/app/wallet/service_payment.py` |
| `src/services/subscription_update_validator.py` | `src/app/billing/service_validation.py` |

## 4. Changes beyond relocation

These are the only edits to code bodies. Each is structural and none alters a
client-visible response.

### 4.1 Package identity

`pyproject.toml` sets `[tool.uv] package = true` and declares the project
package, so `src/app/` is installed rather than picked up from the working
directory (R-STRUCT-2). All imports become absolute from `app.`, replacing
`from src.`.

### 4.2 Application factory

`src/app/main.py` replaces the module-level `app = FastAPI(...)` with
`create_app() -> FastAPI` (R-FA-1). The `lifespan` context manager gains
ownership of the async engine, which today is constructed at import time in
`db/db_utils.py`; it is created in `lifespan` and stored on `app.state.engine`,
with `app.state.redis` alongside it (R-DB-3). Existing CloudWatch connection and
error-digest-loop startup behaviour is preserved exactly, only relocated.

The Redis singleton and `ErrorAlertManager` currently constructed at module
scope in `main.py` move inside `lifespan`. This removes an import-time network
dependency that makes the module unimportable without a reachable Redis — which
is also what currently prevents a plain import smoke test.

`Dockerfile` and `docker-compose.yml` change their command from `main:app` to
`app.main:create_app --factory`.

### 4.3 Dependency aliases

The 73 inline `Depends(...)` call sites are replaced by module-level `Annotated`
aliases (R-FA-2): `SessionDep` in `core/db.py`, and per-context aliases such as
`CurrentUserDep` in `auth/dependencies.py`. The resolved dependency objects are
unchanged; only the declaration site moves.

### 4.4 Health endpoints

`/api/v1/health` is replaced by `/health/live` (no external checks) and
`/health/ready` (database `SELECT 1` and Redis `PING`, each with a hard
timeout), per R-FA-10.

**This is the one client-visible change in the entire design.**
`render-report` calls `caspr-core`'s `/api/v1/health` for its readiness probe,
and `docker-compose.yml` references it. To avoid breaking that contract,
`/api/v1/health` is retained as a deprecated alias returning the same
`{"status": "healthy"}` body it returns today. It is removed only after
`render-report` is migrated, which is out of scope here.

### 4.5 Constraint naming convention

`NAMING_CONVENTION` is applied to the `MetaData` on `Base` (R-DB-7). This has no
runtime effect and produces no migration on its own, but it does change what
Alembic autogenerates for constraints from this point forward. Existing
constraints created before this change retain their database-assigned names;
reconciling them is a separate, reviewed migration and is not attempted here.

### 4.6 Tooling

Added at the repository root:

- `[tool.ruff]` per R-TOOL-1 with the rule set selected explicitly
  (`E`, `W`, `F`, `I`, `UP`, `B`, `SIM`, `ASYNC`, `S`, `T20`, `RUF`),
  `line-length = 100`, `target-version = "py314"`, plus a `per-file-ignores`
  block listing modules that need a logic pass to satisfy. That list is the
  visible debt register, not a permanent exemption.
- `[tool.mypy]` per R-TOOL-2 with `strict = true` and the `pydantic.mypy`
  plugin, plus an `[[tool.mypy.overrides]]` block relaxing the not-yet-annotated
  modules. Strict is therefore a real gate for new and moved-and-cleaned code
  while the override list shrinks over time. Running strict mypy against roughly
  62,000 largely unannotated lines with no overrides would produce thousands of
  errors and no usable gate.
- `[tool.pytest.ini_options]` per R-TEST-1 and R-TEST-2: `asyncio_mode = "auto"`,
  `asyncio_default_fixture_loop_scope = "function"`, `--strict-markers`, and the
  `unit` / `integration` / `e2e` markers.
- `.pre-commit-config.yaml` with local hooks invoking the same `uv run` commands
  CI runs (R-TOOL-3).
- `.github/workflows/ci.yml` per R-TOOL-4: `uv sync --locked`, `ruff check`,
  `ruff format --check`, `mypy src`, `pytest`. No `continue-on-error`.
- `.github/pull_request_template.md` — the checklist from the standard's
  Section 14, verbatim.

Dev dependencies added to the `dev` group: `pytest`, `pytest-asyncio>=1.0`,
`pytest-cov`, `httpx`, `asgi-lifespan`, `ruff>=0.16`, `mypy`.

### 4.7 Dockerfile

Rewritten to the multi-stage form in R-TOOL-5: a builder stage performing the
two-phase `uv sync --frozen --no-dev` (dependencies before source), and a runtime
stage that copies only `.venv` and `src`, contains no `uv`, and runs as a
non-root `app` user. The existing WeasyPrint runtime libraries
(`libpango-1.0-0`, `libpangocairo-1.0-0`, `libffi-dev`, `shared-mime-info`) are
retained in the runtime stage, since `adapters/openai_files.py` imports
WeasyPrint at module scope.

## 5. Deferred, with reasons

Each item below is a rule this service does not yet satisfy. All are deferred
because satisfying them changes runtime behaviour, which this pass excludes.
They are listed in the order they should be tackled.

| Deferred | Rules | Why deferred, and what it will cost |
|---|---|---|
| pydantic-settings replacing `constants.py` | R-CFG-1 → R-CFG-5 | `constants.py` is 808 lines with 195 `os.getenv` calls, plus 15 more across `core/`. Converting to a frozen nested `Settings` with `SecretStr` and `extra="forbid"` changes startup failure modes: a typo'd or absent env var that is silently `None` today would crash the process at boot. That is the correct behaviour and the reason to do it, but it is a behavioural change requiring a full environment audit across all four deployment environments. |
| RFC 9457 Problem Details | R-ERR-1 → R-ERR-5, R-ERR-7 | The current envelope is `{"success": false, "error": "..."}`; RFC 9457 is a different shape with a different content type. Every frontend and every sibling service parses the current form. Requires a coordinated client migration. Also requires replacing 50 `HTTPException` raise sites across 9 files with domain exceptions. |
| structlog | R-LOG-1 → R-LOG-5, R-OBS-1 | Requires rewriting 2,031 f-string log calls to event-name-plus-kwargs form, removing 3 `print()` calls, adding a redaction processor with tests, and binding a correlation ID through contextvars. The log format change affects any downstream alerting built on the current text format. |
| Pure-ASGI middleware | R-FA-7 | The error-alert middleware is a `@app.middleware("http")` (i.e. `BaseHTTPMiddleware`) and reads and reconstructs the response body. Converting it to pure ASGI is a rewrite of its buffering logic, not a move. It is a prerequisite for correlation IDs, since contextvars do not reliably propagate out of `BaseHTTPMiddleware`. |
| SQLAlchemy 2.0 typing | R-DB-1 | All 34 models use legacy `declarative_base()` and bare `Column(...)` across 2,267 lines. Converting to `DeclarativeBase` and `Mapped[...]` is mechanical but touches every model and every migration's autogenerate comparison. |
| `lazy="raise"` on relationships | R-DB-2 | Roughly 30 relationships currently default to lazy `select`; six use `lazy="dynamic"`. Setting `lazy="raise"` will raise at every site that today performs an implicit lazy load, so it must be done together with adding explicit `selectinload`/`joinedload` at each query site. This is the highest-value deferred item and also the one most likely to surface latent `MissingGreenlet` bugs. |
| Celery | R-BG-1 → R-BG-12 | There is no Celery in this service. Report generation and other long work run on `BackgroundTasks` and `asyncio.create_task`, which have no persistence, retry, or visibility — a deploy or pod eviction loses them silently. Introducing Celery is a new subsystem with its own broker, queue topology, and idempotency requirements, and warrants its own design document. |
| Test tiers and coverage | R-TEST-1 → R-TEST-11 | This pass adds the smoke harness in Section 7 only. A real unit/integration/e2e suite with testcontainers PostgreSQL and an 80% coverage floor is a separate, larger effort. |
| `adapters/openai_files.py` and `adapters/grep_service.py` importing repositories | R-LAYER-1, R-CORE-2 (D) | An outbound adapter reaching back into a bounded context is the wrong dependency direction. Correcting it means passing the fetched data in as a parameter instead of the adapter fetching it — a signature change, therefore a logic change. See 3.5. |
| Server-side statement timeout, Redis socket timeouts | R-SEC-5 | The engine sets `command_timeout` and `timeout` but not `server_settings.statement_timeout`; the Redis client sets no socket timeouts. Adding them changes failure timing under load and should be introduced with monitoring in place. |

## 6. Execution plan

Four phases. Each ends in a verifiable state with the application booting.

### Phase 1 — Skeleton

Create `src/app/` with `core/`, `adapters/`, and empty context packages. Move
the cross-cutting modules per 3.6. Add `core/db.py` with `Base` and
`NAMING_CONVENTION`, `app/models.py` as an aggregator initially re-exporting
from the unchanged `db/database.py`, and `app/main.py` with `create_app()`.
Add all tooling from 4.6 and the Dockerfile from 4.7. Retarget
`db_migrations/env.py`.

At the end of Phase 1 the application boots from `app.main:create_app` while
still serving the original routers.

### Phase 2 — Contexts, one at a time

Order: `auth`, `onboarding`, `leads`, `referrals`, `dashboard`, `billing`,
`wallet`, `chats`, `reports`, `cards`, `deliverables`, `admin`, `internal`.

Simplest and most isolated first, so the shim mechanism is proven on `auth`
before it is relied on for the 42-route `internal` context.

For each context, in order: models to `<context>/models.py` and registered in
the aggregator; repository functions out of `async_db_functions.py` (and the
context-specific `*_db.py` modules) into `<context>/repository.py`; schemas from
`resources/schemas/`; service code, extracted where a router currently holds
business logic; routers split per 3.4; `dependencies.py` with the `Annotated`
aliases. No `exceptions.py` is created yet: with `HTTPException` still in use,
a domain-exception module would have no contents, and R-CORE-1 forbids adding an
abstraction ahead of its first use case. It arrives with R-ERR-1.

**Shims.** The old modules are not deleted during Phase 2. Each keeps a
re-export at the top so untouched code continues to import what it always did:

```python
# src/db/async_db_functions.py — TEMPORARY, removed in Phase 3
from app.auth.repository import check_user_by_id, create_user  # noqa: F401
```

The same pattern applies to `db/database.py` re-exporting relocated models, and
`config/constants.py` re-exporting from `app/core/constants.py`. Shims carry the
`TEMPORARY, removed in Phase 3` comment so they are greppable.

Run smoke gates 1–3 (Section 7) after each context lands.

### Phase 3 — Shim removal

Delete `src/config/`, `src/core/`, `src/db/`, `src/resources/`, `src/services/`,
and the root `main.py`. `grep -rn "from src\." src/ tests/ db_migrations/`
returning zero hits is the completion criterion. Remove the
`TEMPORARY, removed in Phase 3` comments along with the files carrying them.

### Phase 4 — Verification

Run the full smoke harness and the static gates. See Section 7.

## 7. Verification

The service has one test file and no CI, so the restructure needs its own
safety net. Gates 1 to 3 become permanent files under `tests/smoke/` and act as
regression guards afterwards.

**Gate 1 — Route inventory.** Before any file moves, snapshot every route from
the live application object: `path`, sorted `methods`, `name`, `response_model`
class name, and `status_code`, sorted by path, written to
`tests/smoke/route_inventory.json`. After the restructure, regenerate and assert
equality. This is the gate that catches a route silently dropped or renamed when
a 10,257-line router is split into 20 files, and it is the single most important
check in this design.

Routes whose `name` changes because the handler function moved to a
differently-named module are legitimate; the snapshot therefore keys on `path` +
`methods` and treats `name` as informational.

**Gate 2 — Import smoke.** Walk `src/app/` with `pkgutil` and import every
module. Catches circular imports introduced by the context split and names
missed by a shim. This gate is only possible after 4.2 removes the import-time
Redis connection.

**Gate 3 — Mapper configuration.** Import `app.models` and call
`sqlalchemy.orm.configure_mappers()`. Forces resolution of every string-form
`relationship()` across the new package boundaries and fails if a model was
omitted from the aggregator.

**Gate 4 — Container boot.** `docker compose build caspr-core`, start it, and
assert `/health/live`, `/health/ready`, and the deprecated `/api/v1/health` all
return 200.

**Static gates.** `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy src`, and `uv run pytest`, with `tests/test_refine_persist.py`
still passing.

All gates are run at the end of the job and their output reported.

## 8. Constraints

- **No commits.** All work is left in the working tree for review.
- **No logic changes.** Function bodies move verbatim. The only edits are
  import statements, the factory rewrite in 4.2, the dependency aliases in 4.3,
  the health endpoints in 4.4, and `NAMING_CONVENTION` in 4.5.
- **Scope is `caspr-core` only.** `ask-caspr`, `file-handling`, and
  `render-report` are not modified. The `/internal/db/*` and `/api/v1/*` wire
  contracts they depend on are preserved, with `/api/v1/health` retained as
  described in 4.4.

## 9. Risks

| Risk | Mitigation |
|---|---|
| A route is lost when `api.py` is split | Gate 1, run after every context in Phase 2 |
| A cross-package `relationship()` fails to resolve at runtime | Gate 3, plus the aggregator in 3.2 |
| A circular import appears between contexts | Gate 2; R-LAYER-1's import direction is what prevents the class of problem |
| A shim is forgotten and dead code survives Phase 3 | `grep -rn "from src\."` returning zero is the Phase 3 completion criterion |
| The mypy override list becomes permanent | It is committed as an explicit list, so it is reviewable and its length is a tracked number |
| `render-report`'s readiness probe breaks | `/api/v1/health` retained as a deprecated alias (4.4) |

## 10. Deviations recorded at completion

The standard requires deviations to be written down. These are the ones this
migration finished with, all measured after Phase 4.

### 10.1 Routers importing SQLAlchemy (R-LAYER-1)

10 import sites across 8 router modules:

```
src/app/cards/router_refine.py:22:from sqlalchemy import select
src/app/cards/router_summary.py:20:from sqlalchemy import and_, or_, select
src/app/cards/router_visualization.py:21:from sqlalchemy import select
src/app/cards/router_visualization.py:22:from sqlalchemy.orm.attributes import flag_modified
src/app/chats/router_management.py:22:from sqlalchemy import select
src/app/chats/router_sessions.py:24:from sqlalchemy import select
src/app/deliverables/router.py:18:from sqlalchemy import select
src/app/internal/router.py:31:from sqlalchemy import select
src/app/reports/router_reports.py:25:from sqlalchemy import select
src/app/reports/router_reports.py:26:from sqlalchemy.ext.asyncio import AsyncSession
```

R-LAYER-1 bans `sqlalchemy` from `*/router.py`. These are inherited from the
pre-migration `api.py`, whose handlers ran ORM queries inline. Removing them
means moving each query into the owning context's `repository.py` and giving
the handler a service call instead — a logic change, which this pass excluded.
The smoke notebook asserts this check and reports it as a known failure rather
than hiding it, so the count cannot drift upward unnoticed.

### 10.2 Files over 400 lines (R-STRUCT-3)

51 of 145 modules exceed the ceiling; the median module is
222 lines. The ten largest:

| Lines | Module |
|---|---|
| 4729 | `src/app/cards/service_cards.py` |
| 4617 | `src/app/research/agent/model.py` |
| 3203 | `src/app/wallet/service.py` |
| 2954 | `src/app/research/prompts/prompt_utils.py` |
| 2646 | `src/app/wallet/repository.py` |
| 2194 | `src/app/reports/repository.py` |
| 2189 | `src/app/chats/router_sessions.py` |
| 1824 | `src/app/research/refine/refiner.py` |
| 1753 | `src/app/cards/repository.py` |
| 1496 | `src/app/reports/router_reports.py` |

Routers were split by role and then by route group as agreed, which is what
took `api.py` from 10,257 lines to a set of context routers. The remainder are
dense pre-existing modules — `cards/service_cards.py`, `research/agent/model.py`,
`wallet/service.py` — where a further split requires understanding the code well
enough to choose a seam, i.e. a logic-level decision rather than a move.

### 10.3 Lint and type debt registers

`ruff check` and `ruff format --check` both pass. `mypy --strict` passes. Both
are real gates, achieved by listing pre-existing debt explicitly rather than by
weakening the configuration:

- `[tool.ruff.lint] ignore` names each rule deferred, with its count and the
  reason it needs a logic change. Notably `W291`/`W293` occur *inside*
  triple-quoted SQL, email templates, and LLM prompts, where stripping
  whitespace would change behaviour.
- `[[tool.mypy.overrides]]` lists the 104 modules carrying pre-existing
  type debt. Every other module is checked under `strict = true`, so new code
  is gated from day one.

Both lists are the metric. They should only shrink.

### 10.4 Pre-existing broken import

`app/cards/service_cards.py` lazily imports `src.core.gemini_card_utils` in a
fallback path. That module does not exist anywhere in the repository and has no
history in git, so the fallback raises `ModuleNotFoundError` whenever reached.
This predates the migration and was left untouched and annotated in place. It
needs its own fix: restore the module, or delete the dead fallback.

### 10.5 OpenAPI component names changed

Moving modules changed FastAPI's qualified component names for the two classes
that collide by bare name — for example
`src__resources__schemas__subscription__SubscribeResponse` became
`app__billing__schemas__SubscribeResponse`. Request and response *shapes* are
byte-identical; only the `$ref` identifier differs. A regenerated client SDK
would name those two types differently.
