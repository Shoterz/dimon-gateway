from __future__ import annotations

import asyncio
import time
import unittest

import httpx
from fastapi import Depends

from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.auth import (
    AccessIdentity,
    AccessTokenVerifier,
    require_access_identity,
)
from services.inference_gateway.cli import main as gateway_main
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.errors import GatewayError


def valid_env() -> dict[str, str]:
    return {
        "DIMON_GATEWAY_HOST": "127.0.0.1",
        "DIMON_GATEWAY_PORT": "8891",
        "DIMON_GATEWAY_TRUSTED_PROXIES": "127.0.0.1,::1",
        "DIMON_CF_ACCESS_AUDIENCE": "audience-tag",
        "DIMON_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
        "DIMON_QWEN_API_BASE": "http://127.0.0.1:8080/v1",
        "DIMON_NEMOTRON_URL": "ws://127.0.0.1:8004/v1/realtime",
        "DIMON_KOKORO_URL": "http://127.0.0.1:3001",
    }


class StaticJwksClient:
    def __init__(self, public_key):
        self.public_key = public_key
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str):
        del token
        self.calls += 1

        class SigningKey:
            key = self.public_key

        return SigningKey()


class GatewayConfigTests(unittest.TestCase):
    def test_valid_config_is_loopback_and_derives_jwks_endpoint(self) -> None:
        config = GatewayConfig.from_env(valid_env())

        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.bind_port, 8891)
        self.assertEqual(
            config.jwks_url,
            "https://team.cloudflareaccess.com/cdn-cgi/access/certs",
        )
        self.assertEqual({str(value) for value in config.trusted_proxies}, {"127.0.0.1", "::1"})

    def test_required_identity_settings_cannot_be_missing(self) -> None:
        for key in (
            "DIMON_GATEWAY_TRUSTED_PROXIES",
            "DIMON_CF_ACCESS_AUDIENCE",
            "DIMON_CF_ACCESS_ISSUER",
        ):
            with self.subTest(key=key):
                env = valid_env()
                env[key] = ""
                with self.assertRaisesRegex(ValueError, key):
                    GatewayConfig.from_env(env)

    def test_bind_and_all_upstream_endpoints_must_be_loopback(self) -> None:
        cases = {
            "DIMON_GATEWAY_HOST": "0.0.0.0",
            "DIMON_QWEN_API_BASE": "https://models.example/v1",
            "DIMON_NEMOTRON_URL": "wss://speech.example/v1/realtime",
            "DIMON_KOKORO_URL": "http://192.168.1.10:3001",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                env = valid_env()
                env[key] = value
                with self.assertRaisesRegex(ValueError, "loopback"):
                    GatewayConfig.from_env(env)

    def test_invalid_port_and_issuer_are_rejected(self) -> None:
        for key, value in (
            ("DIMON_GATEWAY_PORT", "70000"),
            ("DIMON_CF_ACCESS_ISSUER", "http://team.cloudflareaccess.com"),
            ("DIMON_CF_ACCESS_ISSUER", "https://team.cloudflareaccess.com/path"),
        ):
            with self.subTest(key=key, value=value):
                env = valid_env()
                env[key] = value
                with self.assertRaises(ValueError):
                    GatewayConfig.from_env(env)


class AccessTokenVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = jwt
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.config = GatewayConfig.from_env(valid_env())

    def claims(self, **overrides):
        now = int(time.time())
        values = {
            "type": "app",
            "iss": self.config.access_issuer,
            "aud": [self.config.access_audience],
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 60,
            "sub": "user-123",
            "email": "friend@example.com",
        }
        values.update(overrides)
        return values

    def token(self, claims=None, *, key=None) -> str:
        return self.jwt.encode(
            claims or self.claims(),
            key or self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def verifier(self) -> AccessTokenVerifier:
        return AccessTokenVerifier(
            self.config,
            jwks_client=StaticJwksClient(self.private_key.public_key()),
        )

    def assert_unauthorized(self, token: str | None, peer: str = "127.0.0.1") -> None:
        with self.assertRaises(GatewayError) as raised:
            self.verifier().verify(token, peer)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.code, "UNAUTHORIZED")

    def test_missing_and_malformed_assertions_are_rejected(self) -> None:
        self.assert_unauthorized(None)
        self.assert_unauthorized("not-a-jwt")

    def test_wrong_audience_issuer_expiry_and_signature_are_rejected(self) -> None:
        cases = (
            self.token(self.claims(aud=["other-audience"])),
            self.token(self.claims(iss="https://other.cloudflareaccess.com")),
            self.token(self.claims(exp=int(time.time()) - 1)),
            self.token(key=self.other_key),
        )
        for token in cases:
            with self.subTest(token=token[:24]):
                self.assert_unauthorized(token)

    def test_untrusted_immediate_peer_is_rejected_before_key_lookup(self) -> None:
        jwks = StaticJwksClient(self.private_key.public_key())
        verifier = AccessTokenVerifier(self.config, jwks_client=jwks)

        with self.assertRaises(GatewayError) as raised:
            verifier.verify(self.token(), "192.168.1.20")

        self.assertEqual(raised.exception.code, "UNTRUSTED_PROXY")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(jwks.calls, 0)

    def test_verified_user_identity_uses_subject_and_email(self) -> None:
        identity = self.verifier().verify(self.token(), "127.0.0.1")

        self.assertEqual(
            identity,
            AccessIdentity("user-123", "user", "friend@example.com"),
        )

    def test_verified_service_identity_uses_common_name_when_sub_is_empty(self) -> None:
        token = self.token(
            self.claims(
                sub="",
                email=None,
                common_name="service-client.access",
            )
        )

        identity = self.verifier().verify(token, "::1")

        self.assertEqual(
            identity,
            AccessIdentity("service-client.access", "service", None),
        )

    def test_missing_user_and_service_subject_is_rejected(self) -> None:
        self.assert_unauthorized(self.token(self.claims(sub="", email=None)))

    def request(self, app, path: str, *, client: tuple[str, int], headers=None):
        async def send():
            transport = httpx.ASGITransport(app=app, client=client)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://gateway.test",
            ) as http:
                return await http.get(path, headers=headers)

        return asyncio.run(send())

    def test_request_dependency_ignores_forwarded_identity_from_untrusted_peer(self) -> None:
        app = create_gateway_app(self.config, verifier=self.verifier())

        @app.get("/probe")
        async def probe(identity: AccessIdentity = Depends(require_access_identity)):
            return {"subject": identity.subject}

        response = self.request(
            app,
            "/probe",
            client=("192.168.1.20", 1234),
            headers={
                "cf-access-jwt-assertion": self.token(),
                "x-forwarded-for": "127.0.0.1",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "UNTRUSTED_PROXY")

    def test_gateway_error_envelope_does_not_expose_exception_details(self) -> None:
        app = create_gateway_app(self.config, verifier=self.verifier())

        @app.get("/probe")
        async def probe():
            raise GatewayError("SAFE_CODE", "safe public message", 429, "request-123")

        response = self.request(app, "/probe", client=("127.0.0.1", 1234))

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json(),
            {
                "error": {
                    "code": "SAFE_CODE",
                    "message": "safe public message",
                    "request_id": "request-123",
                }
            },
        )


class GatewayCliTests(unittest.TestCase):
    def test_cli_uses_only_validated_loopback_bind(self) -> None:
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))

        gateway_main(environ=valid_env(), runner=runner)

        self.assertEqual(calls[0][0], ("services.inference_gateway.app:application_factory",))
        self.assertEqual(
            calls[0][1],
            {"factory": True, "host": "127.0.0.1", "port": 8891},
        )


if __name__ == "__main__":
    unittest.main()
