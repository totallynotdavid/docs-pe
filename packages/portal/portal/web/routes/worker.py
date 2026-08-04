from __future__ import annotations

import base64
import hashlib
import secrets

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from portal.credentials.secrets import AesGcmSecretProtector
from portal.repository.jobs import PostgresJobRepository
from portal.storage.port import ObjectReference
from portal.web.deps import Settings, Storage


router = APIRouter(prefix="/api/worker")


def _worker_identity(request: Request, settings: Settings) -> str:
    expected = settings.worker_bootstrap_token
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    worker_id = request.headers.get("x-portal-worker", "").strip()

    if not expected or not worker_id or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="trabajador no autorizado")

    if not worker_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(
            status_code=400,
            detail="identificador de trabajador inválido",
        )

    return worker_id


def _worker_jobs(request: Request) -> PostgresJobRepository:
    return request.app.state.worker_queue


def _secret_protector(request: Request) -> AesGcmSecretProtector:
    return request.app.state.secret_protector


class ClaimRequest(BaseModel):
    sources: list[str] = Field(default_factory=list)


class PublishRequest(BaseModel):
    item_id: UUID
    fence: int
    content: str


WorkerId = Annotated[str, Depends(_worker_identity)]
WorkerJobs = Annotated[PostgresJobRepository, Depends(_worker_jobs)]
SecretProtector = Annotated[AesGcmSecretProtector, Depends(_secret_protector)]


@router.post("/claim")
async def worker_claim(
    body: ClaimRequest,
    worker_id: WorkerId,
    jobs: WorkerJobs,
    protector: SecretProtector,
) -> dict[str, object] | None:
    claim = await jobs.claim(worker_id, tuple(body.sources))

    if claim is None:
        return None

    credential = await jobs.credential_for_job(claim.job_id)

    if credential is None:
        raise HTTPException(
            status_code=409,
            detail="credencial no disponible",
        )

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


@router.post("/publish")
async def worker_publish(
    body: PublishRequest,
    worker_id: WorkerId,
    jobs: WorkerJobs,
    storage: Storage,
) -> dict[str, bool]:
    try:
        content = base64.b64decode(body.content, validate=True)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="resultado de trabajador inválido",
        ) from error

    team_id = await jobs.item_team(body.item_id)

    if team_id is None:
        raise HTTPException(
            status_code=404,
            detail="trabajo no encontrado",
        )

    reference = ObjectReference(
        id=uuid4(),
        team_id=team_id,
        provider="portal-worker",
        container="results",
        object_key=f"{body.item_id}/{uuid4()}",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
    )

    await storage.put_immutable(reference, content)
    await jobs.add_object_reference(reference)

    published = await jobs.publish(
        body.item_id,
        worker_id,
        body.fence,
        reference.id,
    )

    return {"published": published}
