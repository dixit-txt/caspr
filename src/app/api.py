"""Top-level router. Contexts are mounted here and nothing else happens.

During the R-STRUCT-1 migration this file is the seam between the old and new
worlds: it starts out mounting the five pre-migration router modules, and each
Phase 2 context task replaces one of those includes with the context's own
routers. When the last legacy include is gone, Phase 3 deletes
``src/resources/``.
"""

from fastapi import APIRouter

# TEMPORARY: removed in Phase 3 (R-STRUCT-1 migration)
from src.resources.routers import admin_api, internal_db_api, onboarding_api, wallet_api
from src.resources.routers import api as legacy_api

api_router = APIRouter()

api_router.include_router(legacy_api.router, prefix="/api/v1", tags=["Chat"])
api_router.include_router(wallet_api.router, prefix="/api/v1", tags=["Wallet"])
api_router.include_router(onboarding_api.router, prefix="/api/v1", tags=["Onboarding"])
api_router.include_router(admin_api.router, prefix="/api/v1", tags=["Admin Dashboard"])
# internal_db_api's routes carry their full /internal/db/... path in each
# decorator, so no prefix here. Service-to-service only, not part of the
# public /api/v1 surface.
api_router.include_router(internal_db_api.router, tags=["Internal DB (service-to-service)"])
