import concurrent.futures
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apps.dimon_live.conversation import (
    ConversationInput,
    ConversationRejected,
    ConversationStatus,
    ConversationTransition,
    InputKind,
)
from apps.dimon_live.conversation_store import ConversationStore
from apps.dimon_live.database import Database


NOW = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def transition(
    status=ConversationStatus.WAITING,
    *,
    answer="Choose.",
    question="What next?",
    step="one",
    advance_version=True,
):
    return ConversationTransition(
        state={"step": step},
        status=status,
        outcome="APPLIED",
        answer=answer,
        question=question,
        allowed_choices=("NEXT", "CANCEL"),
        public_view={"step": step},
        advance_version=advance_version,
    )


def text_input(value):
    return ConversationInput(kind=InputKind.TEXT, text=value)


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "demo.sqlite3"
        self.database = Database(self.path, clock=lambda: NOW)
        self.database.initialize()
        self.store = ConversationStore(self.path, clock=lambda: NOW)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_initialize_creates_conversation_tables(self):
        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("conversation_threads", tables)
        self.assertIn("conversation_turns", tables)

    def test_starting_thread_supersedes_waiting_and_ready_thread_in_same_domain(self):
        waiting, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(step="waiting"),
            expires_at=NOW + timedelta(minutes=15),
        )
        ready, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(
                ConversationStatus.READY_FOR_DRAFT,
                step="ready",
            ),
            expires_at=NOW + timedelta(minutes=15),
        )
        newest, superseded = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(step="new"),
            expires_at=NOW + timedelta(minutes=15),
        )

        self.assertTrue(superseded)
        self.assertEqual(
            self._status(waiting.conversation_id), ConversationStatus.SUPERSEDED.value
        )
        self.assertEqual(
            self._status(ready.conversation_id), ConversationStatus.SUPERSEDED.value
        )
        self.assertEqual(newest.status, ConversationStatus.WAITING)

    def test_threads_in_other_domains_are_not_superseded(self):
        payment, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="PAYMENT",
            handler_name="payment",
            handler_version=1,
            transition=transition(step="payment"),
            expires_at=NOW + timedelta(minutes=15),
        )
        self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(step="investment"),
            expires_at=NOW + timedelta(minutes=15),
        )

        self.assertEqual(
            self._status(payment.conversation_id), ConversationStatus.WAITING.value
        )

    def test_cross_session_and_cross_user_reads_return_not_found(self):
        thread, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=15),
        )

        for session_scope, user_id in (("session-b", "demo-user"), ("session-a", "other")):
            with self.subTest(session_scope=session_scope, user_id=user_id):
                with self.assertRaises(ConversationRejected) as context:
                    self.store.get_thread(thread.conversation_id, session_scope, user_id)
                self.assertEqual(context.exception.reason_code, "CONVERSATION_NOT_FOUND")

    def test_identical_request_replays_stored_turn(self):
        thread, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=15),
        )
        input_value = text_input("next")
        updated, first_turn, first_replay = self.store.commit_turn(
            thread=thread,
            request_id="request-1",
            source="typed",
            conversation_input=input_value,
            transition=transition(step="two"),
            expires_at=NOW + timedelta(minutes=15),
        )

        replayed, replay_turn, replay = self.store.commit_turn(
            thread=thread,
            request_id="request-1",
            source="typed",
            conversation_input=input_value,
            transition=transition(step="two"),
            expires_at=NOW + timedelta(minutes=15),
        )

        self.assertFalse(first_replay)
        self.assertTrue(replay)
        self.assertEqual(replayed.version, updated.version)
        self.assertEqual(replay_turn.turn_id, first_turn.turn_id)

    def test_changed_input_with_same_request_id_conflicts_without_mutation(self):
        thread, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=15),
        )
        self.store.commit_turn(
            thread=thread,
            request_id="request-1",
            source="typed",
            conversation_input=text_input("next"),
            transition=transition(step="two"),
            expires_at=NOW + timedelta(minutes=15),
        )

        with self.assertRaises(ConversationRejected) as context:
            self.store.commit_turn(
                thread=thread,
                request_id="request-1",
                source="typed",
                conversation_input=text_input("different"),
                transition=transition(step="three"),
                expires_at=NOW + timedelta(minutes=15),
            )
        self.assertEqual(
            context.exception.reason_code, "CONVERSATION_IDEMPOTENCY_CONFLICT"
        )
        self.assertEqual(self._version(thread.conversation_id), 2)

    def test_commit_turn_compare_and_swap_allows_only_one_writer(self):
        thread, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=15),
        )

        def commit(request_id):
            store = ConversationStore(self.path, clock=lambda: NOW)
            try:
                return store.commit_turn(
                    thread=thread,
                    request_id=request_id,
                    source="typed",
                    conversation_input=text_input(request_id),
                    transition=transition(step=request_id),
                    expires_at=NOW + timedelta(minutes=15),
                )
            except ConversationRejected as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, ("one", "two")))

        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].reason_code, "CONVERSATION_VERSION_CONFLICT")
        self.assertEqual(self._version(thread.conversation_id), 2)

    def test_expired_thread_is_persisted_as_expired(self):
        current = [NOW]
        store = ConversationStore(self.path, clock=lambda: current[0])
        thread, _ = store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=1),
        )
        current[0] = NOW + timedelta(minutes=2)

        with self.assertRaises(ConversationRejected) as context:
            store.get_thread(thread.conversation_id, "session-a", "demo-user")
        self.assertEqual(context.exception.reason_code, "CONVERSATION_EXPIRED")
        self.assertEqual(self._status(thread.conversation_id), ConversationStatus.EXPIRED.value)

    def test_demo_reset_removes_turns_before_threads(self):
        thread, _ = self.store.create_thread(
            session_scope="session-a",
            user_id="demo-user",
            domain="INVESTMENT",
            handler_name="investment",
            handler_version=1,
            transition=transition(),
            expires_at=NOW + timedelta(minutes=15),
        )
        self.store.commit_turn(
            thread=thread,
            request_id="request-1",
            source="typed",
            conversation_input=text_input("next"),
            transition=transition(step="two"),
            expires_at=NOW + timedelta(minutes=15),
        )

        self.database.reset()

        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversation_threads").fetchone()[0], 0)

    def _status(self, conversation_id):
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT status FROM conversation_threads WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]

    def _version(self, conversation_id):
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT version FROM conversation_threads WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
