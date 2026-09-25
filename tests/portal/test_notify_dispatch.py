from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from portal.notify.dispatch import CLAIM_LEASE_SECONDS, dispatch_once
from portal.notify.mailer import SEND_TIMEOUT_SECONDS

from tests.portal.conftest import object_reference, seed_team, submit_command


if TYPE_CHECKING:
    import asyncpg

    from portal.application.service import PortalService


class RecordingMailer:
    def __init__(self) -> None:
        self.recipients: list[str] = []

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.recipients.append(to)

    async def aclose(self) -> None:
        return


class DyingMailer:
    """A dispatcher that is killed mid-send, after the claim and before any
    outcome is recorded."""

    async def send(self, *, to: str, subject: str, body: str) -> None:
        msg = "dispatcher died"
        raise SystemExit(msg)

    async def aclose(self) -> None:
        return


async def _queue_email(pool: asyncpg.Pool, service: PortalService) -> None:
    """An all-excluded job completes at once and queues its notifications."""
    team = await seed_team(pool)
    key = f"entradas/{uuid4().hex}.csv"

    await service.submit(
        submit_command(
            team,
            await object_reference(pool, team.team_id, key),
            value="inválido",
        )
    )


async def _abandon_claim(pool: asyncpg.Pool) -> None:
    with pytest.raises(SystemExit):
        await dispatch_once(pool, DyingMailer())


async def _end_lease(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        UPDATE portal_notification_outbox
           SET available_at = now() - interval '1 second'
         WHERE state = 'sending'
        """
    )


def test_the_lease_outlasts_a_send() -> None:
    assert CLAIM_LEASE_SECONDS > 4 * SEND_TIMEOUT_SECONDS


async def test_a_row_abandoned_by_a_dead_dispatcher_is_sent_once_after_its_lease(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    await _queue_email(pool, service)
    await _abandon_claim(pool)
    await _end_lease(pool)

    mailer = RecordingMailer()

    await dispatch_once(pool, mailer)
    await dispatch_once(pool, mailer)

    assert len(mailer.recipients) == 1
    assert (
        await pool.fetchval(
            "SELECT state FROM portal_notification_outbox WHERE channel = 'email'"
        )
        == "sent"
    )


async def test_a_row_within_its_lease_is_not_claimed_again(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    await _queue_email(pool, service)
    await _abandon_claim(pool)

    mailer = RecordingMailer()

    await dispatch_once(pool, mailer)

    assert mailer.recipients == []
    assert (
        await pool.fetchval(
            "SELECT state FROM portal_notification_outbox WHERE channel = 'email'"
        )
        == "sending"
    )
