"""internal_auth.py — the header that proves an /internal/* caller is caspr-core.

render-report gates every `/internal/render/*` route on an `x-internal-secret`
header (see that service's `src/resources/dependencies.py`). It skips the check
in DEV when `INTERNAL_API_SECRET` is unset — so a developer can curl it — and
refuses to boot in production without it.

The three HTTP shims in this service (`report_util/entry_point.py`,
`report_util/pptx_utils.py`, `infographics/one_pager.py`) call that router, so
they all need the header under exactly the same condition: send it when a
secret is configured, send nothing when one is not. That "exactly the same
condition" is why it lives in one function rather than three copies — the
failure mode of the copies drifting is a 403 in production only.
"""

from app.core.constants import INTERNAL_API_SECRET

#: The header name render-report reads. Keep in sync with
#: render-report/src/resources/dependencies.py:INTERNAL_SECRET_HEADER.
INTERNAL_SECRET_HEADER = "x-internal-secret"


def internal_headers() -> dict[str, str]:
    """Auth headers for a call to a sibling service's /internal/* router.

    Returns an empty dict when no secret is configured, which is the local-dev
    case: the callee skips the check, and sending an empty header value would
    instead fail its constant-time comparison the moment a secret IS set on
    only one side.
    """
    if not INTERNAL_API_SECRET:
        return {}
    return {INTERNAL_SECRET_HEADER: INTERNAL_API_SECRET}
