"""Explicit code-owned registry; no import paths, plugins, URLs or dynamic loading."""

from app.connectors.contracts import (
    Connector,
    ConnectorCapabilities,
    ConnectorError,
    ConnectorType,
    ErrorCategory,
    OperationSpec,
)
from app.connectors.simulators import (
    ATSSimulator,
    CalendarSimulator,
    CRMSimulator,
    DisabledRestConnector,
    EmailSimulator,
    SlackSimulator,
)


class ConnectorRegistry:
    def __init__(self) -> None:
        self._implementations: dict[ConnectorType, Connector] = {}
        self._names: dict[ConnectorType, str] = {}

    def register(self, kind: ConnectorType, name: str, connector: Connector) -> None:
        if not isinstance(kind, ConnectorType) or kind in self._implementations:
            raise ValueError("Duplicate or unknown connector registration")
        self._implementations[kind], self._names[kind] = connector, name

    def resolve(self, kind: ConnectorType | str) -> Connector:
        try:
            return self._implementations[ConnectorType(kind)]
        except (ValueError, KeyError):
            raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION) from None

    def capabilities(self, kind: ConnectorType | str) -> ConnectorCapabilities:
        connector = self.resolve(kind)
        kind = ConnectorType(kind)
        operations = [
            OperationSpec(
                name=name,
                mutating=name.value.split(".")[-1]
                in {"upsert", "send", "create", "update", "cancel"},
                payload_schema=model.model_json_schema(),
            )
            for name, model in connector.supported_operations().items()
        ]
        return ConnectorCapabilities(
            connector_type=kind,
            name=self._names[kind],
            synthetic=kind != ConnectorType.REST,
            enabled=kind != ConnectorType.REST,
            limitation="No outbound execution; safe DNS-pinned transport deferred"
            if kind == ConnectorType.REST
            else None,
            config_schema=connector.config_model.model_json_schema(),
            operations=operations,
        )

    def catalog(self) -> list[ConnectorCapabilities]:
        return [self.capabilities(kind) for kind in self._implementations]


registry = ConnectorRegistry()
registry.register(ConnectorType.CRM, "HubSpot-style CRM simulator", CRMSimulator())
registry.register(ConnectorType.ATS, "Recruiting ATS simulator", ATSSimulator())
registry.register(ConnectorType.SLACK, "Slack-style notification simulator", SlackSimulator())
registry.register(ConnectorType.EMAIL, "Outbound email simulator", EmailSimulator())
registry.register(ConnectorType.CALENDAR, "Interview calendar simulator", CalendarSimulator())
registry.register(ConnectorType.REST, "Generic REST (disabled)", DisabledRestConnector())
