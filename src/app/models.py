"""Single import site for every ORM class in the service.

SQLAlchemy resolves the string form of ``relationship("Wallet")`` against the
declarative registry at mapper-configuration time. Models are declared in
``app/<context>/models.py`` but all inherit the one ``app.core.db.Base``, so a
class that has not been imported by then fails resolution with
``InvalidRequestError`` at first query — at runtime, in production, not at
import. Importing this module guarantees every mapper is registered.

``app.main`` imports it during application construction and
``db_migrations/env.py`` imports it before autogenerate, so both paths see the
complete metadata.
"""

from app.admin.models import (  # noqa: F401
    CostTracker,
    WebSearchCitation,
    WebSearchEvent,
    WebSearchRawResponse,
)
from app.auth.models import (  # noqa: F401
    User,
)
from app.billing.models import (  # noqa: F401
    Subscription,
    SubscriptionInterval,
)
from app.cards.models import (  # noqa: F401
    Card,
    CardVersion,
    RefinementHistory,
    Table,
)
from app.chats.models import (  # noqa: F401
    AskCasprChat,
    Message,
)
from app.internal.models import (  # noqa: F401
    ChatFile,
    FileVersion,
    UploadedFile,
    UploadedFileChunk,
    UserVectorStore,
    VectorStoreFile,
)
from app.leads.models import (  # noqa: F401
    CallBooking,
    Request,
    Subscriber,
)
from app.onboarding.models import (  # noqa: F401
    ResearchInterest,
    University,
    UserResearchInterest,
    UserRole,
)
from app.referrals.models import (  # noqa: F401
    Referral,
)
from app.reports.models import (  # noqa: F401
    Publish,
    Report,
    ReportVersion,
    ReportVersionCard,
)
from app.wallet.models import (  # noqa: F401
    TokenBatch,
    TokenTransaction,
    Wallet,
)
