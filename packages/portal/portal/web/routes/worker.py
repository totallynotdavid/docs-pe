from __future__ import annotations

import base64
import binascii
import hashlib
import secrets

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from litestar import Router, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException

from portal.credentials.secrets import AesGcmSecretProtector
from portal.repository.jobs import PostgresJobRepository
from portal.storage.port import ObjectReference, ObjectStorage


if TYPE_CHECKING:
    from litestar import Request
    from litestar.datastructures import State


def provide_worker_id(request: Request, state: State) -> str:
    expected = state.settings.worker_bootstrap_token
    authorization = request.headers.get("authorization", "")
    worker_id = request.headers.get("x-portal-worker", "").strip()

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="worker not authorized")

    token = authorization.removeprefix("Bearer ")

    if not expected or not worker_id or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="worker not authorized")

    if not worker_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid worker id")

    return worker_id


def provide_worker_jobs(state: State) -> PostgresJobRepository:
    return state.worker_queue


def provide_secret_protector(state: State) -> AesGcmSecretProtector:
    return state.secret_protector


@dataclass
class ClaimRequest:
    sources: list[str] = field(default_factory=list)


@dataclass
class PublishRequest:
    item_id: UUID
    fence: int
    content: str


@post("/claim", status_code=200)
async def worker_claim(
    data: ClaimRequest,
    worker_id: NamedDependency[str],
    worker_jobs: NamedDependency[PostgresJobRepository],
    protector: NamedDependency[AesGcmSecretProtector],
) -> dict[str, object] | None:
    claim = await worker_jobs.claim(worker_id, tuple(data.sources))

    if claim is None:
        return None

    credential = await worker_jobs.credential_for_job(claim.job_id)

    if credential is None:
        raise HTTPException(status_code=409, detail="credential not available")

    return {
        "item_id": str(claim.item_id),
        "job_id": str(claim.job_id),
        "source": claim.source,
        "document": claim.document,
        "fence": claim.lease_fence,
        "credential": {
            "provider": credential.provider,
            "config": protector.reveal(credential.config_ciphertext),
        },
    }


@post("/publish", status_code=200)
async def worker_publish(
    data: PublishRequest,
    worker_id: NamedDependency[str],
    worker_jobs: NamedDependency[PostgresJobRepository],
    storage: NamedDependency[ObjectStorage],
) -> dict[str, bool]:
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
        worker_id,
        data.fence,
        reference.id,
    )

    return {"published": published}


router = Router(
    path="/api/worker",
    route_handlers=[worker_claim, worker_publish],
    dependencies={
        "worker_id": Provide(provide_worker_id, sync_to_thread=False),
        "worker_jobs": Provide(provide_worker_jobs, sync_to_thread=False),
        "protector": Provide(provide_secret_protector, sync_to_thread=False),
    },
)
