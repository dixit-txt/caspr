"""Gate 1 (spec §7): the HTTP contract must survive the restructure.

This is the load-bearing check of the R-STRUCT-1 migration. Splitting a
10,257-line router across 13 context packages can silently drop or rename a
route, and nothing else in the repository would notice.
"""

import pytest

from tests.smoke.route_inventory import _load_app, build_inventory, diff, load_baseline


@pytest.mark.smoke
def test_route_inventory_matches_baseline() -> None:
    problems = diff(load_baseline(), build_inventory(_load_app()))
    assert not problems, "HTTP contract changed:\n" + "\n".join(problems)
