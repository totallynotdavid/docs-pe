from __future__ import annotations

import functools
import inspect

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast

from portal.domain.errors import Reason, StepUpRequired


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID


# OWASP Authorization Cheat Sheet: "adopt a deny-by-default mentality... whenever
# new functionality or resources are exposed" and prefer centralized, framework-
# level enforcement over a check a handler has to remember to call.
# https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
#
# A manual `await self._require_site_admin(actor_id)` at the top of a method is
# exactly the pattern that guidance warns about: present today, but nothing
# stops a new method from shipping without it. These decorators make the check
# part of the method's declared shape instead. AuthorizedService.__init_subclass__
# then makes the omission a TypeError at import time (a class that defines an
# undeclared public method fails to define), not a silent hole in production.
_MARKER = "_portal_access_control"

# OWASP Multifactor Authentication Cheat Sheet calls for step-up MFA on sensitive
# actions and privilege elevation. 15 minutes matches how long a login's MFA
# proof is trusted before a sensitive action asks for it again.
# https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html
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


def is_step_up_fresh(
    verified_at: datetime | None,
    *,
    within: timedelta = STEP_UP_WINDOW,
) -> bool:
    """The freshness rule @site_admin_step_up enforces, factored out so there
    is one place that defines "recent" rather than the constant getting
    copied wherever something needs to ask the same question."""
    return verified_at is not None and datetime.now(UTC) - verified_at <= within


def _step_up(*, within: timedelta = STEP_UP_WINDOW) -> Callable[[F], F]:
    """Layers a freshness check on top of whatever role check already guards
    this method. Private: only site_admin_step_up composes it, because the
    only enrollment path in this system is ensure_site_admin, so admin is
    the only role a second factor can ever exist for. A step-up check on any
    other role would be a permanent, silent deny for an actor who could never
    produce a fresh proof, not a real access control.

    The wrapped method must declare a parameter literally named
    `mfa_verified_at` (datetime | None) with no default, so a caller cannot
    forget to decide what to pass; it has to be `session.mfa_verified_at` or
    an explicit `None`, not an omission.
    """

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
                "@team_leader(...), @team_reader(...), or @public). "
                "See portal/application/access.py."
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
