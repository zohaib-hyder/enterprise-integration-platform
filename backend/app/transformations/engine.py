"""Pure deterministic field operations. No expressions, imports or runtime code dispatch."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, get_args, get_origin

from pydantic import ValidationError

from app.schemas.canonical import (
    ENTITY_MODELS,
    SOURCE_MODELS,
    SOURCE_PATHS,
    MappingDefinition,
    ValidationIssue,
)


class TransformationFailure(Exception):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("Transformation input rejected")


def fail(category: str, field: str | None = None) -> None:
    raise TransformationFailure([ValidationIssue(category=category, field=field)])


def enum_values(annotation) -> set:
    if get_origin(annotation) is Literal:
        return set(get_args(annotation))
    return set().union(*(enum_values(part) for part in get_args(annotation)))


def validate_definition(definition: MappingDefinition, source_kind: str, entity_type: str) -> None:
    targets = ENTITY_MODELS[entity_type].model_fields
    seen = set()
    for rule in definition.fields:
        if (
            rule.target not in targets
            or rule.target in seen
            or rule.source is not None
            and rule.source not in SOURCE_PATHS[source_kind]
        ):
            fail("INVALID_MAPPING")
        seen.add(rule.target)
        allowed = enum_values(targets[rule.target].annotation)
        for operation in rule.steps:
            if (
                operation.operation == "enum"
                and allowed
                and any(value not in allowed for value in operation.values.values())
            ):
                fail("INVALID_MAPPING", rule.target)
        if rule.source is None and rule.default is None:
            fail("INVALID_MAPPING", rule.target)
    if any(field.is_required() and name not in seen for name, field in targets.items()):
        fail("INVALID_MAPPING")
    if len(json.dumps(definition.model_dump(mode="json"))) > 12000:
        fail("INVALID_MAPPING")


def template(source_type: str, kind: str) -> MappingDefinition:
    def field(
        target: str, source: str, required: bool = False, operations: list[str] | None = None
    ) -> dict:
        return {
            "target": target,
            "source": "data." + source,
            "required": required,
            "null_handling": "reject" if required else "omit",
            "steps": [{"operation": op} for op in (operations or [])],
        }

    if kind == "contact":
        fields = [
            field("name", "name", True, ["trim"]),
            field("email", "email", True, ["email"]),
            field("company", "company", True, ["trim"]),
            field("last_contacted_at", "last_contacted_at", False, ["datetime"]),
        ]
    elif kind == "candidate":
        fields = [
            field("name", "name", True, ["trim"]),
            field("email", "email", True, ["email"]),
            field("stage", "stage", True),
            field("skills", "skills"),
            field("years_experience", "years_experience", False, ["number"]),
            field("available_at", "available_at", False, ["datetime"]),
        ]
        fields[2]["steps"] = [
            {
                "operation": "enum",
                "values": {
                    "NEW": "NEW",
                    "SCREENING": "SCREENING",
                    "SUBMITTED": "SUBMITTED",
                    "PLACED": "PLACED",
                    "REJECTED": "REJECTED",
                },
            }
        ]
    else:
        fields = [
            field("title", "title", True, ["trim"]),
            field("department", "department", True, ["trim"]),
            field("location", "location", True, ["trim"]),
            field(
                "status" if source_type == "synthetic_crm" else "recruiting_status", "status", True
            ),
            field("commercial_rate", "commercial_rate", False, ["number"]),
        ]
        fields[3]["steps"] = [
            {
                "operation": "enum",
                "values": {"OPEN": "OPEN", "CLOSED": "CLOSED", "PAUSED": "PAUSED"},
            }
        ]
    return MappingDefinition.model_validate({"schema_version": 1, "fields": fields})


def source_payload(kind: str, value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"data"}:
        fail("INVALID_SOURCE")
    try:
        if len(json.dumps(value, allow_nan=False)) > 16000:
            fail("INVALID_SOURCE")
        data = SOURCE_MODELS[kind].model_validate(value["data"])
        return {"data": data.model_dump(mode="json", exclude_unset=True)}
    except (ValidationError, ValueError, TypeError, RecursionError):
        fail("INVALID_SOURCE")


def step(value: object, operation: str, parameters) -> object:
    if operation in {"trim", "email"}:
        if not isinstance(value, str):
            raise ValueError
        return value.strip().lower() if operation == "email" else value.strip()
    if operation == "enum":
        if not isinstance(value, str) or value not in parameters.values:
            raise ValueError
        return parameters.values[value]
    if operation == "datetime":
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    if operation == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (str, int, float))
            or len(str(value)) > 40
        ):
            raise ValueError
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or not -6 <= number.as_tuple().exponent <= 7
            or abs(number) > 10000000
        ):
            raise ValueError
        if parameters.numeric_type == "integer":
            if number != number.to_integral_value():
                raise ValueError
            return int(number)
        return format(number, "f")
    if operation == "string":
        if (
            not isinstance(value, (str, int, float, bool))
            or len(str(value)) > parameters.max_length
        ):
            raise ValueError
        return str(value)
    raise ValueError


def transform(
    definition: MappingDefinition, kind: str, entity_type: str, payload: dict
) -> tuple[dict, dict]:
    validate_definition(definition, kind, entity_type)
    source = source_payload(kind, payload)
    output, issues = {}, []
    for rule in definition.fields:
        value = source
        for part in rule.source.split(".") if rule.source else []:
            value = value.get(part) if isinstance(value, dict) else None
        if rule.source is None:
            value = rule.default
        if value is None:
            if rule.null_handling == "default":
                value = rule.default
            elif rule.required or rule.null_handling == "reject":
                issues.append(ValidationIssue(category="MISSING_FIELD", field=rule.target))
                continue
            elif rule.null_handling == "omit":
                continue
        try:
            for operation in rule.steps:
                if value is not None:
                    value = step(value, operation.operation, operation)
            output[rule.target] = value
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            issues.append(ValidationIssue(category="INVALID_VALUE", field=rule.target))
    if issues:
        raise TransformationFailure(issues)
    try:
        canonical = ENTITY_MODELS[entity_type].model_validate(output)
        return canonical.model_dump(mode="json", exclude_unset=True), source
    except ValidationError as error:
        allowed = ENTITY_MODELS[entity_type].model_fields
        issues = [
            ValidationIssue(
                category="CANONICAL_VALIDATION",
                field=str(item["loc"][0])
                if item["loc"] and str(item["loc"][0]) in allowed
                else None,
            )
            for item in error.errors(include_input=False, include_context=False)[:20]
        ]
        raise TransformationFailure(issues) from None
