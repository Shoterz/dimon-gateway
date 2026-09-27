from __future__ import annotations

import asyncio
import unittest

from services.inference_gateway.errors import GatewayError
from services.inference_gateway.idempotency import IdempotencyStore


class IdempotencyStoreTests(unittest.TestCase):
    def test_success_is_replayed_and_payload_is_defensively_copied(self) -> None:
        async def scenario() -> None:
            store = IdempotencyStore()
            first = store.begin("alice", "request-1", "fingerprint-a")
            self.assertEqual(first.state, "new")
            response = {"intent": {"kind": "BENEFICIARY_TRANSFER"}}
            store.succeed("alice", "request-1", "fingerprint-a", response)
            response["intent"]["kind"] = "tampered"

            replay = store.begin("alice", "request-1", "fingerprint-a")
            self.assertEqual(replay.state, "replay")
            result = await replay.resolve()
            self.assertEqual(result["intent"]["kind"], "BENEFICIARY_TRANSFER")
            result["intent"]["kind"] = "mutated-again"
            self.assertEqual(
                (await store.begin("alice", "request-1", "fingerprint-a").resolve())["intent"]["kind"],
                "BENEFICIARY_TRANSFER",
            )

        asyncio.run(scenario())

    def test_conflicting_fingerprint_is_explicit_and_identity_isolated(self) -> None:
        async def scenario() -> None:
            store = IdempotencyStore()
            store.begin("alice", "same-id", "one")

            conflict = store.begin("alice", "same-id", "two")
            separate = store.begin("bob", "same-id", "two")

            self.assertEqual(conflict.state, "conflict")
            self.assertEqual(separate.state, "new")

        asyncio.run(scenario())

    def test_concurrent_replay_waits_for_the_first_success(self) -> None:
        async def scenario() -> None:
            store = IdempotencyStore()
            store.begin("alice", "request-1", "fingerprint")
            replay = store.begin("alice", "request-1", "fingerprint")
            waiter = asyncio.create_task(replay.resolve())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())

            store.succeed("alice", "request-1", "fingerprint", {"ok": True})

            self.assertEqual(await waiter, {"ok": True})

        asyncio.run(scenario())

    def test_failed_work_is_not_cached_and_wakes_concurrent_replays(self) -> None:
        async def scenario() -> None:
            store = IdempotencyStore()
            store.begin("alice", "request-1", "fingerprint")
            replay = store.begin("alice", "request-1", "fingerprint")
            waiter = asyncio.create_task(replay.resolve())
            failure = GatewayError("UPSTREAM_UNAVAILABLE", "upstream failed", 503)

            store.abort("alice", "request-1", "fingerprint", failure)

            with self.assertRaises(GatewayError) as raised:
                await waiter
            self.assertEqual(raised.exception.code, "UPSTREAM_UNAVAILABLE")
            self.assertEqual(store.begin("alice", "request-1", "fingerprint").state, "new")

        asyncio.run(scenario())

    def test_ttl_expiry_allows_a_new_request(self) -> None:
        async def scenario() -> None:
            now = [10.0]
            store = IdempotencyStore(ttl_seconds=5, clock=lambda: now[0])
            store.begin("alice", "request-1", "one")
            store.succeed("alice", "request-1", "one", {"ok": True})
            now[0] = 15.0

            self.assertEqual(store.begin("alice", "request-1", "two").state, "new")

        asyncio.run(scenario())

    def test_completed_entries_are_evicted_to_bound_memory(self) -> None:
        async def scenario() -> None:
            store = IdempotencyStore(max_entries=2)
            for index in range(3):
                request_id = f"request-{index}"
                store.begin("alice", request_id, "fingerprint")
                store.succeed("alice", request_id, "fingerprint", {"index": index})

            self.assertEqual(store.entry_count, 2)
            self.assertEqual(store.begin("alice", "request-0", "new-fingerprint").state, "new")

        asyncio.run(scenario())

    def test_invalid_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IdempotencyStore(ttl_seconds=0)
        with self.assertRaises(ValueError):
            IdempotencyStore(max_entries=0)


if __name__ == "__main__":
    unittest.main()
