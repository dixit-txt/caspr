"""Route inventory for the caspr-core restructure (R-STRUCT-1 migration).

The restructure splits a 10,257-line router into ~20 files across 13 bounded
contexts. The failure mode that matters is a route silently disappearing or
changing shape in the process, so this module freezes the service's HTTP
contract and compares against it.

The inventory is built from ``app.openapi()`` rather than by walking
``app.routes``. Under FastAPI 0.141 / Starlette 1.6 an included router is
stored as a single opaque ``fastapi.routing._IncludedRouter`` rather than
being flattened, so a naive ``app.routes`` walk sees 10 entries instead of
132 and would report a green comparison while checking nothing. The OpenAPI
document is also the contract clients actually consume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASELINE = Path(__file__).parent / "baseline_routes.json"

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _bare(component: str) -> str:
    """Strip FastAPI's module-path qualification from a component name.

    FastAPI qualifies a component as ``pkg__mod__ClassName`` only when two
    classes share a bare name. The R-STRUCT-1 migration moves modules by
    design, so ``src__resources__schemas__subscription__SubscribeResponse``
    becoming ``app__billing__schemas__SubscribeResponse`` is the migration
    working, not a contract change. Comparing bare names keeps the gate from
    firing on every module move — the field list below is what actually
    guards the contract.
    """
    return component.rsplit("__", 1)[-1]


def _response_shape(operation: dict[str, Any], components: dict[str, Any]) -> dict[str, str]:
    """Map each status code to the response model's name and its field list.

    Recording the fields as well as the name is strictly stronger than the
    name alone: it catches a route quietly returning a different model *and*
    a model quietly losing or gaining a field, while staying blind to the
    module path the class happens to live at.
    """
    shapes: dict[str, str] = {}
    for status, response in sorted((operation.get("responses") or {}).items()):
        schema = response.get("content", {}).get("application/json", {}).get("schema", {})
        ref = schema.get("$ref") or schema.get("items", {}).get("$ref", "")
        if not ref:
            shapes[status] = ""
            continue
        component = ref.rsplit("/", 1)[-1]
        fields = sorted(components.get(component, {}).get("properties") or {})
        shapes[status] = f"{_bare(component)}({','.join(fields)})"
    return shapes


def build_inventory(app: Any) -> dict[str, Any]:
    """Return a stable, diffable description of every HTTP operation."""
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})
    operations: dict[str, Any] = {}
    for path, path_item in spec.get("paths", {}).items():
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            operations[f"{method.upper()} {path}"] = {
                "responses": _response_shape(operation, components),
                "request_body": bool(operation.get("requestBody")),
                "parameters": sorted(
                    f"{p.get('in')}:{p.get('name')}" for p in operation.get("parameters", [])
                ),
            }
    return {"operation_count": len(operations), "operations": operations}


def load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def diff(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Human-readable differences; empty list means the contract is intact."""
    problems: list[str] = []
    old, new = baseline["operations"], current["operations"]

    for key in sorted(set(old) - set(new)):
        problems.append(f"REMOVED  {key}")
    for key in sorted(set(new) - set(old)):
        problems.append(f"ADDED    {key}")
    for key in sorted(set(old) & set(new)):
        if old[key] != new[key]:
            problems.append(f"CHANGED  {key}\n    was: {old[key]}\n    now: {new[key]}")
    return problems


def _load_app() -> Any:
    """Import the application, preferring the post-restructure factory.

    The repository root is prepended to ``sys.path`` because this module is
    also runnable as a script from ``tests/smoke/``, where the root would
    otherwise not be importable.
    """
    import sys

    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.main import create_app  # type: ignore[import-not-found]

        return create_app()
    except ImportError:
        import main  # type: ignore[import-not-found]

        return main.app


if __name__ == "__main__":
    import sys

    inventory = build_inventory(_load_app())
    if "--write-baseline" in sys.argv:
        BASELINE.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
        print(f"baseline written: {inventory['operation_count']} operations")
    else:
        problems = diff(load_baseline(), inventory)
        if problems:
            print("\n".join(problems))
            print(f"\nFAIL: {len(problems)} difference(s)")
            sys.exit(1)
        print(f"OK: {inventory['operation_count']} operations match baseline")
