"""Provider-neutral LIB-04 structured-generation contracts; performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping


STRUCTURED_GENERATION_SCHEMA = "vera.structured-generation-plan/v1"
STRUCTURED_VALIDATION_SCHEMA = "vera.structured-generation-validation/v1"
_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_KEYWORDS = frozenset({
    "$id", "$schema", "additionalProperties", "allOf", "anyOf", "const",
    "default", "description", "enum", "exclusiveMaximum", "exclusiveMinimum",
    "items", "maxItems", "maxLength", "maximum", "minItems", "minLength",
    "minimum", "multipleOf", "not", "oneOf", "properties", "required",
    "title", "type", "uniqueItems",
})
_ANNOTATIONS = frozenset({"$id", "$schema", "default", "description", "title"})
_PROFILES = {
    "provider_native": {
        "label": "Provider-native JSON Schema",
        "optional_dependency": "",
        "availability": "runtime_dependent",
        "generation_mode": "provider_schema",
        "retry_owner": "vera",
        "semantic_correction": False,
        "constrained_decoding": "provider_dependent",
        "streaming": "queued_live",
    },
    "instructor": {
        "label": "Instructor",
        "optional_dependency": "instructor",
        "availability": "not_imported",
        "generation_mode": "typed_validation_and_correction",
        "retry_owner": "instructor",
        "semantic_correction": True,
        "constrained_decoding": False,
        "streaming": "queued_live",
    },
    "outlines": {
        "label": "Outlines",
        "optional_dependency": "outlines",
        "availability": "not_imported",
        "generation_mode": "constrained_decoding",
        "retry_owner": "vera",
        "semantic_correction": False,
        "constrained_decoding": True,
        "streaming": "queued_live",
    },
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _bounded_text(value: Any, name: str, maximum: int = 256) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _number(value: Any, name: str) -> float | int:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        raise ValueError(f"{name} must be a finite number")
    return value


def _bound_json_value(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > 500:
        raise ValueError("structured value exceeds 500 nodes")
    if depth > 12:
        raise ValueError("structured value nesting exceeds 12 levels")
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise ValueError("structured value object exceeds 100 fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("structured value keys must be bounded strings")
            _bound_json_value(item, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ValueError("structured value array exceeds 100 items")
        for item in value:
            _bound_json_value(item, depth=depth + 1, counter=counter)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("structured value must contain JSON values")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("structured value numbers must be finite")


@dataclass(frozen=True)
class SchemaAnalysis:
    schema_id: str
    normalized_schema: Mapping[str, Any]
    issues: tuple[dict[str, str], ...]
    node_count: int

    @property
    def accepted(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "vera.structured-schema-analysis/v1",
            "schema_id": self.schema_id,
            "normalized_schema": self.normalized_schema,
            "issues": list(self.issues),
            "node_count": self.node_count,
            "accepted": self.accepted,
            "executes": False,
        }


def analyze_schema(schema: Mapping[str, Any]) -> SchemaAnalysis:
    """Normalize the supported deterministic JSON-Schema subset."""
    if not isinstance(schema, Mapping):
        raise TypeError("JSON Schema must be an object")
    encoded = _canonical(schema)
    if len(encoded.encode()) > 65536:
        raise ValueError("JSON Schema exceeds 65536 bytes")
    issues: list[dict[str, str]] = []
    nodes = 0

    def issue(path: str, code: str, detail: str) -> None:
        if len(issues) < 100:
            issues.append({"path": path, "code": code, "detail": detail})

    def walk(value: Any, path: str, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > 500:
            raise ValueError("JSON Schema exceeds 500 nodes")
        if depth > 12:
            raise ValueError("JSON Schema nesting exceeds 12 levels")
        if not isinstance(value, Mapping):
            issue(path, "schema_not_object", "schema nodes must be objects")
            return {}
        out: dict[str, Any] = {}
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            item = value[raw_key]
            here = f"{path}.{key}"
            if len(key) > 128:
                issue(here, "keyword_too_long", "schema keyword exceeds 128 characters")
                continue
            if key == "$ref" or key == "$dynamicRef":
                issue(here, "reference_unsupported", "schema references are not resolved")
                continue
            if key not in _KEYWORDS:
                issue(here, "keyword_unsupported", "keyword is outside the portable subset")
                continue
            if key in _ANNOTATIONS:
                if key in {"$id", "$schema", "description", "title"}:
                    if not isinstance(item, str) or len(item) > 2000:
                        issue(here, "annotation_invalid", "annotation must be a bounded string")
                        continue
                out[key] = item
            elif key == "type":
                raw_types = [item] if isinstance(item, str) else item
                if (not isinstance(raw_types, list) or not raw_types
                        or any(kind not in _TYPES for kind in raw_types)
                        or len(set(raw_types)) != len(raw_types)):
                    issue(here, "type_invalid", "type must contain unique portable JSON types")
                else:
                    out[key] = sorted(raw_types) if len(raw_types) > 1 else raw_types[0]
            elif key == "properties":
                if not isinstance(item, Mapping) or len(item) > 100:
                    issue(here, "properties_invalid", "properties must be a bounded object")
                else:
                    out[key] = {str(name): walk(child, f"{here}.{name}", depth + 1)
                                for name, child in sorted(item.items(), key=lambda pair: str(pair[0]))}
            elif key == "required":
                if (not isinstance(item, list) or len(item) > 100
                        or any(not isinstance(name, str) or not name or len(name) > 128
                               for name in item)
                        or len(set(item)) != len(item)):
                    issue(here, "required_invalid", "required must contain unique bounded property names")
                else:
                    out[key] = sorted(item)
            elif key in {"items", "not"}:
                out[key] = walk(item, here, depth + 1)
            elif key == "additionalProperties":
                if isinstance(item, bool):
                    out[key] = item
                elif isinstance(item, Mapping):
                    out[key] = walk(item, here, depth + 1)
                else:
                    issue(here, "additional_properties_invalid", "must be boolean or a schema")
            elif key in {"allOf", "anyOf", "oneOf"}:
                if not isinstance(item, list) or not item or len(item) > 20:
                    issue(here, "combinator_invalid", "combinator must contain 1 to 20 schemas")
                else:
                    out[key] = [walk(child, f"{here}[{index}]", depth + 1)
                                for index, child in enumerate(item)]
            elif key == "enum":
                if not isinstance(item, list) or not item or len(item) > 100:
                    issue(here, "enum_invalid", "enum must contain 1 to 100 values")
                else:
                    try:
                        identities = [_canonical(option) for option in item]
                    except (TypeError, ValueError):
                        issue(here, "enum_invalid", "enum values must be finite JSON values")
                    else:
                        if len(set(identities)) != len(identities):
                            issue(here, "enum_duplicate", "enum values must be unique")
                        out[key] = sorted(item, key=_canonical)
            elif key == "const":
                try:
                    _canonical(item)
                    out[key] = item
                except (TypeError, ValueError):
                    issue(here, "const_invalid", "const must be a finite JSON value")
            elif key in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}:
                try:
                    number = _number(item, key)
                    if key == "multipleOf" and number <= 0:
                        raise ValueError("multipleOf must be positive")
                    out[key] = number
                except ValueError as exc:
                    issue(here, "numeric_constraint_invalid", str(exc))
            elif key in {"minLength", "maxLength", "minItems", "maxItems"}:
                if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                    issue(here, "size_constraint_invalid", "size constraint must be a non-negative integer")
                else:
                    out[key] = item
            elif key == "uniqueItems":
                if not isinstance(item, bool):
                    issue(here, "unique_items_invalid", "uniqueItems must be boolean")
                else:
                    out[key] = item
        properties = out.get("properties")
        required = out.get("required", [])
        if required and not isinstance(properties, Mapping):
            issue(path, "required_without_properties", "required needs a properties object")
        elif properties is not None:
            for name in required:
                if name not in properties:
                    issue(f"{path}.required", "required_unknown", f"required property {name!r} is undeclared")
        if "minLength" in out and "maxLength" in out and out["minLength"] > out["maxLength"]:
            issue(path, "string_bounds_invalid", "minLength exceeds maxLength")
        if "minItems" in out and "maxItems" in out and out["minItems"] > out["maxItems"]:
            issue(path, "array_bounds_invalid", "minItems exceeds maxItems")
        return out

    normalized = walk(schema, "$", 0)
    schema_id = "structschema_" + hashlib.sha256(_canonical(normalized).encode()).hexdigest()
    return SchemaAnalysis(schema_id, normalized,
                          tuple(sorted(issues, key=lambda row: (row["path"], row["code"]))), nodes)


@dataclass(frozen=True)
class StructuredGenerationPlan:
    plan_id: str
    schema_id: str
    provider_profile: str
    retry_budget: int
    retry_owner: str
    streaming_requested: bool
    semantic_validator_ref: str
    latency_budget_ms: int
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        profile = dict(_PROFILES[self.provider_profile])
        return {
            "schema": STRUCTURED_GENERATION_SCHEMA,
            "plan_id": self.plan_id,
            "schema_id": self.schema_id,
            "provider_profile": self.provider_profile,
            "profile": profile,
            "retry": {"budget": self.retry_budget, "owner": self.retry_owner,
                      "attempts_started": 0},
            "streaming": {"requested": self.streaming_requested,
                          "verification": profile["streaming"]},
            "semantic_validator_ref": self.semantic_validator_ref,
            "latency_budget_ms": self.latency_budget_ms,
            "issues": list(self.issues),
            "accepted": not self.issues,
            "ready_for_generation": False,
            "imports_optional_provider": False,
            "model_called": False,
            "tokens_decoded": False,
            "stream_started": False,
            "executes": False,
        }


def plan_structured_generation(
        schema: Mapping[str, Any], *, provider_profile: str,
        retry_budget: int = 0, retry_owner: str = "",
        streaming: bool = False, semantic_validator_ref: str = "",
        latency_budget_ms: int = 0,
) -> tuple[SchemaAnalysis, StructuredGenerationPlan]:
    analysis = analyze_schema(schema)
    profile_name = str(provider_profile or "").strip().casefold()
    if profile_name not in _PROFILES:
        raise ValueError("unknown structured-generation provider profile")
    if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or not 0 <= retry_budget <= 5:
        raise ValueError("retry_budget must be an integer from 0 to 5")
    expected_owner = _PROFILES[profile_name]["retry_owner"]
    owner = str(retry_owner or expected_owner).strip().casefold()
    if owner != expected_owner:
        raise ValueError(f"{profile_name} requires retry_owner={expected_owner}")
    if not isinstance(streaming, bool):
        raise TypeError("streaming must be boolean")
    validator = str(semantic_validator_ref or "").strip()
    if validator:
        if (not validator.startswith("validatorref:")
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,245}", validator[13:])):
            raise ValueError("semantic validators must use an opaque validatorref: reference")
    if (not isinstance(latency_budget_ms, int) or isinstance(latency_budget_ms, bool)
            or latency_budget_ms < 0 or latency_budget_ms > 3600000):
        raise ValueError("latency_budget_ms must be an integer from 0 to 3600000")
    issues = [f"schema:{row['code']}:{row['path']}" for row in analysis.issues]
    if streaming:
        issues.append("streaming_unverified")
    identity = {
        "schema": STRUCTURED_GENERATION_SCHEMA, "schema_id": analysis.schema_id,
        "provider_profile": profile_name, "retry_budget": retry_budget,
        "retry_owner": owner, "streaming": streaming,
        "semantic_validator_ref": validator, "latency_budget_ms": latency_budget_ms,
        "schema_issues": list(issues),
    }
    plan_id = "structplan_" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
    return analysis, StructuredGenerationPlan(
        plan_id, analysis.schema_id, profile_name, retry_budget, owner,
        streaming, validator, latency_budget_ms, tuple(sorted(issues)))


def validate_structured_value(
        schema: Mapping[str, Any], value: Any, *,
        semantic_status: str = "not_requested",
) -> dict[str, Any]:
    """Validate a JSON value without returning its content or running validators."""
    analysis = analyze_schema(schema)
    violations: list[dict[str, str]] = []

    def fail(path: str, code: str) -> None:
        if len(violations) < 100:
            violations.append({"path": path, "code": code})

    def matches_type(item: Any, kind: str) -> bool:
        return {
            "null": item is None,
            "boolean": isinstance(item, bool),
            "integer": isinstance(item, int) and not isinstance(item, bool),
            "number": isinstance(item, (int, float)) and not isinstance(item, bool)
                      and math.isfinite(item),
            "string": isinstance(item, str),
            "array": isinstance(item, list),
            "object": isinstance(item, Mapping),
        }[kind]

    def check(node: Mapping[str, Any], item: Any, path: str) -> bool:
        start = len(violations)
        kinds = node.get("type")
        if kinds:
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if not any(matches_type(item, kind) for kind in kinds):
                fail(path, "type")
                return False
        if "const" in node and _canonical(item) != _canonical(node["const"]):
            fail(path, "const")
        if "enum" in node and _canonical(item) not in {_canonical(v) for v in node["enum"]}:
            fail(path, "enum")
        if isinstance(item, Mapping):
            props = node.get("properties", {})
            for name in node.get("required", []):
                if name not in item:
                    fail(f"{path}.{name}", "required")
            for name, child in props.items():
                if name in item:
                    check(child, item[name], f"{path}.{name}")
            extras = [name for name in item if name not in props]
            additional = node.get("additionalProperties", True)
            if additional is False:
                for name in extras:
                    fail(f"{path}.{name}", "additional_property")
            elif isinstance(additional, Mapping):
                for name in extras:
                    check(additional, item[name], f"{path}.{name}")
        if isinstance(item, list):
            if "minItems" in node and len(item) < node["minItems"]:
                fail(path, "min_items")
            if "maxItems" in node and len(item) > node["maxItems"]:
                fail(path, "max_items")
            if node.get("uniqueItems") and len({_canonical(v) for v in item}) != len(item):
                fail(path, "unique_items")
            if isinstance(node.get("items"), Mapping):
                for index, child in enumerate(item):
                    check(node["items"], child, f"{path}[{index}]")
        if isinstance(item, str):
            if "minLength" in node and len(item) < node["minLength"]:
                fail(path, "min_length")
            if "maxLength" in node and len(item) > node["maxLength"]:
                fail(path, "max_length")
        if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            for keyword, operator in (
                ("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                ("exclusiveMinimum", lambda a, b: a > b),
                ("exclusiveMaximum", lambda a, b: a < b)):
                if keyword in node and not operator(item, node[keyword]):
                    fail(path, keyword)
            if "multipleOf" in node:
                quotient = item / node["multipleOf"]
                if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                    fail(path, "multiple_of")
        for keyword, expectation in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "one")):
            if keyword in node:
                matches = 0
                for child in node[keyword]:
                    before = len(violations)
                    check(child, item, path)
                    child_ok = len(violations) == before
                    del violations[before:]
                    matches += int(child_ok)
                if ((expectation == "all" and matches != len(node[keyword]))
                        or (expectation == "any" and matches == 0)
                        or (expectation == "one" and matches != 1)):
                    fail(path, keyword)
        if "not" in node:
            before = len(violations)
            check(node["not"], item, path)
            child_ok = len(violations) == before
            del violations[before:]
            if child_ok:
                fail(path, "not")
        return len(violations) == start

    if analysis.accepted:
        _bound_json_value(value)
        try:
            value_encoded = _canonical(value)
        except (TypeError, ValueError):
            fail("$", "non_json_value")
        else:
            if len(value_encoded.encode()) > 65536:
                raise ValueError("structured value exceeds 65536 bytes")
            check(analysis.normalized_schema, value, "$")
    status = str(semantic_status or "").strip().casefold()
    if status not in {"not_requested", "unknown", "passed", "failed"}:
        raise ValueError("semantic_status must be not_requested, unknown, passed, or failed")
    schema_valid = analysis.accepted and not violations
    semantic_valid = status in {"not_requested", "passed"}
    return {
        "schema": STRUCTURED_VALIDATION_SCHEMA,
        "schema_id": analysis.schema_id,
        "schema_accepted": analysis.accepted,
        "schema_valid": schema_valid,
        "semantic_status": status,
        "semantic_valid": semantic_valid,
        "valid": schema_valid and semantic_valid,
        "violations": violations,
        "value_returned": False,
        "validator_executed": False,
        "model_called": False,
        "executes": False,
    }


def plan_structured_retry(plan: Mapping[str, Any], *, failure_kind: str,
                          completed_attempts: int) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise TypeError("plan must be an object")
    if plan.get("schema") != STRUCTURED_GENERATION_SCHEMA:
        raise ValueError("plan must be a structured-generation plan")
    failure = str(failure_kind or "").strip().casefold()
    if failure not in {"cancelled", "provider", "schema", "semantic", "timeout"}:
        raise ValueError("unsupported structured-generation failure kind")
    if (not isinstance(completed_attempts, int) or isinstance(completed_attempts, bool)
            or completed_attempts < 0):
        raise ValueError("completed_attempts must be a non-negative integer")
    retry = plan.get("retry") or {}
    if not isinstance(retry, Mapping):
        raise ValueError("plan retry metadata must be an object")
    budget = retry.get("budget", 0)
    profile_name = str(plan.get("provider_profile") or "")
    if profile_name not in _PROFILES:
        raise ValueError("plan has an unknown provider profile")
    if (not isinstance(budget, int) or isinstance(budget, bool)
            or not 0 <= budget <= 5):
        raise ValueError("plan retry budget is invalid")
    owner = str(retry.get("owner") or "")
    if owner != _PROFILES[profile_name]["retry_owner"]:
        raise ValueError("plan retry owner does not match its provider profile")
    issues = plan.get("issues") or []
    streaming = plan.get("streaming") or {}
    if not isinstance(issues, list) or not isinstance(streaming, Mapping):
        raise ValueError("plan identity metadata is invalid")
    identity = {
        "schema": STRUCTURED_GENERATION_SCHEMA,
        "schema_id": str(plan.get("schema_id") or ""),
        "provider_profile": profile_name,
        "retry_budget": budget,
        "retry_owner": owner,
        "streaming": streaming.get("requested") is True,
        "semantic_validator_ref": str(plan.get("semantic_validator_ref") or ""),
        "latency_budget_ms": plan.get("latency_budget_ms", 0),
        "schema_issues": list(issues),
    }
    expected_plan_id = "structplan_" + hashlib.sha256(
        _canonical(identity).encode()).hexdigest()
    if plan.get("plan_id") != expected_plan_id or plan.get("accepted") is not (not issues):
        raise ValueError("structured-generation plan identity does not verify")
    retryable = failure in {"schema", "semantic"}
    allowed = bool(plan.get("accepted")) and retryable and completed_attempts < budget
    return {
        "schema": "vera.structured-generation-retry-plan/v1",
        "plan_id": plan.get("plan_id", ""),
        "failure_kind": failure,
        "completed_attempts": completed_attempts,
        "budget": budget,
        "retry_owner": owner,
        "allowed": allowed,
        "reason": ("within_budget" if allowed else
                   "cancel_or_timeout_terminal" if failure in {"cancelled", "timeout"} else
                   "failure_not_retryable" if not retryable else "budget_exhausted"),
        "attempt_started": False,
        "model_called": False,
        "executes": False,
    }


def structured_generation_status() -> dict[str, Any]:
    return {
        "schema": STRUCTURED_GENERATION_SCHEMA,
        "portable_keywords": sorted(_KEYWORDS),
        "provider_profiles": [{"id": name, **profile}
                              for name, profile in sorted(_PROFILES.items())],
        "contract": "implemented",
        "deterministic_validation": "implemented",
        "provider_execution": "queued_live",
        "model_execution": "queued_live",
        "optional_providers_imported": False,
        "network_io": False,
        "model_called": False,
        "executes": False,
    }
