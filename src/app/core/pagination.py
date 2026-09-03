"""Server-enforced pagination bounds.

R-FA-11 and R-SEC-3: an unbounded ``?limit=`` is not merely a memory risk, it
is a denial-of-service lever any client can pull, because Pydantic validation
is synchronous CPU work on the event loop and stalls every concurrent request
on the worker.

Not yet wired into any endpoint. The existing list endpoints keep their
current behaviour; adopting these bounds changes what a client may request and
is therefore a logic change, deferred per spec §5.
"""

from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 100


class Page(BaseModel):
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)  # le= is the enforcement
    offset: int = Field(default=0, ge=0)
