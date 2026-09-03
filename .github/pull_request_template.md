### Architecture
- [ ] Routers contain no business logic and no ORM imports (R-LAYER-1, R-LAYER-2)
- [ ] Dependency direction respected: router → service → repository (R-LAYER-1)
- [ ] No new abstraction without a second concrete use case (R-CORE-1)
- [ ] No file over ~400 lines (R-STRUCT-3)

### Types & config
- [ ] All signatures annotated; `mypy --strict` passes (R-CORE-3, R-TOOL-2)
- [ ] No `os.getenv` outside `core/settings.py` (R-CFG-1)
- [ ] New secrets typed `SecretStr` (R-CFG-2)

### Database
- [ ] New relationships set `lazy="raise"` and callers eager-load explicitly (R-DB-2)
- [ ] Migration reviewed line-by-line; no unintended drop+add rename (R-DB-9)
- [ ] Migration is backward-compatible with the currently deployed version (R-DB-10)
- [ ] No `session.commit()` in a repository or a router (R-LAYER-3, R-DB-6)
- [ ] No HTTP call, task enqueue, or sleep inside an open transaction (R-DB-12)
- [ ] Celery tasks enqueued **after** commit, not inside it (R-DB-12)

### Errors & logging
- [ ] No `HTTPException` outside `core/errors.py` (R-ERR-1)
- [ ] New domain exceptions have a stable `code` and are documented in `responses=` (R-ERR-6, R-ERR-7)
- [ ] No internal details in any client-visible `detail` (R-ERR-5)
- [ ] Log calls use event names + kwargs; no f-strings; no `print` (R-LOG-1, R-LOG-2)
- [ ] Nothing sensitive logged; new sensitive keys added to the denylist, and nested/value-pattern cases covered by a test (R-LOG-5)

### Background work
- [ ] Task args are IDs/primitives only (R-BG-3)
- [ ] Task is idempotent and safe to run twice (R-BG-4)
- [ ] `autoretry_for` names transient exceptions only (R-BG-7)
- [ ] Task has an explicit `name=` and a target queue (R-BG-8)

### Async safety
- [ ] No blocking call inside `async def` (R-FA-4)
- [ ] No `AsyncSession` shared across concurrent tasks (R-DB-3)
- [ ] Every new outbound client sets an explicit timeout — HTTP, DB, Redis, broker (R-SEC-5)
- [ ] New list endpoints have a server-enforced max page size (R-FA-11, R-SEC-3)

### Tests
- [ ] Integration tests run against real PostgreSQL, not SQLite (R-TEST-5)
- [ ] No mocking of `session.execute` or repository internals (R-TEST-6)
- [ ] Celery logic tested via the plain function, not `task_always_eager` (R-TEST-7)
- [ ] Error responses asserted on full Problem Details payload (R-TEST-8)
- [ ] Coverage did not decrease (R-TEST-11)
