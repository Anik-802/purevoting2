"""JSON Schema validation against the frozen contracts (used for finding bodies)."""
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

BASE = "https://purevoting2.local/contracts/"
_HERE = Path(__file__).resolve()
# repo checkout: <repo>/core/app → <repo>/contracts; container: /app/app → /app/contracts
CANDIDATES = [_HERE.parents[2] / "contracts", _HERE.parents[1] / "contracts"]


def contracts_dir() -> Path:
    for d in CANDIDATES:
        if (d / "common.schema.json").exists():
            return d
    raise RuntimeError("contracts/ not found")


@lru_cache
def _registry() -> Registry:
    resources = []
    for f in contracts_dir().glob("*.schema.json"):
        resources.append((BASE + f.name, DRAFT202012.create_resource(json.loads(f.read_text(encoding="utf-8")))))
    return Registry().with_resources(resources)


@lru_cache
def validator(file: str, definition: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"{BASE}{file}#/$defs/{definition}"}, registry=_registry())


def errors(file: str, definition: str, instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"[:200]
            for e in validator(file, definition).iter_errors(instance)]


@lru_cache
def enum(file: str, definition: str) -> frozenset:
    """The allowed values of an enum definition, e.g. enum("common.schema.json", "RuleId")."""
    return frozenset(json.loads((contracts_dir() / file).read_text(encoding="utf-8"))["$defs"][definition]["enum"])
