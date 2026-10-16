"""Per-user ledgers that stay O(1) as users grow: sharding, one-time schema
setup, template cloning, the registry, and background expiry."""

import sqlite3
from contextlib import closing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apps.dimon_live.database import LEDGER_PAGE_SIZE, Database, _schema_version
from apps.dimon_live.session_manager import LedgerRegistry, SessionManager


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managers = []
        self.manager = self.make_manager()

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.temp.cleanup()

    def make_manager(self, **overrides):
        options = dict(
            owner_path=self.root / "owner.sqlite3",
            visitor_dir=self.root / "visitors",
            allowed_origins={"http://test"},
            visitor_ttl_seconds=3600,
            max_visitor_sessions=0,
        )
        options.update(overrides)
        manager = SessionManager(**options)
        self.managers.append(manager)
        return manager

    def age_registry(self, last_seen):
        with closing(sqlite3.connect(self.root / "visitors" / "registry.sqlite3")) as connection:
            connection.execute("UPDATE ledgers SET last_seen = ?", (last_seen,))
            connection.commit()

    def resolve(self, cookie=None, manager=None):
        return (manager or self.manager).resolve_headers(
            host="test", origin="http://test", cookie=cookie, access_assertion=None
        )

    def test_ledgers_are_spread_over_hashed_folders(self):
        path = self.manager.ledger_path("A" * 32)
        relative = path.relative_to(self.root / "visitors")
        self.assertEqual(len(relative.parts), 3)
        self.assertRegex(relative.parts[0], r"^[0-9a-f]{2}$")
        self.assertRegex(relative.parts[1], r"^[0-9a-f]{2}$")
        self.assertNotEqual(self.manager.ledger_path("a" * 32).parent, path.parent)

    def test_new_users_are_cloned_from_one_template_with_small_pages(self):
        first = self.manager.create_visitor()
        second = self.manager.create_visitor()
        self.assertEqual(len(list((self.root / "visitors" / "_template").glob("ledger-*.sqlite3"))), 1)
        for context in (first, second):
            connection = sqlite3.connect(context.database.path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], _schema_version(True))
                self.assertEqual(connection.execute("PRAGMA page_size").fetchone()[0], LEDGER_PAGE_SIZE)
            finally:
                connection.close()
        self.assertEqual(first.database.get_state()["accounts"][0]["balance_minor"], 200000)
        self.assertLess(first.database.path.stat().st_size, 200_000)
        # Independent ledgers despite the shared template.
        first.database.reset()
        self.assertNotEqual(first.database.path, second.database.path)

    def test_schema_runs_once_per_file_not_per_request(self):
        context = self.manager.create_visitor()
        fresh = Database(context.database.path)
        with mock.patch.object(Database, "_create_or_upgrade", side_effect=AssertionError("schema re-run")):
            fresh.initialize()   # stamped: a single PRAGMA read

    def test_requests_reuse_the_cached_ledger(self):
        context = self.manager.create_visitor()
        again = self.resolve(context.session_id)
        self.assertIs(again.database, context.database)

    def test_creating_users_never_scans_the_folder(self):
        self.manager.create_visitor()
        with mock.patch.object(Path, "glob", side_effect=AssertionError("folder scanned")):
            self.manager.create_visitor()
            self.resolve(self.manager.create_visitor().session_id)

    def test_idle_anonymous_ledgers_expire_but_verified_customers_stay(self):
        anonymous = self.manager.create_visitor()
        customer = self.manager.create_visitor()
        self.manager.mark_registered(customer.session_id)
        old = time.time() - 7200
        self.age_registry(old)
        manager = self.make_manager()   # a fresh process: nothing cached
        self.assertNotEqual(self.resolve(anonymous.session_id, manager).session_id, anonymous.session_id)
        self.assertFalse(anonymous.database.path.exists())
        self.assertEqual(self.resolve(customer.session_id, manager).session_id, customer.session_id)

    def test_background_cleanup_removes_expired_ledgers_in_batches(self):
        contexts = [self.manager.create_visitor() for _ in range(5)]
        self.manager.mark_registered(contexts[0].session_id)
        self.age_registry(time.time() - 7200)
        removed = self.manager.cleanup(batch=2) + self.manager.cleanup(batch=2)
        self.assertEqual(removed, 4)
        self.assertEqual(self.manager.registry.count(), 1)
        self.assertTrue(contexts[0].database.path.exists())
        self.assertFalse(any(c.database.path.exists() for c in contexts[1:]))

    def test_session_cap_uses_the_registry(self):
        manager = self.make_manager(max_visitor_sessions=2)
        manager.create_visitor()
        manager.create_visitor()
        with self.assertRaises(Exception) as raised:
            manager.create_visitor()
        self.assertEqual(getattr(raised.exception, "reason_code", None), "SESSION_CAP_REACHED")

    def test_ledgers_from_before_sharding_are_still_served(self):
        legacy_id = "L" * 32
        legacy = self.root / "visitors" / f"{legacy_id}.sqlite3"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        Database(legacy).initialize()
        self.assertEqual(self.resolve(legacy_id).session_id, legacy_id)

    def test_audio_written_by_one_worker_is_served_by_another_but_only_to_its_owner(self):
        import asyncio
        from types import SimpleNamespace
        from apps.dimon_live.speech import SpeechService

        audio_dir = self.root / "audio"
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28

        async def synthesize(text):
            return wav

        owner = SimpleNamespace(session_id="S" * 32)
        other = SimpleNamespace(session_id="T" * 32)

        async def scenario():
            first = SpeechService(base_url="http://k.test", audio_dir=audio_dir, synthesizer=synthesize)
            artifact = first.queue_text("Sent five dollars.", owner)
            await asyncio.gather(*first._tasks)
            second = SpeechService(base_url="http://k.test", audio_dir=audio_dir, synthesizer=synthesize)
            return (await second.read(artifact, owner), await second.read(artifact, other),
                    await second.read("../../etc/passwd", owner))

        served, stolen, traversal = asyncio.run(scenario())
        self.assertEqual(served, wav)
        self.assertIsNone(stolen)
        self.assertIsNone(traversal)

    def test_registry_lookups_use_indexes(self):
        registry = LedgerRegistry(self.root / "r.sqlite3")
        with registry._connect() as connection:
            plans = [
                " ".join(str(row[-1]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}", args))
                for query, args in (
                    ("SELECT last_seen FROM ledgers WHERE session_id = ?", ("x",)),
                    ("SELECT session_id FROM ledgers WHERE registered = 0 AND last_seen < ? LIMIT 10", (1.0,)),
                )
            ]
        registry.close()
        self.assertIn("PRIMARY KEY", plans[0])
        self.assertIn("ledgers_expiry", plans[1])


if __name__ == "__main__":
    unittest.main()
