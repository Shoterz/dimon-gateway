import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apps.dimon_live.conversation import (
    ConversationAdapter,
    ConversationInput,
    ConversationRejected,
    ConversationService,
    ConversationStatus,
    ConversationTransition,
    InputKind,
    build_adapter_registry,
)
from apps.dimon_live.conversation_store import ConversationStore


NOW = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


class StubAdapter:
    domain = "TEST"
    name = "test"
    version = 1

    def start(self, context):
        raise NotImplementedError

    def apply_turn(self, state_json, conversation_input, context):
        raise NotImplementedError

    def render_public(self, state_json):
        return json.loads(state_json)


class LifecycleAdapter(StubAdapter):
    def start(self, context):
        return _transition(step="start")

    def apply_turn(self, state_json, conversation_input, context):
        if conversation_input.text == "explode":
            raise RuntimeError("adapter exploded")
        if conversation_input.choice == "CANCEL":
            return _transition(
                status=ConversationStatus.CANCELLED,
                step="cancelled",
                question=None,
                allowed_choices=(),
                outcome="CANCELLED",
            )
        if conversation_input.text == "reask":
            return _transition(step="start", advance_version=False, outcome="REASKED")
        if conversation_input.text == "ready":
            return _transition(
                status=ConversationStatus.READY_FOR_DRAFT,
                step="ready",
                question=None,
                allowed_choices=(),
                outcome="READY_FOR_DRAFT",
            )
        if conversation_input.text == "cancel":
            return _transition(
                status=ConversationStatus.CANCELLED,
                step="cancelled",
                question=None,
                allowed_choices=(),
                outcome="CANCELLED",
            )
        return _transition(step="next")


class ConversationContractTests(unittest.TestCase):
    def test_text_input_requires_nonblank_bounded_text(self):
        with self.assertRaises(ConversationRejected):
            ConversationInput(kind=InputKind.TEXT, text="   ")

        with self.assertRaises(ConversationRejected):
            ConversationInput(kind=InputKind.TEXT, text="x" * 501)

        value = ConversationInput(kind=InputKind.TEXT, text="  buy it now  ")
        self.assertEqual(value.text, "buy it now")
        self.assertEqual(json.loads(value.canonical_json())["text"], "buy it now")

    def test_choice_input_requires_name_and_json_parameters(self):
        with self.assertRaises(ConversationRejected):
            ConversationInput(kind=InputKind.CHOICE, choice="buy-now")

        with self.assertRaises(ConversationRejected):
            ConversationInput(kind=InputKind.CHOICE, choice="BUY_NOW", parameters={"bad": {1, 2}})

        value = ConversationInput(
            kind=InputKind.CHOICE,
            choice="BUY_NOW",
            parameters={"source": "button"},
        )
        self.assertEqual(value.choice, "BUY_NOW")

    def test_transition_rejects_execution_or_approval_fields(self):
        with self.assertRaises(ConversationRejected):
            ConversationTransition(
                state={"execution": {"approved": True}},
                status=ConversationStatus.READY_FOR_DRAFT,
                outcome="READY",
                answer="Ready.",
                question=None,
                allowed_choices=(),
                public_view={},
            )

    def test_thread_public_envelope_omits_state_and_handler_internals(self):
        thread = _thread()
        public = thread.public_envelope()
        self.assertEqual(
            set(public),
            {
                "conversation_id",
                "domain",
                "status",
                "version",
                "question",
                "allowed_choices",
                "expires_at",
            },
        )
        self.assertNotIn("state_json", public)
        self.assertNotIn("handler_name", public)

    def test_adapter_registry_rejects_duplicate_domain_registration(self):
        first = StubAdapter()
        second = StubAdapter()
        with self.assertRaises(ConversationRejected):
            build_adapter_registry((first, second))

    def test_conversation_input_rejects_canonical_payload_over_4096_bytes(self):
        with self.assertRaises(ConversationRejected):
            ConversationInput(
                kind=InputKind.TEXT,
                text="ok",
                parameters={"padding": "x" * 4090},
            )

    def test_start_creates_public_waiting_thread_with_fifteen_minute_expiry(self):
        with self._service() as (service, _):
            result = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )

        self.assertEqual(result["conversation"]["status"], "WAITING")
        self.assertEqual(result["conversation"]["version"], 1)
        self.assertEqual(result["trace"][0]["stage"], "CONVERSATION_STARTED")
        self.assertNotIn("state_json", result["conversation"])
        self.assertEqual(
            datetime.fromisoformat(result["conversation"]["expires_at"]),
            NOW + timedelta(minutes=15),
        )

    def test_apply_turn_updates_state_and_resets_expiry(self):
        with self._service() as (service, _):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            result = service.apply_turn(
                started["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
                request_id="request-1",
                expected_version=1,
                source="typed",
                conversation_input=ConversationInput(InputKind.TEXT, text="next"),
            )

        self.assertEqual(result["conversation"]["version"], 2)
        self.assertEqual(result["trace"][0]["stage"], "CONVERSATION_TURN_APPLIED")
        self.assertEqual(result["public_view"]["step"], "next")

    def test_reasked_turn_keeps_version_and_expiry(self):
        with self._service() as (service, _):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            result = service.apply_turn(
                started["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
                request_id="request-1",
                expected_version=1,
                source="typed",
                conversation_input=ConversationInput(InputKind.TEXT, text="reask"),
            )

        self.assertEqual(result["conversation"]["version"], 1)
        self.assertEqual(result["trace"][0]["stage"], "CONVERSATION_REASKED")
        self.assertEqual(
            result["conversation"]["expires_at"], started["conversation"]["expires_at"]
        )

    def test_terminal_thread_rejects_another_turn(self):
        with self._service() as (service, _):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            ready = service.apply_turn(
                started["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
                request_id="request-1",
                expected_version=1,
                source="typed",
                conversation_input=ConversationInput(InputKind.TEXT, text="ready"),
            )
            with self.assertRaises(ConversationRejected) as context:
                service.apply_turn(
                    ready["conversation"]["conversation_id"],
                    session_scope="session-a",
                    user_id="demo-user",
                    request_id="request-2",
                    expected_version=2,
                    source="typed",
                    conversation_input=ConversationInput(InputKind.TEXT, text="next"),
                )

        self.assertEqual(context.exception.reason_code, "CONVERSATION_TERMINAL")

    def test_adapter_exception_leaves_thread_unchanged(self):
        with self._service() as (service, _):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            with self.assertRaises(ConversationRejected) as context:
                service.apply_turn(
                    started["conversation"]["conversation_id"],
                    session_scope="session-a",
                    user_id="demo-user",
                    request_id="request-1",
                    expected_version=1,
                    source="typed",
                    conversation_input=ConversationInput(InputKind.TEXT, text="explode"),
                )
            current = service.get(
                started["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
            )

        self.assertEqual(context.exception.reason_code, "CONVERSATION_HANDLER_FAILED")
        self.assertEqual(current["conversation"]["version"], 1)

    def test_missing_and_mismatched_handler_versions_fail_closed(self):
        with self._service() as (service, store):
            with self.assertRaises(ConversationRejected) as context:
                service.start(
                    domain="MISSING", session_scope="session-a", user_id="demo-user", payload={}
                )
            self.assertEqual(context.exception.reason_code, "CONVERSATION_DOMAIN_UNAVAILABLE")
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "UPDATE conversation_threads SET handler_version = 99 WHERE conversation_id = ?",
                    (started["conversation"]["conversation_id"],),
                )
                connection.commit()
            with self.assertRaises(ConversationRejected) as context:
                service.get(
                    started["conversation"]["conversation_id"],
                    session_scope="session-a",
                    user_id="demo-user",
                )

        self.assertEqual(context.exception.reason_code, "CONVERSATION_HANDLER_VERSION_UNAVAILABLE")

    def test_two_service_instances_cannot_commit_same_expected_version(self):
        with self._service() as (service, store):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            services = [
                ConversationService(store, build_adapter_registry((LifecycleAdapter(),)), lambda: NOW),
                ConversationService(store, build_adapter_registry((LifecycleAdapter(),)), lambda: NOW),
            ]

            def apply(instance, value):
                try:
                    return instance.apply_turn(
                        started["conversation"]["conversation_id"],
                        session_scope="session-a",
                        user_id="demo-user",
                        request_id=f"request-{value}",
                        expected_version=1,
                        source="typed",
                        conversation_input=ConversationInput(InputKind.TEXT, text=value),
                    )
                except ConversationRejected as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(apply, services, ("one", "two")))

        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        conflicts = [result for result in results if isinstance(result, ConversationRejected)]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].reason_code, "CONVERSATION_VERSION_CONFLICT")

    def test_public_results_use_bounded_conversation_trace_stages(self):
        with self._service() as (service, _):
            started = service.start(
                domain="TEST", session_scope="session-a", user_id="demo-user", payload={}
            )
            reasked = service.apply_turn(
                started["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
                request_id="request-1",
                expected_version=1,
                source="typed",
                conversation_input=ConversationInput(InputKind.TEXT, text="reask"),
            )
            cancelled = service.apply_turn(
                reasked["conversation"]["conversation_id"],
                session_scope="session-a",
                user_id="demo-user",
                request_id="request-2",
                expected_version=1,
                source="button",
                conversation_input=ConversationInput(
                    InputKind.CHOICE, choice="CANCEL"
                ),
            )

        self.assertEqual(reasked["trace"][0]["stage"], "CONVERSATION_REASKED")
        self.assertEqual(cancelled["trace"][0]["stage"], "CONVERSATION_CANCELLED")
        self.assertNotIn("state_json", cancelled)

    def _service(self):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "conversation.sqlite3"
        from apps.dimon_live.database import Database

        database = Database(path, clock=lambda: NOW)
        database.initialize()
        store = ConversationStore(path, clock=lambda: NOW)
        service = ConversationService(
            store,
            build_adapter_registry((LifecycleAdapter(),)),
            lambda: NOW,
        )
        return _ServiceContext(temporary, service, store)


def _thread():
    from apps.dimon_live.conversation import ConversationThread

    return ConversationThread(
        conversation_id="conversation_" + "a" * 32,
        session_scope="session-a",
        user_id="demo-user",
        domain="TEST",
        handler_name="test",
        handler_version=1,
        status=ConversationStatus.WAITING,
        version=1,
        state_json='{"secret":"never return"}',
        last_response_json=json.dumps(
            {
                "question": "Choose.",
                "allowed_choices": ["YES"],
            }
        ),
        created_at=NOW,
        updated_at=NOW,
        expires_at=datetime(2026, 10, 5, 8, 15, tzinfo=timezone.utc),
    )


def _transition(
    *,
    status=ConversationStatus.WAITING,
    step="start",
    answer="Choose.",
    question="What next?",
    allowed_choices=("NEXT", "CANCEL"),
    outcome="APPLIED",
    advance_version=True,
):
    return ConversationTransition(
        state={"step": step},
        status=status,
        outcome=outcome,
        answer=answer,
        question=question,
        allowed_choices=allowed_choices,
        public_view={"step": step},
        advance_version=advance_version,
    )


class _ServiceContext:
    def __init__(self, temporary, service, store):
        self.temporary = temporary
        self.service = service
        self.store = store

    def __enter__(self):
        return self.service, self.store

    def __exit__(self, exc_type, exc_value, traceback):
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
