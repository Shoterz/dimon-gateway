from __future__ import annotations

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout

from scripts.verify_nemotron_lifecycle import (
    LifecycleVerificationError,
    main,
    verify_lifecycle,
)


class FakeLease:
    def __init__(self, client):
        self.client = client
        self.released = False

    async def release(self, *, disconnected=False):
        del disconnected
        if self.released:
            return
        self.released = True
        self.client.release_calls += 1
        if self.client.release_error is not None:
            raise self.client.release_error
        self.client.active -= 1


class FakeClient:
    def __init__(
        self,
        *,
        status_override=None,
        acquire_error=None,
        release_error=None,
        cancel_after_acquire=False,
    ):
        self.status_override = status_override
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.cancel_after_acquire = cancel_after_acquire
        self.acquire_calls = 0
        self.release_calls = 0
        self.active = 0

    async def acquire(self):
        self.acquire_calls += 1
        if self.acquire_error is not None:
            raise self.acquire_error
        self.active += 1
        if self.cancel_after_acquire:
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return FakeLease(self)

    async def status(self):
        if self.status_override is not None:
            return self.status_override
        return {
            "active_sessions": self.active,
            "capacity": 1,
            "lease_timeout_seconds": 30,
            "max_lease_seconds": 90,
        }


class LifecycleVerifierTests(unittest.TestCase):
    def test_one_hundred_cycles_finish_at_zero(self) -> None:
        client = FakeClient()

        result = asyncio.run(verify_lifecycle(client, cycles=100))

        self.assertEqual(result, {"cycles": 100, "final_active_sessions": 0})
        self.assertEqual(client.acquire_calls, 100)
        self.assertEqual(client.release_calls, 100)
        self.assertEqual(client.active, 0)

    def test_nonzero_final_count_fails_within_the_bound(self) -> None:
        client = FakeClient(status_override={"active_sessions": 1})

        with self.assertRaisesRegex(LifecycleVerificationError, "did not return to zero"):
            asyncio.run(
                verify_lifecycle(
                    client,
                    cycles=1,
                    settle_timeout_seconds=0.01,
                    poll_interval_seconds=0,
                )
            )

    def test_interrupted_cycle_still_releases_its_handle(self) -> None:
        client = FakeClient(cancel_after_acquire=True)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(verify_lifecycle(client, cycles=1))

        self.assertEqual(client.release_calls, 1)
        self.assertEqual(client.active, 0)

    def test_malformed_status_is_rejected(self) -> None:
        for status in ({}, {"active_sessions": "0"}, {"active_sessions": -1}):
            with self.subTest(status=status):
                with self.assertRaisesRegex(
                    LifecycleVerificationError,
                    "invalid lifecycle status",
                ):
                    asyncio.run(verify_lifecycle(FakeClient(status_override=status), cycles=1))

    def test_acquire_and_release_failures_are_classified_without_details(self) -> None:
        sensitive_detail = "private-upstream-detail lease-id-deadbeef"
        clients = (
            FakeClient(acquire_error=RuntimeError(sensitive_detail)),
            FakeClient(release_error=RuntimeError(sensitive_detail)),
        )
        for client in clients:
            with self.subTest(client=client):
                with self.assertRaises(LifecycleVerificationError) as raised:
                    asyncio.run(verify_lifecycle(client, cycles=1))
                self.assertNotIn(sensitive_detail, str(raised.exception))

    def test_cli_output_contains_only_aggregate_results(self) -> None:
        success_output = io.StringIO()
        with redirect_stdout(success_output):
            success_code = main(
                ["--socket", "/tmp/nemotron-test.sock", "--cycles", "3"],
                client_factory=lambda _path: FakeClient(),
            )

        success = json.loads(success_output.getvalue())
        self.assertEqual(success_code, 0)
        self.assertEqual(
            success,
            {"cycles": 3, "final_active_sessions": 0, "ok": True},
        )

        sensitive_detail = "private-upstream-detail lease-id-deadbeef"
        failure_output = io.StringIO()
        with redirect_stdout(failure_output):
            failure_code = main(
                ["--socket", "/tmp/nemotron-test.sock", "--cycles", "1"],
                client_factory=lambda _path: FakeClient(
                    acquire_error=RuntimeError(sensitive_detail)
                ),
            )

        failure = failure_output.getvalue()
        self.assertEqual(failure_code, 1)
        self.assertEqual(
            json.loads(failure),
            {"error": "lifecycle_verification_failed", "ok": False},
        )
        self.assertNotIn(sensitive_detail, failure)


if __name__ == "__main__":
    unittest.main()
