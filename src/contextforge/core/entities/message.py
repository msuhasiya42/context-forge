"""Message entity (implementation plan section 3.3): one record type, shape selected by `kind`."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from contextforge.core.entities import Entity
from contextforge.core.entities.task import Confidence
from contextforge.core.values import ArtifactRef, EvidenceRef


class Sender(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageKind(StrEnum):
    REQUEST = "request"
    CLARIFICATION = "clarification"
    CLARIFICATION_RESPONSE = "clarification_response"
    PROPOSED_ACTION = "proposed_action"
    ACTION_RESPONSE = "action_response"
    REPORT = "report"
    TEXT = "text"


class RequestStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Claim(Entity):
    """One report claim. CONFIRMED needs two independent sources and no contradiction."""

    claim_id: str
    text: str
    evidence_refs: list[EvidenceRef]
    status: Literal["CONFIRMED", "REJECTED", "UNKNOWN"] = "UNKNOWN"
    # Numeric value code re-reads at the first evidence locator and compares before writing.
    value: float | None = None


class ReportConclusion(Entity):
    """A judgement, comparison or recommendation of the report (F10 ruling 1), resting on the
    claims ``based_on`` names by claim id. It cites no evidence of its own, so it is never
    shown with a source link."""

    text: str
    based_on: list[str]
    confidence: Confidence


class ReportConstraint(Entity):
    """A constraint the request ran under, carried into the next request's prior context."""

    text: str
    # Where it came from: "request" (the request text) or "clarification" (the user's answer to
    # a question). Standing instructions reach the model through the memory file, not here.
    source: str
    date: str


class ReportBody(Entity):
    claims: list[Claim]
    # The assessment drawn from the claims; reports saved before F10 have none.
    conclusions: list[ReportConclusion] = []
    inputs_consulted: list[ArtifactRef]
    deliverables: list[ArtifactRef] = []
    uncertainties: list[str]
    constraints: list[ReportConstraint] = []
    coverage: dict[str, Any]


class ActionPayload(Entity):
    """The tool call exactly as shown to the user; changed arguments mean a new Message."""

    tool: str
    arguments: dict[str, Any]
    account: str | None


class _MessageFields(Entity):
    """Fields every Message kind carries."""

    message_id: str
    chat_id: str
    user_id: str
    sender: Sender
    content: str
    created_at: str
    updated_at: str


class _RequestEntryFields(_MessageFields):
    """Every kind but ``request`` belongs to a request."""

    request_message_id: str


class _ReplyFields(_RequestEntryFields):
    """Kinds that answer one exact earlier entry; a request can raise several."""

    in_reply_to_message_id: str


class RequestMessage(_MessageFields):
    kind: Literal[MessageKind.REQUEST]
    status: RequestStatus
    failure_reason: str | None = None


class ClarificationMessage(_RequestEntryFields):
    kind: Literal[MessageKind.CLARIFICATION]


class ClarificationResponseMessage(_ReplyFields):
    kind: Literal[MessageKind.CLARIFICATION_RESPONSE]


class ProposedActionMessage(_RequestEntryFields):
    kind: Literal[MessageKind.PROPOSED_ACTION]
    payload: ActionPayload
    status: ActionStatus
    failure_reason: str | None = None


class ActionResponseMessage(_ReplyFields):
    kind: Literal[MessageKind.ACTION_RESPONSE]
    decision: Decision


class ReportMessage(_RequestEntryFields):
    kind: Literal[MessageKind.REPORT]
    report: ReportBody


class TextMessage(_RequestEntryFields):
    kind: Literal[MessageKind.TEXT]


Message = Annotated[
    RequestMessage
    | ClarificationMessage
    | ClarificationResponseMessage
    | ProposedActionMessage
    | ActionResponseMessage
    | ReportMessage
    | TextMessage,
    Field(discriminator="kind"),
]

MessageAdapter: TypeAdapter[Message] = TypeAdapter(Message)
