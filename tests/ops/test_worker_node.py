from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ops import worker_node


if TYPE_CHECKING:
    import pytest


def test_add_reconfigures_existing_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = worker_node.Node("north", "100.64.0.2")
    state = worker_node.AddState(server_id="server")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        worker_node,
        "find_application",
        lambda _name: {"applicationId": "application"},
    )
    monkeypatch.setattr(worker_node, "worker_bootstrap_token", lambda: "bootstrap")

    def fake_dokploy(method: str, procedure: str, **_kwargs: Any) -> Any:
        calls.append((method, procedure))
        return None

    monkeypatch.setattr(worker_node, "dokploy", fake_dokploy)

    worker_node.run_steps(
        worker_node.build_application_steps(node, state),
        dry_run=False,
    )

    assert calls == [
        ("POST", "application.saveGithubProvider"),
        ("POST", "application.saveBuildType"),
        ("POST", "application.update"),
        ("POST", "application.saveEnvironment"),
        ("POST", "application.deploy"),
    ]
