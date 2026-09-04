from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ops import worker_node


if TYPE_CHECKING:
    import pytest


def test_add_reconfigures_existing_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = worker_node.Node("north", "100.64.0.2")
    state = worker_node.AddState(server_id="server")
    calls: list[tuple[str, str]] = []
    json_bodies: list[dict[str, Any] | None] = []

    monkeypatch.setattr(
        worker_node,
        "find_compose",
        lambda _name: {"composeId": "compose"},
    )
    monkeypatch.setattr(worker_node, "worker_bootstrap_token", lambda: "bootstrap")

    deployment_responses = iter(
        [
            {"deployments": [{"deploymentId": "old", "status": "done"}]},
            {"deployments": [{"deploymentId": "new", "status": "done"}]},
        ]
    )

    def fake_dokploy(method: str, procedure: str, **_kwargs: Any) -> Any:
        calls.append((method, procedure))
        json_bodies.append(_kwargs.get("json_body"))
        if procedure == "compose.one":
            return next(deployment_responses)
        return None

    monkeypatch.setattr(worker_node, "dokploy", fake_dokploy)

    worker_node.run_steps(
        worker_node.build_compose_steps(node, state),
        dry_run=False,
    )

    assert calls == [
        ("POST", "compose.update"),
        ("POST", "compose.saveEnvironment"),
        ("GET", "compose.one"),
        ("POST", "compose.deploy"),
        ("GET", "compose.one"),
    ]
    assert json_bodies[0] is not None
    assert json_bodies[0]["watchPaths"] == [
        "packages/core",
        "packages/portal",
        worker_node.WORKER_COMPOSE_PATH,
    ]
    assert json_bodies[0]["composeType"] == "docker-compose"
    assert json_bodies[1] is not None
    assert json_bodies[1]["createEnvFile"] is True
