"""Report-domain taxonomy constants.

These lived as module-level assignments in ``src/db/async_db_functions.py``.
``reports`` owns them because they describe report domains, but ``chats`` and
``dashboard`` both read them, so they sit in their own module rather than
inside ``reports/repository.py`` — importing a repository just to read a
constant would drag a database module into a caller that does not need one.

Cross-context imports of these are absolute and explicit by design (R-STRUCT-2):
the coupling should be visible in the importer's import block.
"""

from app.core.enums import ReportStatus

#: Every report status except DRAFT. Used to separate finished work from drafts.
_NON_DRAFT_STATUSES = [s.value for s in ReportStatus if s != ReportStatus.DRAFT]

#: External-facing slug for the default (uncategorised) report domain.
STANDARD_CATEGORY_SLUG = "standard"

#: Internal representation of that same domain.
DEFAULT_DOMAIN_INTERNAL = "default"
