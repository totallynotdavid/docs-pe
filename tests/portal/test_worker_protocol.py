from __future__ import annotations

from uuid import uuid4

import msgspec
import pytest

from portal.worker.protocol import CredentialLease, PublishRequest, WorkLease


def _lease() -> WorkLease:
    return WorkLease(
        item_id=uuid4(),
        job_id=uuid4(),
        source="osiptel",
        document="10412345678",
        fence=3,
        credential_version_id=uuid4(),
        credential=CredentialLease(
            provider="geonode",
            config={"username": "equipo", "password": "clave"},
        ),
    )


def test_a_lease_survives_the_wire_unchanged() -> None:
    """Both sides of the worker API read this from one definition."""
    lease = _lease()

    assert msgspec.json.decode(msgspec.json.encode(lease), type=WorkLease) == lease


def test_an_empty_queue_decodes_as_no_lease() -> None:
    # The claim handler answers `null` when there is nothing to hand out, and
    # the agent has to tell that apart from a malformed response.
    assert msgspec.json.decode(b"null", type=WorkLease | None) is None


def test_a_lease_missing_a_field_is_refused_rather_than_guessed() -> None:
    partial = msgspec.json.encode({"item_id": str(uuid4()), "source": "osiptel"})

    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(partial, type=WorkLease)


def test_a_publish_request_survives_the_wire_unchanged() -> None:
    request = PublishRequest(
        item_id=uuid4(),
        fence=7,
        source="osiptel",
        provider="geonode",
        healthy_contact=True,
        document="10412345678",
        status="ok",
        columns=("documento",),
        rows=(("10412345678",),),
        error_code=None,
        content="Y29udGVuaWRv",
    )

    assert (
        msgspec.json.decode(msgspec.json.encode(request), type=PublishRequest)
        == request
    )
