import hashlib
import hmac
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.views.decorators.debug import sensitive_variables

from inscriptions.models import Registration, RegistrationEvent


class InvalidEditToken(Exception):
    """Raised when a public edit token cannot grant access."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    value: str
    digest: str


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not token:
        return ""
    return hmac.new(
        force_bytes(settings.SECRET_KEY), force_bytes(token), hashlib.sha256
    ).hexdigest()


@sensitive_variables("value")
def issue_token() -> IssuedToken:
    value = secrets.token_urlsafe(32)
    return IssuedToken(value=value, digest=token_digest(value))


@sensitive_variables("token", "candidate")
def verify_token(token: str, expected_digest: str) -> bool:
    candidate = token_digest(token)
    expected = expected_digest if isinstance(expected_digest, str) else ""
    return hmac.compare_digest(candidate, expected)


@sensitive_variables("token")
def get_registration_for_token(*, reference, token: str, at=None) -> Registration:
    at = at or timezone.now()
    try:
        registration = Registration.objects.get(reference=reference)
    except (Registration.DoesNotExist, ValueError, TypeError) as exc:
        raise InvalidEditToken("Lien de modification invalide.") from exc

    deadline = settings.REGISTRATION_EDIT_DEADLINE
    valid = (
        registration.status != Registration.Status.CANCELLED
        and registration.token_revoked_at is None
        and at <= deadline
        and verify_token(token, registration.edit_token_digest)
    )
    if not valid:
        raise InvalidEditToken("Lien de modification invalide ou expiré.")
    return registration


@sensitive_variables("token")
@transaction.atomic
def rotate_registration_token(
    registration_or_id,
    actor_kind=RegistrationEvent.ActorKind.TEACHER,
    actor_user=None,
    at=None,
) -> str:
    at = at or timezone.now()
    registration_id = (
        registration_or_id.pk
        if isinstance(registration_or_id, Registration)
        else registration_or_id
    )
    registration = Registration.objects.select_for_update().get(pk=registration_id)
    if registration.status == Registration.Status.CANCELLED:
        raise InvalidEditToken("Une inscription annulée ne peut pas recevoir de nouveau lien.")
    token = issue_token()
    registration.edit_token_digest = token.digest
    registration.token_created_at = at
    registration.token_revoked_at = None
    registration.save(
        update_fields=(
            "edit_token_digest",
            "token_created_at",
            "token_revoked_at",
            "updated_at",
        )
    )
    RegistrationEvent.objects.create(
        registration=registration,
        event_type=RegistrationEvent.Type.TOKEN_ROTATED,
        actor_kind=actor_kind,
        actor_user=actor_user,
        changes={},
    )
    return token.value
