"""Principal identity: a local-file JWKS verifier and the fixed disabled-mode principal.

See docs/web/adr/0004-service-identity.md. Verification never reaches the
network: public keys come from a JSON file mounted with the release
configuration, and the API treats every valid principal as an ordinary user.
Raw identity-provider subjects, emails, and display names are never retained
past deriving `owner_id`; `Principal` carries only that opaque, non-reversible
value.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import jwt
from jwt.algorithms import RSAAlgorithm

from .errors import ApiError


AUTH_MODE_REQUIRED = "required"
AUTH_MODE_DISABLED = "disabled"
AUTH_MODES = (AUTH_MODE_REQUIRED, AUTH_MODE_DISABLED)

# A fixed RS256 allowlist: the API never negotiates an algorithm with a caller,
# which closes the classic "alg: none" / HMAC-confusion bypass.
ALGORITHM = "RS256"
# Generous for a compact RS256 JWT with a handful of short claims; anything
# larger is refused before it is even base64-decoded.
MAX_ASSERTION_BYTES = 8 * 1024
# The short validity window ADR 0004 requires. An edge that mints
# longer-lived assertions is misconfigured, not merely generous.
MAX_LIFETIME_SECONDS = 300
# Absorbs ordinary clock drift between the edge and the API without widening
# the assertion's effective lifetime meaningfully.
CLOCK_SKEW_SECONDS = 30
REQUIRED_CLAIMS = ("iss", "aud", "sub", "jti", "iat", "nbf", "exp")

# The fixed local-development identity `disabled` mode creates. Derived the
# same way a real principal is, so downstream ownership code never special-cases it.
_LOCAL_DEVELOPMENT_ISSUER = "local-development"
_LOCAL_DEVELOPMENT_SUBJECT = "local-development"


@dataclasses.dataclass(frozen=True)
class Principal:
    """An authenticated caller. Carries nothing but the opaque ownership key."""

    owner_id: str


def derive_owner_id(issuer: str, subject: str) -> str:
    """owner_id = SHA-256(issuer, NUL, subject), hex-encoded. Never reversed."""
    digest = hashlib.sha256()
    digest.update(issuer.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(subject.encode("utf-8"))
    return digest.hexdigest()


LOCAL_DEVELOPMENT_OWNER_ID = derive_owner_id(_LOCAL_DEVELOPMENT_ISSUER, _LOCAL_DEVELOPMENT_SUBJECT)
LOCAL_DEVELOPMENT_PRINCIPAL = Principal(owner_id=LOCAL_DEVELOPMENT_OWNER_ID)


class AuthConfigurationError(Exception):
    """The JWKS file or its keys cannot support verification. A startup failure."""


class VerificationError(Exception):
    """One assertion failed verification. `reason` is for tests and logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _load_jwks(path: Path) -> Dict[str, Any]:
    """Load local RS256 RSA signing keys, keyed by `kid`.

    A `kid` that does not identify exactly one usable RSA signing key (missing,
    duplicated, wrong key type, wrong algorithm, or malformed) is simply never
    added here, so a token naming it fails verification the same way an unknown
    key would. Both a retiring and a replacement key may be present at once.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthConfigurationError(f"the JWKS file could not be read: {path}") from error
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys:
        raise AuthConfigurationError("the JWKS file must contain a non-empty 'keys' array")

    kid_occurrences: Dict[str, int] = {}
    candidates: Dict[str, Any] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        kid_occurrences[kid] = kid_occurrences.get(kid, 0) + 1
        candidates[kid] = entry

    usable: Dict[str, Any] = {}
    for kid, count in kid_occurrences.items():
        # A repeated kid does not select exactly one JWK, ambiguous or not.
        if count != 1:
            continue
        entry = candidates[kid]
        if entry.get("kty") != "RSA":
            continue
        if entry.get("use", "sig") != "sig":
            continue
        if entry.get("alg", ALGORITHM) != ALGORITHM:
            continue
        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(entry))
        except (ValueError, TypeError, jwt.exceptions.InvalidKeyError):
            continue
        # A public key carries no "d"; refuse to verify against anything that
        # looks like it could sign, in case a private JWK is ever mounted here.
        if hasattr(public_key, "private_numbers"):
            continue
        usable[kid] = public_key

    if not usable:
        raise AuthConfigurationError("the JWKS file named no usable RS256 RSA signing keys")
    return usable


class JWKSVerifier:
    """Verifies an RS256 bearer assertion against keys loaded once from disk."""

    def __init__(
        self,
        jwks_path: Path,
        issuer: str,
        audience: str,
        max_lifetime_seconds: int = MAX_LIFETIME_SECONDS,
        clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._max_lifetime_seconds = max_lifetime_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._keys = _load_jwks(jwks_path)

    def verify(self, assertion: str) -> Principal:
        if not isinstance(assertion, str) or not assertion:
            raise VerificationError("empty_assertion")
        if len(assertion.encode("utf-8")) > MAX_ASSERTION_BYTES:
            raise VerificationError("oversized_assertion")
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError as error:
            raise VerificationError("malformed_header") from error
        if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
            raise VerificationError("wrong_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise VerificationError("missing_kid")
        key = self._keys.get(kid)
        if key is None:
            raise VerificationError("unknown_kid")
        try:
            claims = jwt.decode(
                assertion,
                key=key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._clock_skew_seconds,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as error:
            raise VerificationError("claim_verification_failed") from error
        return self._principal_from_claims(claims)

    def _principal_from_claims(self, claims: Dict[str, Any]) -> Principal:
        subject = claims.get("sub")
        jti = claims.get("jti")
        issuer = claims.get("iss")
        audience = claims.get("aud")
        not_before = claims.get("nbf")
        expires_at = claims.get("exp")
        issued_at = claims.get("iat")
        roles = claims.get("roles", [])
        if not isinstance(subject, str) or not subject or "\x00" in subject:
            raise VerificationError("invalid_subject_claim")
        if not isinstance(jti, str) or not jti:
            raise VerificationError("invalid_jti_claim")
        if not isinstance(issuer, str) or not issuer:
            raise VerificationError("invalid_issuer_claim")
        if not (
            isinstance(audience, str) and audience
            or isinstance(audience, list)
            and audience
            and all(isinstance(item, str) and item for item in audience)
        ):
            raise VerificationError("invalid_audience_claim")
        # bool is an int subclass; exclude it explicitly.
        if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
            raise VerificationError("invalid_nbf_claim")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise VerificationError("invalid_exp_claim")
        if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
            raise VerificationError("invalid_iat_claim")
        if not isinstance(roles, list) or not all(
            isinstance(role, str) and role for role in roles
        ):
            raise VerificationError("invalid_roles_claim")
        if expires_at - issued_at > self._max_lifetime_seconds:
            raise VerificationError("lifetime_exceeded")
        return Principal(owner_id=derive_owner_id(issuer, subject))


Authenticator = Callable[[Any], Principal]


def _authentication_required(message: str = "A valid bearer assertion is required.") -> ApiError:
    return ApiError("authentication_required", message, 401)


def create_authenticator(
    mode: str,
    jwks_path: Optional[Path] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
) -> Authenticator:
    """Build the per-request authenticator `create_app` installs on `app.state`.

    Raises `AuthConfigurationError` for a required-mode deployment whose JWKS
    file cannot yield a usable verifier; the caller is responsible for turning
    that into a startup failure.
    """
    if mode == AUTH_MODE_DISABLED:
        def authenticate_disabled(request: Any) -> Principal:
            return LOCAL_DEVELOPMENT_PRINCIPAL

        return authenticate_disabled

    if mode != AUTH_MODE_REQUIRED:
        raise AuthConfigurationError(f"unknown auth mode: {mode!r}")
    if jwks_path is None or not issuer or not audience:
        raise AuthConfigurationError("required auth mode needs a JWKS path, issuer, and audience")
    verifier = JWKSVerifier(jwks_path, issuer, audience)

    def authenticate_required(request: Any) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _authentication_required()
        try:
            return verifier.verify(token)
        except VerificationError as error:
            raise _authentication_required() from error

    return authenticate_required
