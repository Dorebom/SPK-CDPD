"""Cold-worker validation of the exact pinned L02 contract, with full fallback.

This module imports only the standard library and the existing strict spool
parser. It does not authorize capture, change a deadline, or publish evidence.
The public Bundle validator remains the canonical fallback and COMMIT verifier.
Only successful checks may take the fast path; every unsupported or rejected
case is passed to the original validator with the original input.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable

from .spool import canonical_json, strict_json, validate_envelope

ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_SHA256 = "2bb2483725d3042c3f53e7a44d616c4f949c0f3fe7868e716b6d74ca2eeca996"
_CONTRACT_SHA256 = "12673cb12fa8761acb41e665bd8987674890b80f79eb9eb1c1f184926ffb361c"
_MATRIX_SHA256 = "6e491659097cba50e37fa25ebf9d2de0bb7d358c09d1297297418a6207718ced"
_PINS = (
    ("bundle_payload_v1.schema.json", _SCHEMA_SHA256),
    ("bundle_input_contract.json", _CONTRACT_SHA256),
    ("diagnostic_event_matrix.json", _MATRIX_SHA256),
)
_KEYWORDS = frozenset({
    "$ref", "type", "required", "properties", "additionalProperties", "items",
    "minItems", "maxItems", "anyOf", "minimum", "maximum", "enum", "const",
    "minLength", "maxLength", "pattern", "not",
})
_STRUCTURE_REFS = frozenset({"structure", "node", "probe", "shape_node"})


class _FastUnavailable(ValueError):
    """The small approved schema subset cannot be used; take full validation."""


def _jsonschema_loaded() -> bool:
    # Preserve an embedding application's already-installed FormatChecker and
    # validator customizations. A copied key tuple is safe during other imports.
    return any(name == "jsonschema" or name.startswith("jsonschema.")
               for name in tuple(sys.modules))


def _compile_structure(schema: dict) -> Callable[[Any], bool]:
    """Compile only the format-free, pinned STRUCTURE subtree's 17 keywords.

    Values reaching this predicate have undergone a strict JSON round trip.
    Exact integer tokens are required, as in the existing extended validator.
    Every unknown keyword/type/ref or unsupported schema value rejects this
    compiler rather than being silently ignored. There is no eval or codegen.
    """
    if type(schema) is not dict or type(schema.get("$defs")) is not dict:
        raise _FastUnavailable()
    definitions = schema["$defs"]
    compiled: dict[str, Callable[[Any], bool]] = {}
    active: set[str] = set()
    types = {
        "object": lambda value: type(value) is dict,
        "array": lambda value: type(value) is list,
        "string": lambda value: type(value) is str,
        "integer": lambda value: type(value) is int,
        "number": lambda value: (type(value) is int or
                                  type(value) is float and math.isfinite(value)),
        "boolean": lambda value: type(value) is bool,
        "null": lambda value: value is None,
    }

    def reference(name):
        if type(name) is not str or not name.startswith("#/$defs/"):
            raise _FastUnavailable()
        key = name[len("#/$defs/"):]
        if key not in _STRUCTURE_REFS or key not in definitions or key in active:
            raise _FastUnavailable()
        if key not in compiled:
            active.add(key)
            try:
                compiled[key] = compile_node(definitions[key])
            finally:
                active.remove(key)
        return compiled[key]

    def nonnegative_integer(value):
        if type(value) is not int or value < 0:
            raise _FastUnavailable()
        return value

    def compile_node(node):
        if type(node) is not dict or set(node) - _KEYWORDS:
            raise _FastUnavailable()
        checks = []
        if "$ref" in node:
            checks.append(reference(node["$ref"]))
        if "type" in node:
            kind = node["type"]
            if type(kind) is not str or kind not in types:
                raise _FastUnavailable()
            checks.append(types[kind])
        if "const" in node:
            # Every const/enum in the attested STRUCTURE subtree is a string.
            # Numeric/bool equality therefore cannot be conflated by Python.
            const = node["const"]
            if type(const) is not str:
                raise _FastUnavailable()
            checks.append(lambda value, const=const: type(value) is str and value == const)
        if "enum" in node:
            enum = node["enum"]
            if type(enum) is not list or not enum or any(type(item) is not str for item in enum):
                raise _FastUnavailable()
            allowed = frozenset(enum)
            checks.append(lambda value, allowed=allowed: type(value) is str and value in allowed)
        if "anyOf" in node:
            alternatives = node["anyOf"]
            if type(alternatives) is not list or not alternatives:
                raise _FastUnavailable()
            alternatives = tuple(compile_node(item) for item in alternatives)
            checks.append(lambda value, alternatives=alternatives: any(check(value) for check in alternatives))
        if "not" in node:
            prohibited = compile_node(node["not"])
            checks.append(lambda value, prohibited=prohibited: not prohibited(value))
        if "required" in node:
            required = node["required"]
            if type(required) is not list or any(type(item) is not str for item in required):
                raise _FastUnavailable()
            required = frozenset(required)
            checks.append(lambda value, required=required:
                          type(value) is not dict or required.issubset(value))
        properties = node.get("properties", {})
        if type(properties) is not dict or any(type(name) is not str for name in properties):
            raise _FastUnavailable()
        properties = tuple((name, compile_node(part)) for name, part in properties.items())
        if "properties" in node:
            checks.append(lambda value, properties=properties: type(value) is not dict or
                          all(name not in value or check(value[name]) for name, check in properties))
        if "additionalProperties" in node:
            if node["additionalProperties"] is not False:
                raise _FastUnavailable()
            names = frozenset(name for name, _ in properties)
            checks.append(lambda value, names=names: type(value) is not dict or set(value).issubset(names))
        if "items" in node:
            item_check = compile_node(node["items"])
            checks.append(lambda value, item_check=item_check:
                          type(value) is not list or all(item_check(item) for item in value))
        if "minItems" in node:
            minimum = nonnegative_integer(node["minItems"])
            checks.append(lambda value, minimum=minimum: type(value) is not list or len(value) >= minimum)
        if "maxItems" in node:
            maximum = nonnegative_integer(node["maxItems"])
            checks.append(lambda value, maximum=maximum: type(value) is not list or len(value) <= maximum)
        if "minLength" in node:
            minimum = nonnegative_integer(node["minLength"])
            checks.append(lambda value, minimum=minimum: type(value) is not str or len(value) >= minimum)
        if "maxLength" in node:
            maximum = nonnegative_integer(node["maxLength"])
            checks.append(lambda value, maximum=maximum: type(value) is not str or len(value) <= maximum)
        if "pattern" in node:
            if type(node["pattern"]) is not str:
                raise _FastUnavailable()
            try:
                pattern = re.compile(node["pattern"])
            except re.error:
                raise _FastUnavailable() from None
            checks.append(lambda value, pattern=pattern: type(value) is not str or pattern.search(value) is not None)
        for keyword in ("minimum", "maximum"):
            if keyword not in node:
                continue
            bound = node[keyword]
            if type(bound) not in (int, float) or type(bound) is float and not math.isfinite(bound):
                raise _FastUnavailable()
            if keyword == "minimum":
                checks.append(lambda value, bound=bound:
                              type(value) not in (int, float) or value >= bound)
            else:
                checks.append(lambda value, bound=bound:
                              type(value) not in (int, float) or value <= bound)
        checks = tuple(checks)
        return lambda value: all(check(value) for check in checks)

    return reference("#/$defs/structure")


@dataclass(frozen=True)
class _AttestedContracts:
    structure_check: Callable[[Any], bool]
    structure_limit: int
    depth_limit: int
    # Rules are retained as bytes; each returned rule is detached afresh.
    rules: tuple[tuple[tuple[str, str, str], bytes], ...]


@lru_cache(maxsize=1)
def _attested_contracts() -> _AttestedContracts | None:
    try:
        documents = []
        for name, expected in _PINS:
            raw = (ROOT / "contracts" / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                return None
            documents.append(json.loads(raw))
        schema, contract, matrix = documents
        if contract["file_definitions"]["STRUCTURE.json"] != "structure":
            return None
        limits = contract["byte_limits"]
        structure_limit = limits.get("STRUCTURE.json", limits["total"])
        depth_limit = limits["maximum_json_depth"]
        if type(structure_limit) is not int or type(depth_limit) is not int:
            return None
        rules = tuple(((rule["event_kind"], rule["failure_code"], rule["profile_reason"]),
                       canonical_json(rule)) for rule in matrix["rules"])
        return _AttestedContracts(_compile_structure(schema), structure_limit, depth_limit, rules)
    except Exception:
        # Unavailable/malformed/unsupported local contracts cannot enable a
        # shortcut. The original path remains responsible for the final error.
        return None


def _parsed_structure(value, contracts):
    raw = canonical_json(value)
    parsed = strict_json(raw, max_bytes=contracts.structure_limit)
    pending = [(parsed, 0)]
    while pending:
        part, depth = pending.pop()
        if depth > contracts.depth_limit:
            raise _FastUnavailable()
        if type(part) is dict:
            pending.extend((item, depth + 1) for item in part.values())
        elif type(part) is list:
            pending.extend((item, depth + 1) for item in part)
    return parsed


def capture_rule(envelope: dict[str, Any]) -> dict[str, Any]:
    """Classify an independently revalidated Worker envelope; never a permit.

    The existing spool validator accepts a strict subset of this exact schema.
    Inputs outside that subset still receive the unchanged general validator,
    preserving such cases as opaque IDs containing only whitespace.
    """
    # spool.validate_envelope also parses raw bytes; the canonical Bundle API
    # takes a JSON value. Only a worker's detached dict may use this shortcut.
    if type(envelope) is dict and not _jsonschema_loaded():
        try:
            contracts = _attested_contracts()
            if contracts is not None:
                parsed = validate_envelope(envelope)
                triple = (parsed["event"]["kind"], parsed["event"]["failure_code"], parsed["profile"]["reason"])
                matches = [raw for key, raw in contracts.rules if key == triple]
                if len(matches) == 1:
                    return strict_json(matches[0])
        except Exception:
            pass
    from .bundle_validation import capture_rule as canonical_capture_rule
    return canonical_capture_rule(envelope)


def validate_structure_payload(value: dict[str, Any]) -> None:
    """Validate the typed structure before the unchanged worker semantic checks."""
    if not _jsonschema_loaded():
        try:
            contracts = _attested_contracts()
            if contracts is not None and contracts.structure_check(_parsed_structure(value, contracts)):
                return
        except Exception:
            pass
    from .bundle_validation import validate_payload_file
    validate_payload_file("STRUCTURE.json", value)
