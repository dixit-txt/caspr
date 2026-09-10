"""Top-level router. Contexts are mounted here and nothing else happens.

Every one of the 13 bounded contexts owns its own routes. The pre-migration
``src/resources/routers/`` package is gone entirely.
"""

from fastapi import APIRouter

from app.admin import router as admin_router
from app.auth import router_password, router_profile, router_session, router_verification
from app.billing import router as billing_router
from app.cards import router_refine, router_summary, router_visualization
from app.chats import router_management, router_sessions, router_temp
from app.dashboard import router as dashboard_router
from app.deliverables import router as deliverables_router
from app.internal import router as internal_router
from app.leads import router as leads_router
from app.onboarding import router as onboarding_router
from app.referrals import router as referrals_router
from app.reports import router_domains, router_reports, router_versions
from app.wallet import router as wallet_router

api_router = APIRouter()

for _auth_part in (router_session, router_password, router_verification, router_profile):
    api_router.include_router(_auth_part.router, prefix="/api/v1", tags=["Auth"])
for _chats_part in (router_sessions, router_temp, router_management):
    api_router.include_router(_chats_part.router, prefix="/api/v1", tags=["Chat"])
for _reports_part in (router_reports, router_versions, router_domains):
    api_router.include_router(_reports_part.router, prefix="/api/v1", tags=["Reports"])
for _cards_part in (router_refine, router_visualization, router_summary):
    api_router.include_router(_cards_part.router, prefix="/api/v1", tags=["Cards"])
api_router.include_router(deliverables_router.router, prefix="/api/v1", tags=["Deliverables"])
api_router.include_router(leads_router.router, prefix="/api/v1", tags=["Leads"])
api_router.include_router(dashboard_router.router, prefix="/api/v1", tags=["Dashboard"])
api_router.include_router(wallet_router.router, prefix="/api/v1", tags=["Wallet"])
api_router.include_router(billing_router.router, prefix="/api/v1", tags=["Billing"])
api_router.include_router(referrals_router.router, prefix="/api/v1", tags=["Referrals"])
api_router.include_router(onboarding_router.router, prefix="/api/v1", tags=["Onboarding"])
api_router.include_router(admin_router.router, prefix="/api/v1", tags=["Admin Dashboard"])
# internal router's routes carry their full /internal/db/... path in each
# decorator, so no prefix here. Service-to-service only.
api_router.include_router(internal_router.router, tags=["Internal DB (service-to-service)"])
