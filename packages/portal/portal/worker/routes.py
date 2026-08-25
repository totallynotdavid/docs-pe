from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from typing import TYPE_CHECKING
from uuid import uuid4

from litestar import Request, Response, post
from litestar.datastructures import State
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException

from portal.credentials.secrets import EnvelopeProtector, decode_config
from portal.domain.errors import (
    CredentialConfigurationError,
    PermissionDenied,
    PortalError,
    Reason,
)
from portal.domain.models import AuditAction, AuditEvent, WorkerIdentity
from portal.repository.audit import PostgresAuditLog
from portal.repository.breakers import PostgresCircuitBreakers
from portal.repository.jobs import PostgresJobRepository
from portal.repository.workers import PostgresWorkerRegistry
from portal.security import new_worker_credential
from portal.storage.port import ObjectReference, ObjectStorage
from portal.worker.protocol import (
    ClaimRequest,
    CredentialLease,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    PublishRequest,
    PublishResult,
    WorkLease,
)


if TYPE_CHECKING:
    from litestar.types import ExceptionHandlersMap


async def provide_worker(request: Request, state: State) -> WorkerIdentity:
    """Authorize the worker itself.

    Reaching this handler already proves the connection came from the tailnet,
    because portal-worker-api is published on the tailnet address alone. That
    says a worker node called; the bearer credential says which one, and whether
    it is still allowed to.
    """
    authorization = request.headers.get("authorization", "")

    if not authorization.startswith("Bearer "):
        raise PermissionDenied(Reason.WORKER_NOT_AUTHORIZED)

    return await state.workers.authorize(
        request.headers.get("x-portal-worker", "").strip(),
        authorization.removeprefix("Bearer "),
    )


def provide_worker_jobs(state: State) -> PostgresJobRepository:
    return state.worker_queue


def provide_worker_registry(state: State) -> PostgresWorkerRegistry:
    return state.workers


def provide_protector(state: State) -> EnvelopeProtector:
    return state.protector


def provide_audit(state: State) -> PostgresAuditLog:
    return state.audit


def provide_breakers(state: State) -> PostgresCircuitBreakers:
    return state.breakers


def provide_storage(state: State) -> ObjectStorage:
    return state.storage


@post("/enroll", status_code=200)
async def worker_enroll(
    data: EnrollRequest,
    request: Request,
    state: State,
    workers: NamedDependency[PostgresWorkerRegistry],
    audit: NamedDependency[PostgresAuditLog],
) -> EnrollResponse:
    """Self-service credential issuance for the fixed set of worker nodes.

    Reaching this handler already proves the connection came from the tailnet
    (see provide_worker). The bootstrap token is a second, shared secret every
    enrolled node holds, and it authorizes minting a worker-scoped credential
    only. Issuing is idempotent by worker_id (PostgresWorkerRegistry.issue), so
    a restarted node just re-mints its own credential on every start instead of
    a human running `portal enroll-worker` and copying a value that is shown
    once.
    """
    authorization = request.headers.get("authorization", "")
    presented = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )

    if not state.settings.worker_bootstrap_token or not hmac.compare_digest(
        presented,
        state.settings.worker_bootstrap_token,
    ):
        raise PermissionDenied(Reason.WORKER_BOOTSTRAP_INVALID)

    credential = new_worker_credential()
    identity_id = await workers.issue(
        data.worker_id,
        credential,
        data.tailscale_hostname,
    )

    await audit.record(
        AuditEvent(
            action=AuditAction.WORKER_ISSUED,
            target_type="worker",
            target_id=identity_id,
            metadata={
                "worker_id": data.worker_id,
                "tailscale_hostname": data.tailscale_hostname,
                "method": "self-enroll",
            },
        )
    )

    return EnrollResponse(credential=credential)


@post("/claim", status_code=200)
async def worker_claim(
    data: ClaimRequest,
    worker: NamedDependency[WorkerIdentity],
    worker_jobs: NamedDependency[PostgresJobRepository],
    protector: NamedDependency[EnvelopeProtector],
    audit: NamedDependency[PostgresAuditLog],
) -> WorkLease | None:
    claim = await worker_jobs.claim(
        worker.worker_id,
        tuple(data.sources),
        affinity_source=data.affinity_source,
        affinity_credential_version_id=data.affinity_credential_version_id,
    )

    if claim is None:
        return None

    credential = await worker_jobs.credential_for_job(claim.job_id)

    if credential is None:
        raise HTTPException(status_code=409, detail="credential not available")

    # The only place stored proxy configuration is decrypted. The plaintext goes
    # straight into the response and is never written back to the database or
    # the log, and the audit row below is what makes the read accountable.
    config = decode_config(protector.reveal(credential.config))

    await audit.record(
        AuditEvent(
            action=AuditAction.CREDENTIAL_REVEALED,
            actor_id=worker.id,
            target_type="job",
            target_id=claim.job_id,
            metadata={"worker_id": worker.worker_id},
        )
    )

    return WorkLease(
        item_id=claim.item_id,
        job_id=claim.job_id,
        source=claim.source,
        document=claim.document,
        fence=claim.lease_fence,
        credential_version_id=claim.credential_version_id,
        credential=CredentialLease(provider=credential.provider, config=config),
    )


@post("/publish", status_code=200)
async def worker_publish(
    data: PublishRequest,
    worker: NamedDependency[WorkerIdentity],
    worker_jobs: NamedDependency[PostgresJobRepository],
    breakers: NamedDependency[PostgresCircuitBreakers],
    storage: NamedDependency[ObjectStorage],
) -> PublishResult:
    try:
        content = base64.b64decode(data.content, validate=True)
    except binascii.Error as error:
        raise HTTPException(status_code=400, detail="invalid worker result") from error

    team_id = await worker_jobs.item_team(data.item_id)

    if team_id is None:
        raise HTTPException(status_code=404, detail="job not found")

    reference = ObjectReference(
        id=uuid4(),
        team_id=team_id,
        provider="portal-worker",
        container="results",
        object_key=f"{data.item_id}/{uuid4()}",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
    )

    await storage.put_immutable(reference, content)
    await worker_jobs.add_object_reference(reference)

    published = await worker_jobs.publish(
        data.item_id,
        worker.worker_id,
        data.fence,
        reference.id,
    )

    if published:
        await breakers.record_outcome(
            source=data.source,
            provider=data.provider,
            healthy_contact=data.healthy_contact,
        )

    return PublishResult(published=published)


@post("/heartbeat", status_code=204)
async def worker_heartbeat(
    data: HeartbeatRequest,
    worker: NamedDependency[WorkerIdentity],
    workers: NamedDependency[PostgresWorkerRegistry],
) -> None:
    await workers.record_heartbeat(
        worker.worker_id,
        cpu_percent=data.cpu_percent,
        memory_mb=data.memory_mb,
        current_job_id=data.current_job_id,
    )


def _reason_response(error: Exception, status_code: int) -> Response[dict[str, str]]:
    """Answer a machine with a stable code, never a rendered page."""
    reason = error.reason.value if isinstance(error, PortalError) else "unknown"

    return Response({"reason": reason}, status_code=status_code)


def _denied(request: Request, error: Exception) -> Response[dict[str, str]]:
    del request
    return _reason_response(error, status_code=403)


def _unusable_credential(
    request: Request,
    error: Exception,
) -> Response[dict[str, str]]:
    del request
    return _reason_response(error, status_code=409)


handlers = (worker_enroll, worker_claim, worker_publish, worker_heartbeat)

EXCEPTION_HANDLERS: ExceptionHandlersMap = {
    CredentialConfigurationError: _unusable_credential,
    PortalError: _denied,
}
