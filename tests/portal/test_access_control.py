from __future__ import annotations

import pytest

from portal.application.access import (
    AuthorizedService,
    public,
    site_admin,
    site_admin_step_up,
)


def test_an_undeclared_public_method_fails_to_define() -> None:
    with pytest.raises(TypeError, match="orphan"):

        class _Service(AuthorizedService):
            async def orphan(self, actor_id: object) -> object:
                return actor_id


def test_a_declared_public_method_defines_cleanly() -> None:
    class _Service(AuthorizedService):
        @public
        async def fine(self, actor_id: object) -> object:
            return actor_id

    assert _Service.fine is not None


def test_a_private_helper_needs_no_decorator() -> None:
    class _Service(AuthorizedService):
        async def _helper(self) -> None:
            return None

    assert _Service._helper is not None


def test_a_sync_method_is_outside_the_scan() -> None:
    class _Service(AuthorizedService):
        @staticmethod
        def catalog() -> tuple[str, ...]:
            return ()

    assert _Service.catalog() == ()


def test_site_admin_requires_a_parameter_literally_named_actor_id() -> None:
    with pytest.raises(TypeError, match="actor_id"):

        @site_admin
        def action(self: object, user_id: object) -> object:
            return user_id


def test_site_admin_step_up_requires_a_parameter_literally_named_mfa_verified_at() -> (
    None
):
    with pytest.raises(TypeError, match="mfa_verified_at"):

        @site_admin_step_up()
        def action(self: object, actor_id: object) -> object:
            return actor_id
