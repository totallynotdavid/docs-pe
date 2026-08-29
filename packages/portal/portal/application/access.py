from __future__ import annotations

import functools
import inspect

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast

from portal.domain.errors import Reason, StepUpRequired


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID


# Service methods declare their own authorization. The class hook rejects a
# public method without an access rule when the service is defined.
_MARKER = "_portal_access_control"

# Sensitive actions require a second-factor proof no older than this window.
STEP_UP_WINDOW = timedelta(minutes=15)

F = TypeVar("F", bound="Callable[..., Any]")


def public(fn: F) -> F:
    """No actor-scoped check: self-scoped ("my own rows") or pre-session
    (bootstrap, called before any BrowserSession exists)."""
    _mark(fn, fn, "public")
    return fn


def site_admin(fn: F) -> F:
    """Requires actor_id to name a site administrator.

    The wrapped method must declare a parameter literally named `actor_id`.
    """
    _require_parameter(fn, "actor_id")

    @functools.wraps(fn)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        arguments = _arguments(fn, self, args, kwargs)
        await self._require_site_admin(arguments["actor_id"])
        return await fn(self, *args, **kwargs)

    _mark(wrapper, fn, "site_admin")
    return cast("F", wrapper)


def team_leader(
    *,
    actor_id: str | Callable[[Mapping[str, Any]], UUID] = "actor_id",
    team_id: str | Callable[[Mapping[str, Any]], UUID] = "team_id",
) -> Callable[[F], F]:
    """Requires actor_id to hold the team_leader role on team_id.

    Both are resolved from the wrapped method's bound arguments: a plain
    string names a parameter, a callable receives the whole argument mapping
    for the rare method (e.g. one taking a single command object) where the
    id is nested rather than a top-level parameter.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)
            await self._require_leader(
                _resolve(actor_id, arguments),
                _resolve(team_id, arguments),
            )
            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "team_leader")
        return cast("F", wrapper)

    return decorator


def team_reader(
    *,
    actor_id: str | Callable[[Mapping[str, Any]], UUID] = "actor_id",
    team_id: str | Callable[[Mapping[str, Any]], UUID] = "team_id",
) -> Callable[[F], F]:
    """Requires actor_id to hold any membership role on team_id."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)
            await self._require_reader(
                _resolve(actor_id, arguments),
                _resolve(team_id, arguments),
            )
            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "team_reader")
        return cast("F", wrapper)

    return decorator


def site_admin_or_global_search(
    *,
    actor_id: str | Callable[[Mapping[str, Any]], UUID] = "actor_id",
) -> Callable[[F], F]:
    """Requires site-admin standing, or membership on a team with the paid
    global-search entitlement (portal_teams.has_global_search).

    Deliberately not team_id-scoped: global search's whole point is looking
    beyond one team, so there is no team_id parameter to resolve. The
    entitlement check itself still runs per-actor, not per-request-team.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)
            await self._require_global_search(_resolve(actor_id, arguments))
            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "site_admin_or_global_search")
        return cast("F", wrapper)

    return decorator


def site_admin_or_leader(
    *,
    actor_id: str | Callable[[Mapping[str, Any]], UUID] = "actor_id",
    team_id: str | Callable[[Mapping[str, Any]], UUID] = "team_id",
) -> Callable[[F], F]:
    """Requires team_leader on team_id, or site-admin standing.

    Lets a site administrator manage any team's settings (members,
    credentials) without being enrolled as a member, which would otherwise
    change who counts toward "a team must retain at least one leader".
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)
            await self._require_leader_or_site_admin(
                _resolve(actor_id, arguments),
                _resolve(team_id, arguments),
            )
            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "site_admin_or_leader")
        return cast("F", wrapper)

    return decorator


def site_admin_or_reader(
    *,
    actor_id: str | Callable[[Mapping[str, Any]], UUID] = "actor_id",
    team_id: str | Callable[[Mapping[str, Any]], UUID] = "team_id",
) -> Callable[[F], F]:
    """Requires any membership role on team_id, or site-admin standing."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)
            await self._require_reader_or_site_admin(
                _resolve(actor_id, arguments),
                _resolve(team_id, arguments),
            )
            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "site_admin_or_reader")
        return cast("F", wrapper)

    return decorator


def is_step_up_fresh(
    verified_at: datetime | None,
    *,
    within: timedelta = STEP_UP_WINDOW,
) -> bool:
    """Return whether the step-up proof is still fresh."""
    return verified_at is not None and datetime.now(UTC) - verified_at <= within


def _step_up(*, within: timedelta = STEP_UP_WINDOW) -> Callable[[F], F]:
    """Require fresh MFA proof in addition to the role check."""

    def decorator(fn: F) -> F:
        _require_parameter(fn, "mfa_verified_at")

        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            arguments = _arguments(fn, self, args, kwargs)

            if not is_step_up_fresh(arguments["mfa_verified_at"], within=within):
                raise StepUpRequired(Reason.STEP_UP_REQUIRED)

            return await fn(self, *args, **kwargs)

        _mark(wrapper, fn, "step_up")
        return cast("F", wrapper)

    return decorator


def site_admin_step_up(*, within: timedelta = STEP_UP_WINDOW) -> Callable[[F], F]:
    """@site_admin plus a fresh second-factor proof, for the handful of admin
    actions sensitive enough to demand step-up MFA on top of the role check.

    Composed as one decorator rather than two stacked separately so the
    pairing is structural: nothing importable from this module lets a caller
    attach a freshness check to a role other than site_admin. The role check
    runs first, so a non-admin sees "not a site admin" rather than "step up".
    """

    def decorator(fn: F) -> F:
        return site_admin(_step_up(within=within)(fn))

    return decorator


class AuthorizedService:
    """Base for application services whose public async methods each carry an
    explicit access-control decorator.

    A subclass that defines a public async method without @site_admin,
    @site_admin_step_up(...), @team_leader(...), @team_reader(...), or
    @public raises TypeError when the class body finishes executing, i.e. at
    import time. That happens once, at process startup or test collection,
    well before the method could serve a request unchecked.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        undeclared = sorted(
            name
            for name, member in vars(cls).items()
            if not name.startswith("_")
            and inspect.iscoroutinefunction(member)
            and not hasattr(member, _MARKER)
        )

        if undeclared:
            joined = ", ".join(undeclared)
            msg = (
                f"{cls.__qualname__} defines {joined} without an access-control "
                "decorator (@site_admin, @site_admin_step_up(...), "
                "@team_leader(...), @team_reader(...), or @public)."
            )
            raise TypeError(msg)


def _resolve(
    spec: str | Callable[[Mapping[str, Any]], UUID],
    arguments: Mapping[str, Any],
) -> UUID:
    return spec(arguments) if callable(spec) else arguments[spec]


def _arguments(
    fn: Callable[..., Any],
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Mapping[str, Any]:
    bound = inspect.signature(fn).bind(self, *args, **kwargs)
    bound.apply_defaults()
    return bound.arguments


def _require_parameter(fn: Callable[..., Any], name: str) -> None:
    if name not in inspect.signature(fn).parameters:
        msg = f"{fn.__qualname__} is decorated for {name!r} but declares no such parameter"
        raise TypeError(msg)


def _mark(target: Any, fn: Callable[..., Any], label: str) -> None:
    existing = getattr(fn, _MARKER, None)
    setattr(target, _MARKER, f"{existing}+{label}" if existing else label)
