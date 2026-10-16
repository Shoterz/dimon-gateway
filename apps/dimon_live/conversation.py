"""Domain-neutral contracts for server-owned conversational workflows."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Protocol, TypeAlias

if TYPE_CHECKING:
    from .conversation_store import ConversationStore


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_CHOICE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FORBIDDEN_STATE_KEYS = frozenset(
    {"receipt", "approval", "execution", "ledger_mutation"}
)
_MAX_INPUT_BYTES = 4_096


class ConversationRejected(RuntimeError):
    """Raised when a conversation contract or transition is unsafe."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class ConversationStatus(str, Enum):
    WAITING = "WAITING"
    READY_FOR_DRAFT = "READY_FOR_DRAFT"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class InputKind(str, Enum):
    TEXT = "TEXT"
    CHOICE = "CHOICE"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConversationRejected("CONVERSATION_INPUT_INVALID") from exc


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_STATE_KEYS:
                return True
            if _contains_forbidden_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


@dataclass(frozen=True)
class ConversationInput:
    kind: InputKind
    text: str | None = None
    choice: str | None = None
    parameters: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, InputKind):
            raise ConversationRejected("CONVERSATION_INPUT_INVALID")
        if not isinstance(self.parameters, Mapping):
            raise ConversationRejected("CONVERSATION_INPUT_INVALID")
        if len(self.parameters) > 16:
            raise ConversationRejected("CONVERSATION_INPUT_INVALID")
        if self.kind is InputKind.TEXT:
            if not isinstance(self.text, str) or not self.text.strip() or self.choice is not None:
                raise ConversationRejected("CONVERSATION_INPUT_INVALID")
            normalized = self.text.strip()
            if len(normalized) > 500:
                raise ConversationRejected("CONVERSATION_INPUT_INVALID")
            object.__setattr__(self, "text", normalized)
        else:
            if self.text is not None or not isinstance(self.choice, str):
                raise ConversationRejected("CONVERSATION_INPUT_INVALID")
            choice = self.choice.strip()
            if not _CHOICE_NAME.fullmatch(choice):
                raise ConversationRejected("CONVERSATION_INPUT_INVALID")
            object.__setattr__(self, "choice", choice)
        canonical = self.canonical_json()
        if len(canonical.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ConversationRejected("CONVERSATION_INPUT_TOO_LARGE")

    def canonical_json(self) -> str:
        value: dict[str, object] = {"kind": self.kind.value}
        if self.kind is InputKind.TEXT:
            value["text"] = self.text
        else:
            value["choice"] = self.choice
        if self.parameters:
            value["parameters"] = dict(self.parameters)
        return _canonical_json(value)


@dataclass(frozen=True)
class ConversationThread:
    conversation_id: str
    session_scope: str
    user_id: str
    domain: str
    handler_name: str
    handler_version: int
    status: ConversationStatus
    version: int
    state_json: str
    last_response_json: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def public_envelope(self) -> dict[str, JSONValue]:
        try:
            response = json.loads(self.last_response_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConversationRejected("CONVERSATION_STATE_INVALID") from exc
        if not isinstance(response, dict):
            raise ConversationRejected("CONVERSATION_STATE_INVALID")
        return {
            "conversation_id": self.conversation_id,
            "domain": self.domain,
            "status": self.status.value,
            "version": self.version,
            "question": response.get("question"),
            "allowed_choices": response.get("allowed_choices", []),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    conversation_id: str
    request_id: str
    source: str
    input_kind: str
    input_json: str
    before_version: int
    after_version: int
    outcome: str
    response_json: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationTransition:
    state: Mapping[str, JSONValue]
    status: ConversationStatus
    outcome: str
    answer: str
    question: str | None
    allowed_choices: tuple[str, ...]
    public_view: Mapping[str, JSONValue]
    advance_version: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.status, ConversationStatus):
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        if not isinstance(self.answer, str) or len(self.answer) > 2_000:
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        if self.question is not None and (
            not isinstance(self.question, str) or len(self.question) > 500
        ):
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        if any(
            not isinstance(choice, str) or not _CHOICE_NAME.fullmatch(choice)
            for choice in self.allowed_choices
        ):
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        if _contains_forbidden_key(self.state):
            raise ConversationRejected("CONVERSATION_TRANSITION_EXECUTION_FORBIDDEN")
        _canonical_json(self.state)
        _canonical_json(self.public_view)

    def response_json(self) -> str:
        return _canonical_json(
            {
                "answer": self.answer,
                "question": self.question,
                "allowed_choices": list(self.allowed_choices),
                "public_view": self.public_view,
            }
        )


@dataclass(frozen=True)
class StartContext:
    now: datetime
    payload: object


@dataclass(frozen=True)
class TurnContext:
    now: datetime
    session_scope: str
    user_id: str


class ConversationAdapter(Protocol):
    domain: str
    name: str
    version: int

    def start(self, context: StartContext) -> ConversationTransition: ...

    def apply_turn(
        self,
        state_json: str,
        conversation_input: ConversationInput,
        context: TurnContext,
    ) -> ConversationTransition: ...

    def render_public(self, state_json: str) -> dict[str, JSONValue]: ...


def build_adapter_registry(
    adapters: Iterable[ConversationAdapter],
) -> Mapping[str, ConversationAdapter]:
    registry: dict[str, ConversationAdapter] = {}
    for adapter in adapters:
        domain = getattr(adapter, "domain", None)
        name = getattr(adapter, "name", None)
        version = getattr(adapter, "version", None)
        if (
            not isinstance(domain, str)
            or not domain
            or not isinstance(name, str)
            or not name
            or not isinstance(version, int)
            or version < 1
        ):
            raise ConversationRejected("CONVERSATION_ADAPTER_INVALID")
        if domain in registry:
            raise ConversationRejected("CONVERSATION_ADAPTER_DUPLICATE")
        registry[domain] = adapter
    return MappingProxyType(registry)


class ConversationService:
    """Coordinates persisted turns without knowing domain-specific state."""

    def __init__(
        self,
        store: "ConversationStore",
        adapters: Mapping[str, ConversationAdapter],
        clock: Callable[[], datetime],
        ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("conversation TTL must be positive")
        self.store = store
        self.adapters = adapters
        self.clock = clock
        self.ttl = ttl

    def start(
        self,
        *,
        domain: str,
        session_scope: str,
        user_id: str,
        payload: object,
    ) -> dict[str, JSONValue]:
        adapter = self._adapter(domain)
        now = self.clock()
        try:
            transition = adapter.start(StartContext(now=now, payload=payload))
        except ConversationRejected:
            raise
        except Exception as exc:
            raise ConversationRejected("CONVERSATION_HANDLER_FAILED") from exc
        if not isinstance(transition, ConversationTransition):
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        self._validate_start_transition(transition)
        thread, superseded = self.store.create_thread(
            session_scope=session_scope,
            user_id=user_id,
            domain=adapter.domain,
            handler_name=adapter.name,
            handler_version=adapter.version,
            transition=transition,
            expires_at=now + self.ttl,
        )
        trace = []
        if superseded:
            trace.append({"stage": "CONVERSATION_SUPERSEDED"})
        trace.append({"stage": "CONVERSATION_STARTED"})
        return self._result(thread, trace=trace)

    def get(
        self,
        conversation_id: str,
        *,
        session_scope: str,
        user_id: str,
    ) -> dict[str, JSONValue]:
        thread = self.store.get_thread(conversation_id, session_scope, user_id)
        adapter = self._adapter_for_thread(thread)
        try:
            public_view = adapter.render_public(thread.state_json)
        except ConversationRejected:
            raise
        except Exception as exc:
            raise ConversationRejected("CONVERSATION_STATE_INVALID") from exc
        result = self._result(thread, trace=[{"stage": "CONVERSATION_RESUMED"}])
        result["public_view"] = public_view
        return result

    def apply_turn(
        self,
        conversation_id: str,
        *,
        session_scope: str,
        user_id: str,
        request_id: str,
        expected_version: int,
        source: str,
        conversation_input: ConversationInput,
    ) -> dict[str, JSONValue]:
        thread = self.store.get_thread(conversation_id, session_scope, user_id)
        existing_turn = self.store.get_turn(conversation_id, request_id)
        if existing_turn is not None:
            if (
                existing_turn.input_json != conversation_input.canonical_json()
                or existing_turn.source != source
            ):
                raise ConversationRejected("CONVERSATION_IDEMPOTENCY_CONFLICT")
            return self._result_from_response(
                thread,
                existing_turn.response_json,
                trace=[{"stage": "CONVERSATION_TURN_REPLAYED"}],
                idempotent_replay=True,
            )
        if thread.version != expected_version:
            raise ConversationRejected("CONVERSATION_VERSION_CONFLICT")
        if thread.status is not ConversationStatus.WAITING:
            raise ConversationRejected("CONVERSATION_TERMINAL")
        adapter = self._adapter_for_thread(thread)
        try:
            transition = adapter.apply_turn(
                thread.state_json,
                conversation_input,
                TurnContext(
                    now=self.clock(),
                    session_scope=session_scope,
                    user_id=user_id,
                ),
            )
        except ConversationRejected:
            raise
        except Exception as exc:
            raise ConversationRejected("CONVERSATION_HANDLER_FAILED") from exc
        if not isinstance(transition, ConversationTransition):
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        self._validate_turn_transition(thread, transition)
        updated, turn, replay = self.store.commit_turn(
            thread=thread,
            request_id=request_id,
            source=source,
            conversation_input=conversation_input,
            transition=transition,
            expires_at=self.clock() + self.ttl,
        )
        if replay:
            return self._result_from_response(
                updated,
                turn.response_json,
                trace=[{"stage": "CONVERSATION_TURN_REPLAYED"}],
                idempotent_replay=True,
            )
        if not transition.advance_version:
            stage = "CONVERSATION_REASKED"
        elif transition.status is ConversationStatus.CANCELLED:
            stage = "CONVERSATION_CANCELLED"
        elif transition.status is ConversationStatus.READY_FOR_DRAFT:
            stage = "CONVERSATION_READY_FOR_DRAFT"
        else:
            stage = "CONVERSATION_TURN_APPLIED"
        return self._result(updated, trace=[{"stage": stage}])

    def _adapter(self, domain: str) -> ConversationAdapter:
        adapter = self.adapters.get(domain)
        if adapter is None:
            raise ConversationRejected("CONVERSATION_DOMAIN_UNAVAILABLE")
        return adapter

    def _adapter_for_thread(self, thread: ConversationThread) -> ConversationAdapter:
        adapter = self._adapter(thread.domain)
        if adapter.name != thread.handler_name or adapter.version != thread.handler_version:
            raise ConversationRejected("CONVERSATION_HANDLER_VERSION_UNAVAILABLE")
        return adapter

    @staticmethod
    def _validate_start_transition(transition: ConversationTransition) -> None:
        if transition.status not in {
            ConversationStatus.WAITING,
            ConversationStatus.READY_FOR_DRAFT,
        }:
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")

    @staticmethod
    def _validate_turn_transition(
        thread: ConversationThread,
        transition: ConversationTransition,
    ) -> None:
        if transition.advance_version:
            allowed = {
                ConversationStatus.WAITING,
                ConversationStatus.READY_FOR_DRAFT,
                ConversationStatus.CANCELLED,
            }
            if transition.status not in allowed:
                raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")
        elif transition.status is not thread.status:
            raise ConversationRejected("CONVERSATION_TRANSITION_INVALID")

    def _result(
        self,
        thread: ConversationThread,
        *,
        trace: list[dict[str, str]],
        idempotent_replay: bool = False,
    ) -> dict[str, JSONValue]:
        return self._result_from_response(
            thread,
            thread.last_response_json,
            trace=trace,
            idempotent_replay=idempotent_replay,
        )

    @staticmethod
    def _result_from_response(
        thread: ConversationThread,
        response_json: str,
        *,
        trace: list[dict[str, str]],
        idempotent_replay: bool = False,
    ) -> dict[str, JSONValue]:
        try:
            response = json.loads(response_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConversationRejected("CONVERSATION_STATE_INVALID") from exc
        if not isinstance(response, dict):
            raise ConversationRejected("CONVERSATION_STATE_INVALID")
        return {
            "outcome": "ANSWERED",
            "reason_code": "CONVERSATION_TURN_APPLIED",
            "answer": response.get("answer", ""),
            "question": response.get("question"),
            "allowed_choices": response.get("allowed_choices", []),
            "public_view": response.get("public_view", {}),
            "conversation": thread.public_envelope(),
            "trace": trace,
            "idempotent_replay": idempotent_replay,
        }
