#!/usr/bin/env python3
"""Focused, deterministic tests for the JWKS verifier and secure-by-default config.

Two-user cross-owner isolation over HTTP lives in test_ownership.py; this file
only covers the verifier itself and the config/authenticator wiring around it.
"""

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import generate_rsa_keypair, jwk_from_private_key, mint_assertion, write_jwks  # noqa: E402

HAS_AUTH_DEPS = (
    importlib.util.find_spec("jwt") is not None and importlib.util.find_spec("cryptography") is not None
)


@unittest.skipUnless(HAS_AUTH_DEPS, "PyJWT and cryptography are required for the auth tests")
class JWKSVerifierTests(unittest.TestCase):
    ISSUER = "https://edge.example.test"
    AUDIENCE = "orca-web-api"
    SUBJECT = "user-alpha"

    def setUp(self):
        from web.api.auth import JWKSVerifier, derive_owner_id

        self.derive_owner_id = derive_owner_id
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = generate_rsa_keypair()
        self.jwks_path = write_jwks(self.root / "jwks.json", [jwk_from_private_key(self.key, "key-1")])
        self.verifier_cls = JWKSVerifier

    def tearDown(self):
        self.temporary.cleanup()

    def verifier(self, **overrides):
        jwks_path = overrides.pop("jwks_path", self.jwks_path)
        return self.verifier_cls(
            jwks_path,
            overrides.pop("issuer", self.ISSUER),
            overrides.pop("audience", self.AUDIENCE),
            **overrides,
        )

    def assertion(self, **overrides):
        return mint_assertion(
            overrides.pop("private_key", self.key),
            overrides.pop("kid", "key-1"),
            overrides.pop("issuer", self.ISSUER),
            overrides.pop("audience", self.AUDIENCE),
            overrides.pop("subject", self.SUBJECT),
            **overrides,
        )

    def test_verifies_a_valid_assertion_and_derives_the_owner_id(self):
        from web.api.auth import Principal

        principal = self.verifier().verify(self.assertion())
        self.assertIsInstance(principal, Principal)
        self.assertEqual(principal.owner_id, self.derive_owner_id(self.ISSUER, self.SUBJECT))
        # Immutable: a Principal cannot be mutated after construction.
        with self.assertRaises(Exception):
            principal.owner_id = "tampered"

    def test_two_subjects_under_the_same_issuer_get_different_owner_ids(self):
        first = self.verifier().verify(self.assertion(subject="user-alpha"))
        second = self.verifier().verify(self.assertion(subject="user-beta"))
        self.assertNotEqual(first.owner_id, second.owner_id)

    def test_rejects_an_expired_assertion(self):
        from web.api.auth import VerificationError

        stale = self.assertion(issued_at=time.time() - 600, lifetime=60)
        with self.assertRaises(VerificationError):
            self.verifier().verify(stale)

    def test_rejects_an_assertion_not_yet_valid(self):
        from web.api.auth import VerificationError

        future = self.assertion(issued_at=time.time() + 600, lifetime=60)
        with self.assertRaises(VerificationError):
            self.verifier().verify(future)

    def test_rejects_a_wrong_issuer(self):
        from web.api.auth import VerificationError

        with self.assertRaises(VerificationError):
            self.verifier().verify(self.assertion(issuer="https://not-the-edge.example.test"))

    def test_rejects_a_wrong_audience(self):
        from web.api.auth import VerificationError

        with self.assertRaises(VerificationError):
            self.verifier().verify(self.assertion(audience="some-other-api"))

    def test_rejects_a_bad_signature(self):
        from web.api.auth import VerificationError

        forged_key = generate_rsa_keypair()
        # Signed by a key that never appears in the JWKS, but claiming the
        # real key's kid.
        forged = mint_assertion(forged_key, "key-1", self.ISSUER, self.AUDIENCE, self.SUBJECT)
        with self.assertRaises(VerificationError):
            self.verifier().verify(forged)

    def test_rejects_a_non_rs256_algorithm(self):
        from web.api.auth import VerificationError

        # A raw "none"-alg token: unsigned, but structurally a JWT.
        import base64
        import json

        header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "key-1"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": self.ISSUER,
                    "aud": self.AUDIENCE,
                    "sub": self.SUBJECT,
                    "iat": int(time.time()),
                    "nbf": int(time.time()),
                    "exp": int(time.time()) + 60,
                    "jti": "none-alg",
                }
            ).encode()
        ).rstrip(b"=")
        none_alg_token = (header + b"." + payload + b".").decode("ascii")
        with self.assertRaises(VerificationError):
            self.verifier().verify(none_alg_token)

    def test_rejects_missing_required_claims(self):
        from web.api.auth import VerificationError

        for claim in ("iss", "aud", "sub", "jti", "iat", "nbf", "exp"):
            with self.subTest(claim=claim):
                with self.assertRaises(VerificationError):
                    self.verifier().verify(self.assertion(omit_claims=[claim]))

    def test_rejects_malformed_claim_types(self):
        from web.api.auth import VerificationError

        for claims in (
            {"sub": 12345},
            {"sub": "contains\x00separator"},
            {"jti": ["not", "a", "string"]},
            {"aud": [self.AUDIENCE, 7]},
            {"nbf": "now"},
            {"exp": "soon"},
            {"iat": "now"},
            {"roles": "administrator"},
            {"roles": ["administrator", 7]},
        ):
            with self.subTest(claims=claims):
                with self.assertRaises(VerificationError):
                    self.verifier().verify(self.assertion(claim_overrides=claims))

    def test_rejects_an_oversized_assertion(self):
        from web.api.auth import MAX_ASSERTION_BYTES, VerificationError

        oversized = self.assertion(claim_overrides={"padding": "x" * MAX_ASSERTION_BYTES})
        with self.assertRaises(VerificationError):
            self.verifier().verify(oversized)

    def test_rejects_a_lifetime_longer_than_the_documented_maximum(self):
        from web.api.auth import MAX_LIFETIME_SECONDS, VerificationError

        long_lived = self.assertion(lifetime=MAX_LIFETIME_SECONDS + 3600)
        with self.assertRaises(VerificationError):
            self.verifier().verify(long_lived)

    def test_rejects_an_unknown_kid(self):
        from web.api.auth import VerificationError

        with self.assertRaises(VerificationError):
            self.verifier().verify(self.assertion(kid="no-such-key"))

    def test_rejects_a_missing_kid(self):
        from web.api.auth import VerificationError

        with self.assertRaises(VerificationError):
            self.verifier().verify(self.assertion(header_overrides={"kid": None}))

    def test_rejects_a_duplicate_kid_in_the_jwks(self):
        from web.api.auth import VerificationError

        # A usable, unambiguous key too, so the JWKS as a whole still loads;
        # only the ambiguous `kid` itself must fail to resolve.
        other_key = generate_rsa_keypair()
        duplicate_jwks = write_jwks(
            self.root / "duplicate.json",
            [
                jwk_from_private_key(self.key, "key-1"),
                jwk_from_private_key(self.key, "shared-kid"),
                jwk_from_private_key(other_key, "shared-kid"),
            ],
        )
        verifier = self.verifier(jwks_path=duplicate_jwks)
        token = mint_assertion(self.key, "shared-kid", self.ISSUER, self.AUDIENCE, self.SUBJECT)
        with self.assertRaises(VerificationError):
            verifier.verify(token)
        # The unambiguous key in the same file still works.
        self.assertTrue(verifier.verify(self.assertion()).owner_id)

    def test_rotation_accepts_both_the_old_and_the_new_key(self):
        new_key = generate_rsa_keypair()
        rotated_jwks = write_jwks(
            self.root / "rotated.json",
            [jwk_from_private_key(self.key, "key-old"), jwk_from_private_key(new_key, "key-new")],
        )
        verifier = self.verifier(jwks_path=rotated_jwks)

        old_token = mint_assertion(self.key, "key-old", self.ISSUER, self.AUDIENCE, self.SUBJECT)
        new_token = mint_assertion(new_key, "key-new", self.ISSUER, self.AUDIENCE, self.SUBJECT)
        self.assertEqual(
            verifier.verify(old_token).owner_id, verifier.verify(new_token).owner_id
        )

    def test_rejects_a_token_signed_by_a_retired_key_under_the_new_kid(self):
        # Rotation must not become "any key in the file signs for any kid".
        new_key = generate_rsa_keypair()
        rotated_jwks = write_jwks(
            self.root / "rotated.json",
            [jwk_from_private_key(self.key, "key-old"), jwk_from_private_key(new_key, "key-new")],
        )
        verifier = self.verifier(jwks_path=rotated_jwks)
        from web.api.auth import VerificationError

        mismatched = mint_assertion(self.key, "key-new", self.ISSUER, self.AUDIENCE, self.SUBJECT)
        with self.assertRaises(VerificationError):
            verifier.verify(mismatched)

    def test_load_refuses_a_jwks_with_no_usable_keys(self):
        from web.api.auth import AuthConfigurationError

        empty_jwks = write_jwks(self.root / "empty.json", [])
        with self.assertRaises(AuthConfigurationError):
            self.verifier(jwks_path=empty_jwks)

        only_ec = write_jwks(
            self.root / "ec-only.json",
            [{"kty": "EC", "kid": "ec-1", "crv": "P-256", "x": "AA", "y": "AA"}],
        )
        with self.assertRaises(AuthConfigurationError):
            self.verifier(jwks_path=only_ec)

    def test_load_refuses_an_unreadable_jwks_path(self):
        from web.api.auth import AuthConfigurationError

        with self.assertRaises(AuthConfigurationError):
            self.verifier(jwks_path=self.root / "does-not-exist.json")


@unittest.skipUnless(HAS_AUTH_DEPS, "PyJWT and cryptography are required for the auth tests")
class AuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = generate_rsa_keypair()
        self.jwks_path = write_jwks(self.root / "jwks.json", [jwk_from_private_key(self.key, "key-1")])

    def tearDown(self):
        self.temporary.cleanup()

    class _FakeRequest:
        def __init__(self, authorization=None):
            self.headers = {"authorization": authorization} if authorization else {}

    def test_disabled_mode_always_returns_the_fixed_local_principal(self):
        from web.api.auth import LOCAL_DEVELOPMENT_OWNER_ID, create_authenticator

        authenticate = create_authenticator("disabled")
        without_header = authenticate(self._FakeRequest())
        with_header = authenticate(self._FakeRequest("Bearer garbage-not-even-a-jwt"))
        self.assertEqual(without_header.owner_id, LOCAL_DEVELOPMENT_OWNER_ID)
        self.assertEqual(with_header.owner_id, LOCAL_DEVELOPMENT_OWNER_ID)

    def test_required_mode_needs_a_bearer_scheme(self):
        from web.api.errors import ApiError

        authenticate = self._required_authenticator()
        token = mint_assertion(self.key, "key-1", "iss", "aud", "sub")
        for header in (None, "", f"Basic {token}", token, "Bearer"):
            with self.subTest(header=header):
                with self.assertRaises(ApiError) as raised:
                    authenticate(self._FakeRequest(header))
                self.assertEqual(raised.exception.code, "authentication_required")
                self.assertEqual(raised.exception.status, 401)

    def test_required_mode_accepts_a_valid_bearer_assertion(self):
        authenticate = self._required_authenticator()
        token = mint_assertion(self.key, "key-1", "iss", "aud", "sub")
        principal = authenticate(self._FakeRequest(f"Bearer {token}"))
        self.assertTrue(principal.owner_id)

    def test_required_mode_rejects_an_invalid_bearer_assertion(self):
        from web.api.errors import ApiError

        authenticate = self._required_authenticator()
        with self.assertRaises(ApiError) as raised:
            authenticate(self._FakeRequest("Bearer not-a-real-jwt"))
        self.assertEqual(raised.exception.code, "authentication_required")

    def test_required_mode_construction_needs_issuer_audience_and_jwks(self):
        from web.api.auth import AuthConfigurationError, create_authenticator

        with self.assertRaises(AuthConfigurationError):
            create_authenticator("required")
        with self.assertRaises(AuthConfigurationError):
            create_authenticator("required", self.jwks_path, None, "aud")
        with self.assertRaises(AuthConfigurationError):
            create_authenticator("required", self.jwks_path, "iss", None)

    def test_unknown_mode_fails_closed(self):
        from web.api.auth import AuthConfigurationError, create_authenticator

        with self.assertRaises(AuthConfigurationError):
            create_authenticator("permissive")

    def _required_authenticator(self):
        from web.api.auth import create_authenticator

        return create_authenticator("required", self.jwks_path, "iss", "aud")


class ApiConfigAuthDefaultsTests(unittest.TestCase):
    """`ApiConfig` itself, independent of the optional JWT/cryptography stack."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _base_kwargs(self):
        return dict(
            repo_root=self.root,
            state_root=self.root / "state",
            worker_command=(sys.executable,),
            profile_vendors=("Testing",),
        )

    def test_auth_mode_defaults_to_required(self):
        from web.api.config import ApiConfig

        config = ApiConfig(**self._base_kwargs())
        self.assertEqual(config.auth_mode, "required")

    def test_required_mode_fails_startup_without_issuer_audience_or_jwks(self):
        from web.api.config import ApiConfig
        from web.api.errors import ApiError

        with self.assertRaises(ApiError) as raised:
            ApiConfig(**self._base_kwargs()).validate()
        self.assertEqual(raised.exception.code, "invalid_api_configuration")

        with self.assertRaises(ApiError):
            ApiConfig(**self._base_kwargs(), auth_issuer="iss").validate()
        with self.assertRaises(ApiError):
            ApiConfig(**self._base_kwargs(), auth_issuer="iss", auth_audience="aud").validate()

    def test_required_mode_fails_startup_for_a_jwks_path_that_does_not_exist(self):
        from web.api.config import ApiConfig
        from web.api.errors import ApiError

        with self.assertRaises(ApiError) as raised:
            ApiConfig(
                **self._base_kwargs(),
                auth_issuer="iss",
                auth_audience="aud",
                auth_jwks_path=self.root / "missing.json",
            ).validate()
        self.assertEqual(raised.exception.code, "invalid_api_configuration")

    def test_an_unknown_auth_mode_fails_startup(self):
        from web.api.config import ApiConfig
        from web.api.errors import ApiError

        with self.assertRaises(ApiError) as raised:
            ApiConfig(**self._base_kwargs(), auth_mode="sometimes").validate()
        self.assertEqual(raised.exception.code, "invalid_api_configuration")

    def test_disabled_mode_needs_nothing_else(self):
        from web.api.config import ApiConfig

        # Must not raise.
        ApiConfig(**self._base_kwargs(), auth_mode="disabled").validate()


if __name__ == "__main__":
    unittest.main()
