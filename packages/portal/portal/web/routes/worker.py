from __future__ import annotations

import base64
import hashlib
import os
import secrets

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from portal.credentials.secrets import SecretRevealer
from portal.repository.protocols import WorkerQueue
from portal.storage.port import ObjectReference
from portal.web.deps import Storage


router = APIRouter(prefix="/api/worker")


def _worker_identity(request: Request) -> str:
    expected = os.environ.get("PORTAL_WORKER_BOOTSTRAP_TOKEN", "")
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    worker_id = request.headers.get("x-portal-worker", "").strip()
    if not expected or not worker_id or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="trabajador no autorizado")
    if not worker_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(
            status_code=400, detail="identificador de trabajador inválido"
        )
    return worker_id


def _queue(request: Request) -> WorkerQueue:
    """The worker API exists only where the queue and its credentials are real."""
    queue: WorkerQueue | None = request.app.state.worker_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="worker no está configurado")
    return queue


def _revealer(request: Request) -> SecretRevealer:
    revealer: SecretRevealer | None = request.app.state.secret_revealer
    if revealer is None:
        raise HTTPException(status_code=503, detail="worker no está configurado")
    return revealer


WorkerId = Annotated[str, Depends(_worker_identity)]
Queue = Annotated[WorkerQueue, Depends(_queue)]
Revealer = Annotated[SecretRevealer, Depends(_revealer)]


@router.post("/claim")
async def worker_claim(
    request: Request, worker_id: WorkerId, queue: Queue, revealer: Revealer
) -> dict[str, object] | None:
    body = await request.json()
    sources = tuple(str(value) for value in body.get("sources", []))
    work = await queue.claim(worker_id, sources)
    if work is None:
        return None
    credential = await queue.credential_for_job(work.job_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="credencial no disponible")
    return {
        "item_id": str(work.item_id),
        "job_id": str(work.job_id),
        "source": work.source,
        "document": work.document,
        "fence": work.lease_fence,
        "credential": {
            "provider": credential.provider,
            "config": revealer.reveal(credential.config_ciphertext),
        },
    }


@router.post("/publish")
async def worker_publish(
    request: Request, worker_id: WorkerId, queue: Queue, storage: Storage
) -> dict[str, bool]:
    body = await request.json()
    try:
        item_id = UUID(str(body["item_id"]))
        fence = int(body["fence"])
        content = base64.b64decode(str(body["content"]), validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=400, detail="resultado de trabajador inválido"
        ) from error
    team_id = await queue.item_team(item_id)
    if team_id is None:
        raise HTTPException(status_code=404, detail="trabajo no encontrado")
    reference = ObjectReference(
        id=uuid4(),
        team_id=team_id,
        provider="portal-worker",
        container="results",
        object_key=f"{item_id}/{uuid4()}",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
    )
    await storage.put_immutable(reference, content)
    await queue.add_object_reference(reference)
    published = await queue.publish(item_id, worker_id, fence, reference.id)
    return {"published": published}
