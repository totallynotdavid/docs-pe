from __future__ import annotations

import stat

from typing import TYPE_CHECKING

import pytest

from ops.deploy import (
    CORE_COMPOSE,
    WORKER_COMPOSE,
    Deployer,
    DeployError,
    Dokploy,
    Registry,
    Spec,
    Worker,
    admin_exec,
    main,
    parse_env,
)
from tests.ops.fake_dokploy import FakeDokploy


if TYPE_CHECKING:
    from pathlib import Path


ENV = {
    "PORTAL_PUBLIC_ORIGIN": "https://portal.example.org",
    "PORTAL_WORKER_BOOTSTRAP_TOKEN": "bootstrap",
    "PORTAL_WORKER_DATABASE_ADDRESS": "192.0.2.1:5432",
    "WORKER_API_PUBLISHED_PORT": "9443",
    "POSTGRES_PASSWORD": "secret",
}
WORKERS = (Worker("node-a", "192.0.2.2"), Worker("node-b", "192.0.2.3"))


def spec(image: str = "registry.example/portal:1", **env: str) -> Spec:
    return Spec(name="acme", image=image, env={**ENV, **env}, workers=WORKERS)


@pytest.fixture
def fake() -> FakeDokploy:
    return FakeDokploy(docker_hosts={worker.address for worker in WORKERS})


@pytest.fixture
def deployer(fake: FakeDokploy) -> Deployer:
    api = Dokploy("http://dokploy.test", fake.api_key, transport=fake.transport())
    return Deployer(api, log=lambda _: None, poll_seconds=0)


def compose(fake: FakeDokploy, name: str) -> dict[str, object]:
    return next(row for row in fake.composes.values() if row["name"] == name)


def test_first_run_creates_the_whole_deployment(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())

    assert [project["name"] for project in fake.projects] == ["acme"]
    assert sorted(row["name"] for row in fake.composes.values()) == [
        "core",
        "worker-node-a",
        "worker-node-b",
    ]

    core = compose(fake, "core")
    assert core["sourceType"] == "raw"
    assert core["composeFile"] == CORE_COMPOSE.read_text(encoding="utf-8")
    assert core["serverId"] is None
    assert parse_env(str(core["env"])) == {
        "PORTAL_IMAGE": "registry.example/portal:1",
        **ENV,
    }

    [domain] = fake.domains
    assert domain["composeId"] == core["composeId"]
    assert (domain["host"], domain["serviceName"], domain["port"]) == (
        "portal.example.org",
        "web",
        8000,
    )

    [key] = fake.ssh_keys
    assert [
        (server["name"], server["ipAddress"], server["username"], server["sshKeyId"])
        for server in fake.servers
    ] == [
        ("acme/node-a", "192.0.2.2", "root", key["sshKeyId"]),
        ("acme/node-b", "192.0.2.3", "root", key["sshKeyId"]),
    ]

    worker = compose(fake, "worker-node-a")
    assert worker["serverId"] == fake.servers[0]["serverId"]
    assert worker["composeFile"] == WORKER_COMPOSE.read_text(encoding="utf-8")
    assert parse_env(str(worker["env"])) == {
        "PORTAL_IMAGE": "registry.example/portal:1",
        "PORTAL_WORKER_API_URL": "http://192.0.2.1:9443",
        "PORTAL_WORKER_BOOTSTRAP_TOKEN": "bootstrap",
        "PORTAL_WORKER_ID": "node-a",
        "PORTAL_WORKER_TAILSCALE_HOSTNAME": "node-a",
    }

    deployed = [fake.composes[row["composeId"]]["name"] for row in fake.deployments]
    assert deployed == ["core", "worker-node-a", "worker-node-b"]


def test_second_run_changes_nothing(fake: FakeDokploy, deployer: Deployer) -> None:
    deployer.deploy(spec())
    fake.calls.clear()

    deployer.deploy(spec())

    assert fake.mutations() == []


def test_a_staged_deployment_starts_on_the_next_run(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec(), start=False)

    assert fake.deployments == []
    assert len(fake.composes) == 3

    fake.calls.clear()
    deployer.deploy(spec())

    assert set(fake.mutations()) == {"POST compose.deploy"}
    deployed = [fake.composes[row["composeId"]]["name"] for row in fake.deployments]
    assert deployed == ["core", "worker-node-a", "worker-node-b"]


def test_output_shows_the_key_to_install_then_only_ok(fake: FakeDokploy) -> None:
    api = Dokploy("http://dokploy.test", fake.api_key, transport=fake.transport())
    lines: list[str] = []
    deployer = Deployer(api, log=lines.append, poll_seconds=0)

    deployer.deploy(spec())
    [key] = fake.ssh_keys
    assert f"create  ssh key acme: {key['publicKey']}" in lines

    lines.clear()
    deployer.deploy(spec())
    assert lines
    assert all(line.startswith("ok ") for line in lines)


def test_new_image_updates_and_redeploys_without_creating(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    fake.calls.clear()

    deployer.deploy(spec(image="registry.example/portal:2"))

    assert not [call for call in fake.mutations() if call.endswith(".create")]
    assert fake.mutations().count("POST compose.deploy") == 3
    for row in fake.composes.values():
        assert "PORTAL_IMAGE=registry.example/portal:2\n" in str(row["env"])


def test_a_new_origin_moves_the_domain(fake: FakeDokploy, deployer: Deployer) -> None:
    deployer.deploy(spec())
    fake.calls.clear()

    deployer.deploy(spec(PORTAL_PUBLIC_ORIGIN="https://other.example.org"))

    assert [domain["host"] for domain in fake.domains] == ["other.example.org"]
    assert "POST domain.update" in fake.mutations()
    assert fake.mutations().count("POST compose.deploy") == 1


def test_a_failed_deployment_is_retried_on_the_next_run(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    fake.failing_composes.add("worker-node-b")

    with pytest.raises(DeployError, match="worker-node-b: deployment ended error"):
        deployer.deploy(spec())

    fake.failing_composes.clear()
    fake.calls.clear()
    deployer.deploy(spec())

    assert fake.mutations() == ["POST compose.deploy"]
    assert compose(fake, "worker-node-b")["composeStatus"] == "done"


def test_a_host_without_docker_stops_the_deploy(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    fake.docker_hosts.discard("192.0.2.3")

    with pytest.raises(
        DeployError, match=r"acme/node-b \(192\.0\.2\.3\) has no Docker"
    ):
        deployer.deploy(spec())

    assert "worker-node-b" not in {row["name"] for row in fake.composes.values()}


def test_remove_worker_revokes_then_deletes(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    fake.calls.clear()

    deployer.remove_worker("acme", "node-a")

    assert fake.commands_run == [
        "worker-api: portal-admin worker revoke --worker-id node-a"
    ]
    mutations = fake.mutations()
    assert mutations.index("POST compose.stop") < mutations.index(
        "POST schedule.runManually"
    )
    assert mutations.index("POST schedule.runManually") < mutations.index(
        "POST compose.delete"
    )
    assert [server["name"] for server in fake.servers] == ["acme/node-b"]
    assert "worker-node-a" not in {row["name"] for row in fake.composes.values()}
    assert fake.schedules == []

    fake.calls.clear()
    deployer.remove_worker("acme", "node-a")

    assert fake.mutations() == []


def test_remove_worker_resumes_without_revoking_twice(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    server = fake.servers[0]
    # A compose still on the server makes server.remove fail after the revoke.
    fake.composes["blocker"] = {"composeId": "blocker", "serverId": server["serverId"]}

    with pytest.raises(DeployError, match=r"server\.remove"):
        deployer.remove_worker("acme", "node-a")

    del fake.composes["blocker"]
    deployer.remove_worker("acme", "node-a")

    assert len(fake.commands_run) == 1
    assert [server["name"] for server in fake.servers] == ["acme/node-b"]


def test_a_failed_revoke_keeps_the_worker(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    fake.failing_commands.add("portal-admin worker revoke --worker-id node-a")

    with pytest.raises(DeployError, match=r"schedule\.runManually"):
        deployer.remove_worker("acme", "node-a")

    assert "worker-node-a" in {row["name"] for row in fake.composes.values()}
    assert [server["name"] for server in fake.servers] == ["acme/node-a", "acme/node-b"]


def test_list_names_every_object(fake: FakeDokploy, deployer: Deployer) -> None:
    deployer.deploy(spec())

    lines = deployer.describe("acme")

    assert lines == [
        "project\tacme\t\t",
        "compose\tcore\tacme-core-abc123\tdone",
        "compose\tworker-node-a\tacme-worker-node-a-abc123\tdone",
        "compose\tworker-node-b\tacme-worker-node-b-abc123\tdone",
        "server\tacme/node-a\t192.0.2.2\tactive",
        "server\tacme/node-b\t192.0.2.3\tactive",
    ]


def colliding_pair(fake: FakeDokploy) -> tuple[Spec, Spec]:
    """Two deployments whose names joined by "-" would name the same server."""
    first = Spec(
        name="acme",
        image="portal:1",
        env=ENV,
        workers=(Worker("blue-node", "192.0.2.2"),),
    )
    second = Spec(
        name="acme-blue",
        image="portal:1",
        env=ENV,
        workers=(Worker("node", "192.0.2.3"),),
    )
    fake.docker_hosts.update({"192.0.2.2", "192.0.2.3"})
    return first, second


def test_deployments_with_colliding_names_get_separate_servers(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    first, second = colliding_pair(fake)

    deployer.deploy(first)
    deployer.deploy(second)

    assert sorted((server["name"], server["ipAddress"]) for server in fake.servers) == [
        ("acme-blue/node", "192.0.2.3"),
        ("acme/blue-node", "192.0.2.2"),
    ]

    fake.calls.clear()
    deployer.deploy(first)
    assert fake.mutations() == []


def test_list_shows_only_the_named_deployments_servers(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    first, second = colliding_pair(fake)
    deployer.deploy(first)
    deployer.deploy(second)

    servers = [line for line in deployer.describe("acme") if line.startswith("server")]

    assert servers == ["server\tacme/blue-node\t192.0.2.2\tactive"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "Acme"}, "deployment name"),
        ({"name": "acme/blue"}, "deployment name"),
        ({"env": {**ENV, "PORTAL_IMAGE": "x"}}, "pass the image with --image"),
        ({"env": {"PORTAL_PUBLIC_ORIGIN": "https://a.example"}}, "BOOTSTRAP_TOKEN"),
        ({"workers": (WORKERS[0], WORKERS[0])}, "unique"),
    ],
)
def test_spec_rejects_bad_input(change: dict[str, object], message: str) -> None:
    fields: dict[str, object] = {
        "name": "acme",
        "image": "portal:1",
        "env": ENV,
        "workers": WORKERS,
        **change,
    }

    with pytest.raises(DeployError, match=message):
        Spec(**fields)  # type: ignore[arg-type]


def test_worker_and_env_parsing() -> None:
    assert Worker.parse("node-a=192.0.2.2") == Worker("node-a", "192.0.2.2")
    for name in ("Node", "blue/node"):
        with pytest.raises(DeployError, match="NAME=ADDRESS"):
            Worker.parse(f"{name}=192.0.2.2")

    assert parse_env("# comment\n\nA=1\nB='two words'\nC=\"x=y\"\n") == {
        "A": "1",
        "B": "two words",
        "C": "x=y",
    }
    with pytest.raises(DeployError, match="line 1"):
        parse_env("export A=1\n")


def test_cli_needs_dokploy_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOKPLOY_URL", raising=False)
    monkeypatch.delenv("DOKPLOY_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="DOKPLOY_URL"):
        main(["list"])


REGISTRY = Registry("registry.example", "reader", "token-1")


@pytest.fixture
def private_deployer(fake: FakeDokploy) -> Deployer:
    api = Dokploy("http://dokploy.test", fake.api_key, transport=fake.transport())
    return Deployer(api, log=lambda _: None, poll_seconds=0, registry=REGISTRY)


def test_one_deploy_logs_every_server_into_the_registry(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    private_deployer.deploy(spec())

    assert [entry["registryName"] for entry in fake.registries] == [
        "acme",
        "acme/node-a",
        "acme/node-b",
    ]
    assert fake.logins == [
        (None, "registry.example", "reader"),
        (fake.servers[0]["serverId"], "registry.example", "reader"),
        (fake.servers[1]["serverId"], "registry.example", "reader"),
    ]
    assert all(row["status"] == "done" for row in fake.deployments), (
        "every compose deployed after its login"
    )


def test_a_converged_deployment_logs_in_nothing(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    private_deployer.deploy(spec())
    fake.calls.clear()
    fake.logins.clear()

    private_deployer.deploy(spec())

    assert fake.mutations() == []
    assert fake.logins == []


def test_a_retried_deployment_logs_in_again_on_its_own_server(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    fake.failing_composes.add("worker-node-b")
    with pytest.raises(DeployError, match="worker-node-b: deployment ended error"):
        private_deployer.deploy(spec())

    fake.failing_composes.clear()
    fake.calls.clear()
    fake.logins.clear()
    private_deployer.deploy(spec())

    assert fake.mutations() == ["POST registry.update", "POST compose.deploy"]
    assert fake.logins == [(fake.servers[1]["serverId"], "registry.example", "reader")]


def test_a_new_token_reaches_the_entries_on_the_next_deploy(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    private_deployer.deploy(spec())
    api = Dokploy("http://dokploy.test", fake.api_key, transport=fake.transport())
    renewed = Deployer(
        api,
        log=lambda _: None,
        poll_seconds=0,
        registry=Registry("registry.example", "reader", "token-2"),
    )

    renewed.deploy(spec(image="registry.example/portal:2"))

    assert {entry["password"] for entry in fake.registries} == {"token-2"}
    assert len(fake.registries) == 3


def test_a_login_failure_stops_before_the_deploy(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    fake.refused_passwords.add(REGISTRY.password)

    with pytest.raises(DeployError, match="docker login was refused"):
        private_deployer.deploy(spec())

    assert fake.deployments == []


@pytest.mark.parametrize(
    ("image", "environ", "expected"),
    [
        (
            "ghcr.io/acme/app/portal:abc",
            {"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": "p"},
            Registry("ghcr.io", "u", "p"),
        ),
        (
            "localhost:5000/portal:abc",
            {"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": "p"},
            Registry("localhost:5000", "u", "p"),
        ),
        ("ghcr.io/acme/portal:abc", {}, None),
        ("ghcr.io/acme/portal:abc", {"REGISTRY_USERNAME": "u"}, None),
        ("acme/portal:abc", {"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": "p"}, None),
        ("portal:local", {"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": "p"}, None),
    ],
)
def test_registry_comes_from_the_image_host_and_the_environment(
    image: str, environ: dict[str, str], expected: Registry | None
) -> None:
    assert Registry.for_image(image, environ) == expected


def test_remove_worker_deletes_its_registry_entry(
    fake: FakeDokploy, private_deployer: Deployer
) -> None:
    private_deployer.deploy(spec())

    private_deployer.remove_worker("acme", "node-a")

    assert [entry["registryName"] for entry in fake.registries] == [
        "acme",
        "acme/node-b",
    ]


def test_rotate_key_restarts_the_core_then_rewraps(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    fake.calls.clear()
    deployed = len(fake.deployments)

    deployer.rotate_key("acme", "v2")

    assert (
        parse_env(str(compose(fake, "core")["env"]))["PORTAL_MASTER_KEY_VERSION"]
        == "v2"
    )
    mutations = fake.mutations()
    assert mutations.index("POST compose.update") < mutations.index(
        "POST compose.deploy"
    )
    assert mutations.index("POST compose.deploy") < mutations.index(
        "POST schedule.runManually"
    )
    assert mutations.count("POST compose.deploy") == 1
    assert len(fake.deployments) == deployed + 2
    assert fake.commands_run == ["worker-api: portal-admin key rewrap"]


def test_rotate_key_rerun_rewraps_again_without_redeploying(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    deployer.rotate_key("acme", "v2")
    fake.calls.clear()

    deployer.rotate_key("acme", "v2")

    assert fake.mutations() == ["POST schedule.runManually"]
    assert fake.commands_run == ["worker-api: portal-admin key rewrap"] * 2


def test_a_failed_core_deploy_stops_before_the_rewrap(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    fake.failing_composes.add("core")

    with pytest.raises(DeployError, match="core: deployment ended error"):
        deployer.rotate_key("acme", "v2")

    assert fake.commands_run == []

    fake.failing_composes.clear()
    deployer.rotate_key("acme", "v2")

    assert fake.commands_run == ["worker-api: portal-admin key rewrap"]


def test_a_later_deploy_keeps_the_rotated_key_version(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    deployer.deploy(spec())
    deployer.rotate_key("acme", "v2")
    fake.calls.clear()

    deployer.deploy(spec())

    assert fake.mutations() == []


def test_rotate_key_needs_a_core_and_a_plain_version(
    fake: FakeDokploy, deployer: Deployer
) -> None:
    with pytest.raises(DeployError, match="no Dokploy project"):
        deployer.rotate_key("acme", "v2")

    deployer.deploy(spec(), start=False)
    with pytest.raises(DeployError, match="must match"):
        deployer.rotate_key("acme", "v 2")


def test_the_env_file_cannot_set_the_key_version() -> None:
    with pytest.raises(DeployError, match="rotate-key"):
        spec(PORTAL_MASTER_KEY_VERSION="v2")


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `docker` on PATH that logs its arguments. `ps` lists STUB_CONTAINERS
    and `exec` exits with STUB_EXEC_STATUS."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$STUB_LOG"\n'
        'if [ "$1" = ps ]; then for id in $STUB_CONTAINERS; do echo "$id"; done; fi\n'
        'if [ "$1" = exec ]; then exit "${STUB_EXEC_STATUS:-0}"; fi\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_CONTAINERS", "c1")
    return log


def ps_command(app_name: str) -> str:
    return (
        f"ps --quiet --filter label=com.docker.compose.project={app_name} "
        "--filter label=com.docker.compose.service=worker-api"
    )


def test_admin_exec_finds_the_container_by_its_compose_labels(docker: Path) -> None:
    status = admin_exec("acme-core-abc123", ["worker", "revoke", "--worker-id", "n"])

    assert status == 0
    assert docker.read_text(encoding="utf-8").splitlines() == [
        ps_command("acme-core-abc123"),
        "exec c1 portal-admin worker revoke --worker-id n",
    ]


def test_admin_exec_returns_the_command_status(
    docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_EXEC_STATUS", "3")

    assert admin_exec("acme-core-abc123", ["worker", "revoke"]) == 3


@pytest.mark.parametrize(("containers", "found"), [("", 0), ("c1 c2", 2)])
def test_admin_exec_needs_exactly_one_container(
    docker: Path, monkeypatch: pytest.MonkeyPatch, containers: str, found: int
) -> None:
    monkeypatch.setenv("STUB_CONTAINERS", containers)

    with pytest.raises(DeployError, match=rf"found {found}"):
        admin_exec("acme-core-abc123", ["worker", "revoke"])

    assert "exec" not in docker.read_text(encoding="utf-8")


def test_worker_issue_resolves_the_core_container_itself(
    fake: FakeDokploy,
    deployer: Deployer,
    docker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployer.deploy(spec())
    monkeypatch.setenv("DOKPLOY_URL", "http://dokploy.test")
    monkeypatch.setenv("DOKPLOY_API_KEY", fake.api_key)
    app_name = str(compose(fake, "core")["appName"])

    with pytest.raises(SystemExit) as stopped:
        main(["worker", "issue", "acme", "node-c"], transport=fake.transport())

    assert stopped.value.code == 0
    assert docker.read_text(encoding="utf-8").splitlines() == [
        ps_command(app_name),
        "exec c1 portal-admin worker issue --worker-id node-c --tailscale-hostname node-c",
    ]


def test_worker_revoke_passes_the_failure_status_through(
    fake: FakeDokploy,
    deployer: Deployer,
    docker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployer.deploy(spec())
    monkeypatch.setenv("DOKPLOY_URL", "http://dokploy.test")
    monkeypatch.setenv("DOKPLOY_API_KEY", fake.api_key)
    monkeypatch.setenv("STUB_EXEC_STATUS", "1")

    with pytest.raises(SystemExit) as stopped:
        main(["worker", "revoke", "acme", "node-a"], transport=fake.transport())

    assert stopped.value.code == 1
    assert docker.read_text(encoding="utf-8").splitlines()[-1] == (
        "exec c1 portal-admin worker revoke --worker-id node-a"
    )
