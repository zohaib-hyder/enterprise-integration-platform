"""Local PostgreSQL adapters. This module imports no networking libraries."""

import hashlib
import json
import uuid
from typing import ClassVar

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors.contracts import (
    BusinessConfig,
    CalendarConfig,
    CandidateData,
    CandidateUpsert,
    ConnectorConfig,
    ConnectorError,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    ContactData,
    ContactUpsert,
    Contract,
    DeliveryOutput,
    EmailConfig,
    EmailSend,
    ErrorCategory,
    EventCreate,
    EventOutput,
    EventPage,
    EventUpdate,
    GetPayload,
    ListPayload,
    OperationName,
    RecordOutput,
    RecordPage,
    RequisitionData,
    RequisitionUpsert,
    RestConfig,
    SearchPayload,
    SlackConfig,
    SlackSend,
)
from app.core.security import utcnow
from app.models.integration import (
    IntegrationConnection,
    SyntheticATSRecord,
    SyntheticCalendarEvent,
    SyntheticCRMRecord,
    SyntheticEmail,
    SyntheticNotification,
)


def fingerprint(data: Contract) -> str:
    return hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class BaseSimulator:
    config_model: type[ConnectorConfig] = BusinessConfig
    connector_type: str
    operations: ClassVar[dict[OperationName, type[Contract]]] = {}

    def validate_config(self, value: dict) -> ConnectorConfig:
        try:
            return self.config_model.model_validate(value)
        except ValidationError:
            raise ConnectorError(ErrorCategory.VALIDATION_FAILED) from None

    def supported_operations(self) -> dict[OperationName, type[Contract]]:
        return dict(self.operations)

    def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        self.validate_config(config.model_dump())
        return ConnectorHealth(
            status="HEALTHY",
            checked_at=utcnow(),
            message="Local PostgreSQL simulator; no provider connection",
        )

    def execute(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        operation: ConnectorOperation,
    ) -> ConnectorResult:
        model = self.operations.get(operation.name)
        if model is None:
            raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
        try:
            payload = model.model_validate(operation.payload)
        except ValidationError:
            raise ConnectorError(ErrorCategory.VALIDATION_FAILED) from None
        connection = db.scalar(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.id == connection_id,
                IntegrationConnection.tenant_id == tenant_id,
                IntegrationConnection.connector_type == self.connector_type,
            )
            .with_for_update()
        )
        if connection is None:
            raise ConnectorError(ErrorCategory.NOT_FOUND)
        if connection.status != "ENABLED":
            raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
        validated = self.validate_config(connection.public_config)
        data = self.perform(db, tenant_id, connection_id, validated, operation.name, payload)
        # Validate outputs even for direct, non-HTTP consumers.
        return ConnectorResult.model_validate({"operation": operation.name, "data": data})

    def perform(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        name: OperationName,
        payload: Contract,
    ) -> RecordOutput | RecordPage | DeliveryOutput | EventOutput | EventPage:
        raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)


class BusinessSimulator(BaseSimulator):
    record_model: type[SyntheticCRMRecord] | type[SyntheticATSRecord]
    person_kind: str
    person_model: type[ContactData] | type[CandidateData]

    def output(self, row: SyntheticCRMRecord | SyntheticATSRecord) -> RecordOutput:
        model = RequisitionData if row.kind == "requisition" else self.person_model
        return RecordOutput(
            id=row.id,
            external_id=row.external_id,
            kind=row.kind,
            data=model.model_validate(row.payload),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def perform(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        name: OperationName,
        payload: Contract,
    ) -> RecordOutput | RecordPage:
        model = self.record_model
        scope = [model.tenant_id == tenant_id, model.connection_id == connection_id]
        if name == OperationName.RECORD_SEARCH:
            if payload.kind not in {self.person_kind, "requisition"}:
                raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
            row = db.scalar(
                select(model).where(
                    *scope, model.kind == payload.kind, model.external_id == payload.external_id
                )
            )
            if row is None:
                raise ConnectorError(ErrorCategory.NOT_FOUND)
            return self.output(row)
        kind, action = name.value.split(".")
        if action == "list":
            total = db.scalar(
                select(func.count()).select_from(model).where(*scope, model.kind == kind)
            )
            rows = db.scalars(
                select(model)
                .where(*scope, model.kind == kind)
                .order_by(model.created_at, model.id)
                .offset(payload.offset)
                .limit(payload.limit)
            )
            return RecordPage(
                items=[self.output(row) for row in rows],
                total=total,
                offset=payload.offset,
                limit=payload.limit,
            )
        if action == "get":
            row = db.scalar(select(model).where(*scope, model.kind == kind, model.id == payload.id))
            if row is None:
                raise ConnectorError(ErrorCategory.NOT_FOUND)
            return self.output(row)
        row = db.scalar(
            select(model).where(
                *scope, model.kind == kind, model.external_id == payload.external_id
            )
        )
        if row is None:
            row = model(
                tenant_id=tenant_id,
                connection_id=connection_id,
                kind=kind,
                external_id=payload.external_id,
                payload=payload.data.model_dump(mode="json"),
            )
            db.add(row)
        else:
            row.payload = payload.data.model_dump(mode="json")
        db.flush()
        return self.output(row)


class CRMSimulator(BusinessSimulator):
    connector_type = "synthetic_crm"
    record_model = SyntheticCRMRecord
    person_kind = "contact"
    person_model = ContactData
    operations: ClassVar[dict[OperationName, type[Contract]]] = {
        OperationName.CONTACT_LIST: ListPayload,
        OperationName.CONTACT_GET: GetPayload,
        OperationName.CONTACT_UPSERT: ContactUpsert,
        OperationName.REQUISITION_LIST: ListPayload,
        OperationName.REQUISITION_GET: GetPayload,
        OperationName.REQUISITION_UPSERT: RequisitionUpsert,
        OperationName.RECORD_SEARCH: SearchPayload,
    }


class ATSSimulator(BusinessSimulator):
    connector_type = "synthetic_ats"
    record_model = SyntheticATSRecord
    person_kind = "candidate"
    person_model = CandidateData
    operations: ClassVar[dict[OperationName, type[Contract]]] = {
        OperationName.CANDIDATE_LIST: ListPayload,
        OperationName.CANDIDATE_GET: GetPayload,
        OperationName.CANDIDATE_UPSERT: CandidateUpsert,
        OperationName.REQUISITION_LIST: ListPayload,
        OperationName.REQUISITION_GET: GetPayload,
        OperationName.REQUISITION_UPSERT: RequisitionUpsert,
        OperationName.RECORD_SEARCH: SearchPayload,
    }


class MessageSimulator(BaseSimulator):
    record_model: type[SyntheticNotification] | type[SyntheticEmail]
    operations: ClassVar[dict[OperationName, type[Contract]]]

    def perform(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        name: OperationName,
        payload: Contract,
    ) -> DeliveryOutput:
        if isinstance(payload, SlackSend):
            if payload.channel not in config.allowed_channels:
                raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
            fields = {"channel": payload.channel, "text": payload.text}
        else:
            if payload.recipient not in config.allowed_recipients:
                raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
            fields = {
                "recipient": payload.recipient,
                "subject": payload.subject,
                "body": payload.body,
            }
        model = self.record_model
        row = db.scalar(
            select(model).where(
                model.tenant_id == tenant_id,
                model.connection_id == connection_id,
                model.idempotency_key == payload.idempotency_key,
            )
        )
        digest = fingerprint(payload)
        duplicate = row is not None
        if row is not None and row.request_hash != digest:
            raise ConnectorError(ErrorCategory.CONFLICT)
        if row is None:
            row = model(
                tenant_id=tenant_id,
                connection_id=connection_id,
                idempotency_key=payload.idempotency_key,
                request_hash=digest,
                **fields,
            )
            db.add(row)
            db.flush()
        return DeliveryOutput(
            id=row.id, delivery_reference=f"simulated:{row.id}", duplicate=duplicate
        )


class SlackSimulator(MessageSimulator):
    connector_type = "synthetic_slack"
    config_model = SlackConfig
    record_model = SyntheticNotification
    operations: ClassVar[dict[OperationName, type[Contract]]] = {
        OperationName.MESSAGE_SEND: SlackSend
    }


class EmailSimulator(MessageSimulator):
    connector_type = "synthetic_email"
    config_model = EmailConfig
    record_model = SyntheticEmail
    operations: ClassVar[dict[OperationName, type[Contract]]] = {
        OperationName.MESSAGE_SEND: EmailSend
    }


def event_output(row: SyntheticCalendarEvent, duplicate: bool = False) -> EventOutput:
    return EventOutput(
        id=row.id,
        calendar=row.calendar,
        title=row.title,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        status=row.status,
        duplicate=duplicate,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class CalendarSimulator(BaseSimulator):
    connector_type = "synthetic_calendar"
    config_model = CalendarConfig
    operations: ClassVar[dict[OperationName, type[Contract]]] = {
        OperationName.EVENT_LIST: ListPayload,
        OperationName.EVENT_CREATE: EventCreate,
        OperationName.EVENT_UPDATE: EventUpdate,
        OperationName.EVENT_CANCEL: GetPayload,
    }

    def perform(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: CalendarConfig,
        name: OperationName,
        payload: Contract,
    ) -> EventOutput | EventPage:
        model = SyntheticCalendarEvent
        scope = [model.tenant_id == tenant_id, model.connection_id == connection_id]
        if name == OperationName.EVENT_LIST:
            total = db.scalar(select(func.count()).select_from(model).where(*scope))
            rows = db.scalars(
                select(model)
                .where(*scope)
                .order_by(model.starts_at, model.id)
                .offset(payload.offset)
                .limit(payload.limit)
            )
            return EventPage(
                items=[event_output(row) for row in rows],
                total=total,
                offset=payload.offset,
                limit=payload.limit,
            )
        row = None
        if name == OperationName.EVENT_CREATE:
            row = db.scalar(
                select(model).where(*scope, model.idempotency_key == payload.idempotency_key)
            )
            if row is not None:
                if row.request_hash != fingerprint(payload):
                    raise ConnectorError(ErrorCategory.CONFLICT)
                return event_output(row, duplicate=True)
        else:
            row = db.scalar(select(model).where(*scope, model.id == payload.id))
            if row is None:
                raise ConnectorError(ErrorCategory.NOT_FOUND)
        if name == OperationName.EVENT_CANCEL:
            row.status = "CANCELLED"
            db.flush()
            return event_output(row)
        if payload.calendar not in config.allowed_calendars:
            raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
        if row is not None and row.status == "CANCELLED":
            raise ConnectorError(ErrorCategory.CONFLICT)
        overlap = db.scalar(
            select(model.id)
            .where(
                *scope,
                model.calendar == payload.calendar,
                model.status == "BOOKED",
                model.starts_at < payload.ends_at,
                model.ends_at > payload.starts_at,
                model.id != row.id if row else True,
            )
            .limit(1)
        )
        if overlap is not None:
            raise ConnectorError(ErrorCategory.CONFLICT)
        if row is None:
            row = model(
                tenant_id=tenant_id,
                connection_id=connection_id,
                calendar=payload.calendar,
                title=payload.title,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
                idempotency_key=payload.idempotency_key,
                request_hash=fingerprint(payload),
            )
            db.add(row)
        else:
            row.calendar, row.title, row.starts_at, row.ends_at = (
                payload.calendar,
                payload.title,
                payload.starts_at,
                payload.ends_at,
            )
        db.flush()
        return event_output(row)


class DisabledRestConnector(BaseSimulator):
    connector_type = "generic_rest"
    config_model = RestConfig
    operations: ClassVar[dict[OperationName, type[Contract]]] = {}

    def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        return ConnectorHealth(
            status="DISABLED",
            synthetic=False,
            checked_at=utcnow(),
            message="Outbound REST disabled: DNS pinning and safe transport are not implemented",
        )

    def execute(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        operation: ConnectorOperation,
    ) -> ConnectorResult:
        # Deliberately fails before touching DNS, sockets, HTTP clients or credentials.
        raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
