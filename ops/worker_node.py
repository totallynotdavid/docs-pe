#!/usr/bin/env python
"""Add or remove a worker-fleet node: host config and Dokploy deployment.

    uv run ops/worker_node.py list
    uv run ops/worker_node.py add <name> <tailnet-ip> [--dry-run]
    uv run ops/worker_node.py remove <name> [--dry-run] [--yes]

See docs/worker-fleet-bringup.md for the fleet model and the one manual
prerequisite this script cannot do for you: a fresh box needs a human to
place dubu's SSH key on it and grant dubu NOPASSWD sudo before `add` can run
against it non-interactively.
"""

from __future__ import annotations

import argparse
import base64
import os
import shlex
import subprocess
import sys
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


DOKPLOY_PROJECT_ID = "7vIKTngpThRQOl6qLNj6z"
DOKPLOY_ENVIRONMENT_ID = "LNBiAHi2juK4fbQQ58lzs"
DOKPLOY_SSH_KEY_ID = "AW23Q6JMq4M5m2UT9tNTZ"
DOKPLOY_WORKER_API_COMPOSE_ID = "4j2tZFo9wcw4v9IeEjsnc"
GITHUB_PROVIDER_ID = "pYmDtTubK-eOUX-g1hiHJ"
GIT_OWNER = "totallynotdavid"
GIT_REPOSITORY = "docs-pe"
GIT_BRANCH = "scale"
GIT_DOCKERFILE = "packages/portal/Dockerfile"

TAILNET_SUFFIX = "taila2cbc1.ts.net"
MASTER_HOST = f"master.{TAILNET_SUFFIX}"
MASTER_TAILNET_IP = "100.86.240.39"
WORKER_API_URL = f"http://{MASTER_TAILNET_IP}:8443"

SSH_USER = "dubu"
SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "StrictHostKeyChecking=accept-new",
)


@dataclass(frozen=True)
class Node:
    name: str
    tailnet_ip: str = ""

    @property
    def hostname(self) -> str:
        return f"{self.name}.{TAILNET_SUFFIX}"

    @property
    def worker_id(self) -> str:
        return f"{self.name}-1"

    @property
    def app_name(self) -> str:
        return f"portal-worker-{self.name}"


@dataclass
class AddState:
    server_id: str = ""
    application_id: str = ""


@dataclass
class Step:
    description: str
    check: Callable[[], bool]
    do: Callable[[], None]


def run_steps(steps: Sequence[Step], *, dry_run: bool) -> None:
    for step in steps:
        if dry_run:
            print(f"  would   {step.description}")
            continue
        if step.check():
            print(f"  ok      {step.description}")
            continue
        step.do()
        print(f"  done    {step.description}")


def dokploy(
    method: str,
    procedure: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    base_url = os.environ.get("DOKPLOY_URL")
    api_key = os.environ.get("DOKPLOY_API_KEY")

    if not base_url or not api_key:
        msg = "DOKPLOY_URL and DOKPLOY_API_KEY must be set in the environment."
        raise SystemExit(msg)

    response = httpx.request(
        method,
        f"{base_url}/api/{procedure}",
        headers={"x-api-key": api_key},
        params=params,
        json=json_body,
        timeout=60,
    )
    response.raise_for_status()

    return response.json() if response.content else None


def ssh_run(
    hostname: str, command: str, *, check: bool = True, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", *SSH_OPTS, f"{SSH_USER}@{hostname}", command],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def run_python_in_worker_api(code: str) -> str:
    """Run `code` inside the running worker-api container on master.

    Reuses that container's own PortalSettings/DATABASE_URL instead of the
    caller needing separate Postgres credentials. Passed base64-encoded so
    the code's own quoting never has to survive two levels of shell parsing
    (this ssh invocation's, then the remote shell's).
    """
    cid = worker_api_container_id()
    encoded = base64.b64encode(code.encode()).decode()
    remote_cmd = (
        f"docker exec {cid} python3 -c "
        f"\"import base64;exec(base64.b64decode('{encoded}'))\""
    )

    return ssh_run(MASTER_HOST, remote_cmd, timeout=30).stdout


def worker_api_container_id() -> str:
    result = ssh_run(MASTER_HOST, "docker ps -q --filter name=worker-api", check=False)
    lines = result.stdout.strip().splitlines()

    if not lines:
        msg = "no running worker-api container found on master"
        raise SystemExit(msg)

    return lines[0]


def dokploy_container_id() -> str:
    """Find the Dokploy service's own container by its swarm service label.

    Not the ancestor=<image> filter: a swarm service task's image can outlive
    the local tag->id mapping it was started from (this host's dokploy image
    lost its v0.29.14 tag at some point after the container started, while the
    container itself kept running unaffected), so ancestor=<tag> silently
    matches nothing days later even though the container is up and healthy.
    """
    result = ssh_run(
        MASTER_HOST,
        "docker ps -q --filter label=com.docker.swarm.service.name=dokploy",
        check=False,
    )
    lines = result.stdout.strip().splitlines()

    if not lines:
        msg = "no running dokploy service container found on master"
        raise SystemExit(msg)

    return lines[0]


def find_server(name: str) -> dict[str, Any] | None:
    servers: list[dict[str, Any]] = dokploy("GET", "server.all")
    return next((s for s in servers if s["name"] == name), None)


def find_application(app_name: str) -> dict[str, Any] | None:
    result = dokploy(
        "GET",
        "application.search",
        params={
            "projectId": DOKPLOY_PROJECT_ID,
            "environmentId": DOKPLOY_ENVIRONMENT_ID,
            "limit": 100,
        },
    )
    items: list[dict[str, Any]] = result["items"]
    return next((a for a in items if a["name"] == app_name), None)


def worker_bootstrap_token() -> str:
    compose = dokploy(
        "GET", "compose.one", params={"composeId": DOKPLOY_WORKER_API_COMPOSE_ID}
    )

    for line in compose["env"].splitlines():
        if line.startswith("PORTAL_WORKER_BOOTSTRAP_TOKEN="):
            return line.split("=", 1)[1]

    msg = "PORTAL_WORKER_BOOTSTRAP_TOKEN not found in worker-api's own env"
    raise SystemExit(msg)


def dokploy_public_key() -> str:
    key = dokploy("GET", "sshKey.one", params={"sshKeyId": DOKPLOY_SSH_KEY_ID})
    return str(key["publicKey"]).strip()


def preflight(node: Node) -> None:
    reachable = ssh_run(node.hostname, "true", check=False)
    if reachable.returncode != 0:
        msg = (
            f"cannot SSH to {SSH_USER}@{node.hostname}: {reachable.stderr.strip()}\n"
            "A fresh box needs a human to place dubu's SSH key on it first. "
            "See docs/worker-fleet-bringup.md."
        )
        raise SystemExit(msg)

    sudo_ok = ssh_run(node.hostname, "sudo -n true", check=False)
    if sudo_ok.returncode != 0:
        msg = (
            f"{SSH_USER}@{node.hostname} does not have passwordless sudo yet. Run "
            f"on that box: echo '{SSH_USER} ALL=(ALL) NOPASSWD:ALL' | "
            f"sudo tee /etc/sudoers.d/{SSH_USER}-dokploy"
        )
        raise SystemExit(msg)


def build_docker_steps(node: Node) -> list[Step]:
    def has_docker() -> bool:
        return ssh_run(node.hostname, "docker ps", check=False).returncode == 0

    def install_docker() -> None:
        ssh_run(
            node.hostname, "curl -fsSL https://get.docker.com | sudo sh", timeout=180
        )
        ssh_run(node.hostname, f"sudo usermod -aG docker {SSH_USER}")

    def has_etc_dokploy() -> bool:
        check = "test -d /etc/dokploy && test -O /etc/dokploy || test -w /etc/dokploy"
        return ssh_run(node.hostname, check, check=False).returncode == 0

    def make_etc_dokploy() -> None:
        ssh_run(
            node.hostname,
            f"sudo mkdir -p /etc/dokploy && sudo chown {SSH_USER}:{SSH_USER} /etc/dokploy",
        )

    return [
        Step("docker installed, dubu in the docker group", has_docker, install_docker),
        Step("/etc/dokploy exists, owned by dubu", has_etc_dokploy, make_etc_dokploy),
    ]


def build_swarm_steps(node: Node) -> list[Step]:
    def is_swarm_manager() -> bool:
        result = ssh_run(
            node.hostname,
            "docker info --format '{{.Swarm.ControlAvailable}}'",
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def init_swarm() -> None:
        state_check = ssh_run(
            node.hostname, "docker info --format '{{.Swarm.LocalNodeState}}'"
        )
        if state_check.stdout.strip() == "active":
            ssh_run(node.hostname, "docker swarm leave --force")
        ssh_run(node.hostname, f"docker swarm init --advertise-addr {node.tailnet_ip}")

    def has_dokploy_network() -> bool:
        return (
            ssh_run(
                node.hostname, "docker network inspect dokploy-network", check=False
            ).returncode
            == 0
        )

    def create_dokploy_network() -> None:
        ssh_run(
            node.hostname,
            "docker network create --driver overlay --attachable dokploy-network",
        )

    def has_dokploy_pubkey() -> bool:
        result = ssh_run(node.hostname, "cat ~/.ssh/authorized_keys", check=False)
        return dokploy_public_key() in result.stdout

    def add_dokploy_pubkey() -> None:
        quoted = shlex.quote(dokploy_public_key())
        ssh_run(node.hostname, f"echo {quoted} >> ~/.ssh/authorized_keys")

    return [
        Step("single-node swarm manager", is_swarm_manager, init_swarm),
        Step("dokploy-network overlay", has_dokploy_network, create_dokploy_network),
        Step(
            "Dokploy's SSH key in dubu's authorized_keys",
            has_dokploy_pubkey,
            add_dokploy_pubkey,
        ),
    ]


def build_server_steps(node: Node, state: AddState) -> list[Step]:
    def has_server() -> bool:
        existing = find_server(node.name)
        if existing is None:
            return False
        state.server_id = existing["serverId"]
        return True

    def register_server() -> None:
        response = dokploy(
            "POST",
            "server.create",
            json_body={
                "name": node.name,
                "description": None,
                "ipAddress": node.tailnet_ip,
                "port": 22,
                "username": SSH_USER,
                "sshKeyId": DOKPLOY_SSH_KEY_ID,
                "serverType": "deploy",
            },
        )
        server_id = (response or {}).get("serverId")
        if not server_id:
            existing = find_server(node.name)
            if existing is None:
                msg = f"server.create for {node.name} returned no serverId"
                raise SystemExit(msg)
            server_id = existing["serverId"]
        state.server_id = server_id

    def validate_and_setup_server() -> None:
        dokploy("GET", "server.validate", params={"serverId": state.server_id})
        dokploy("POST", "server.setup", json_body={"serverId": state.server_id})

    def has_known_host() -> bool:
        cid = dokploy_container_id()
        pattern = shlex.quote(node.tailnet_ip)
        cmd = f"docker exec {cid} sh -c 'grep -qF {pattern} /root/.ssh/known_hosts 2>/dev/null'"
        return ssh_run(MASTER_HOST, cmd, check=False).returncode == 0

    def seed_known_host() -> None:
        cid = dokploy_container_id()
        scan = ssh_run(MASTER_HOST, f"ssh-keyscan -H {shlex.quote(node.tailnet_ip)}")
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"{SSH_USER}@{MASTER_HOST}",
                (
                    f"docker exec -i {cid} sh -c "
                    "'mkdir -p /root/.ssh && cat >> /root/.ssh/known_hosts'"
                ),
            ],
            input=scan.stdout,
            text=True,
            check=True,
            timeout=30,
        )

    return [
        Step("Dokploy server row", has_server, register_server),
        Step(
            "server.validate + server.setup", lambda: False, validate_and_setup_server
        ),
        Step(
            "node's host key in Dokploy's own known_hosts",
            has_known_host,
            seed_known_host,
        ),
    ]


def build_application_steps(node: Node, state: AddState) -> list[Step]:
    def has_application() -> bool:
        existing = find_application(node.app_name)
        if existing is None:
            return False
        state.application_id = existing["applicationId"]
        return True

    def create_application() -> None:
        response = dokploy(
            "POST",
            "application.create",
            json_body={
                "name": node.app_name,
                "description": (
                    f"Worker fleet agent on {node.name}. Outbound-only, no "
                    "domain/port. See docs/worker-fleet-bringup.md."
                ),
                "environmentId": DOKPLOY_ENVIRONMENT_ID,
                "serverId": state.server_id,
            },
        )
        application_id = (response or {}).get("applicationId")
        if not application_id:
            existing = find_application(node.app_name)
            if existing is None:
                msg = (
                    f"application.create for {node.app_name} returned no applicationId"
                )
                raise SystemExit(msg)
            application_id = existing["applicationId"]
        state.application_id = application_id

        dokploy(
            "POST",
            "application.saveGithubProvider",
            json_body={
                "applicationId": application_id,
                "repository": GIT_REPOSITORY,
                "owner": GIT_OWNER,
                "buildPath": "/",
                "githubId": GITHUB_PROVIDER_ID,
                "branch": GIT_BRANCH,
                "triggerType": "push",
            },
        )
        dokploy(
            "POST",
            "application.saveBuildType",
            json_body={
                "applicationId": application_id,
                "buildType": "dockerfile",
                "dockerfile": GIT_DOCKERFILE,
                "dockerContextPath": "/",
                "dockerBuildStage": None,
                "herokuVersion": None,
                "railpackVersion": None,
            },
        )
        dokploy(
            "POST",
            "application.update",
            json_body={"applicationId": application_id, "command": "portal worker"},
        )
        dokploy(
            "POST",
            "application.saveEnvironment",
            json_body={
                "applicationId": application_id,
                "env": "\n".join(
                    [
                        f"PORTAL_WORKER_API_URL={WORKER_API_URL}",
                        f"PORTAL_WORKER_ID={node.worker_id}",
                        f"PORTAL_WORKER_BOOTSTRAP_TOKEN={worker_bootstrap_token()}",
                        f"PORTAL_WORKER_TAILSCALE_HOSTNAME={node.hostname}",
                    ]
                ),
                "buildArgs": None,
                "buildSecrets": None,
                "createEnvFile": False,
            },
        )

    def deploy_application() -> None:
        dokploy(
            "POST",
            "application.deploy",
            json_body={"applicationId": state.application_id},
        )

    return [
        Step(
            f"Dokploy application {node.app_name}", has_application, create_application
        ),
        Step("deploy", lambda: False, deploy_application),
    ]


def build_add_steps(node: Node, state: AddState) -> list[Step]:
    return [
        *build_docker_steps(node),
        *build_swarm_steps(node),
        *build_server_steps(node, state),
        *build_application_steps(node, state),
    ]


def verify_worker_online(node: Node, *, timeout: float = 90) -> None:
    code = (
        "import asyncio, asyncpg\n"
        "from portal.settings import PortalSettings\n"
        "async def main():\n"
        "    settings = PortalSettings.from_environment()\n"
        "    pool = await asyncpg.create_pool(settings.database_dsn)\n"
        "    try:\n"
        "        row = await pool.fetchrow(\n"
        "            'select last_seen_at from portal_workers '\n"
        "            'where worker_id = $1 and revoked_at is null',\n"
        f"            {node.worker_id!r},\n"
        "        )\n"
        "        print(row['last_seen_at'].isoformat() if row and row['last_seen_at'] else '')\n"
        "    finally:\n"
        "        await pool.close()\n"
        "asyncio.run(main())\n"
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last_seen = run_python_in_worker_api(code).strip()
        if last_seen:
            print(f"  {node.worker_id} is online, last_seen_at={last_seen}")
            return
        time.sleep(5)

    msg = (
        f"{node.worker_id} did not report a heartbeat within {timeout:.0f}s. "
        f"Check `application.readLogs` for {node.app_name} in Dokploy."
    )
    raise SystemExit(msg)


def revoke_worker(node: Node) -> None:
    cid = worker_api_container_id()
    result = ssh_run(
        MASTER_HOST,
        f"docker exec {cid} portal enroll-worker --worker-id {node.worker_id} --revoke",
        check=False,
    )
    already_gone = "not enrolled" in result.stderr or "already revoked" in result.stderr
    if result.returncode != 0 and not already_gone:
        msg = f"revoking {node.worker_id} failed: {result.stderr.strip()}"
        raise SystemExit(msg)
    print(f"  done    {(result.stdout or result.stderr).strip()}")


def cmd_add(node: Node, *, dry_run: bool) -> None:
    print(f"add {node.name} ({node.tailnet_ip}) -> {node.hostname}")

    if not dry_run:
        preflight(node)

    state = AddState()
    run_steps(build_add_steps(node, state), dry_run=dry_run)

    if dry_run:
        return

    print("verifying the worker is claiming work...")
    verify_worker_online(node)


def cmd_remove(node: Node, *, dry_run: bool, yes: bool) -> None:
    print(f"remove {node.name}")

    app = find_application(node.app_name)
    server = find_server(node.name)

    print(
        f"  {'would' if dry_run else 'will'}   revoke credential for {node.worker_id}"
    )
    print(
        f"  {'would' if dry_run else 'will'}   stop + delete application "
        f"{node.app_name}" + ("" if app else " (none found, skipping)")
    )
    print(
        f"  {'would' if dry_run else 'will'}   delete server row {node.name}"
        + ("" if server else " (none found, skipping)")
    )

    if dry_run:
        return
    if not yes:
        msg = "pass --yes to actually remove this node"
        raise SystemExit(msg)

    revoke_worker(node)

    if app:
        dokploy(
            "POST",
            "application.stop",
            json_body={"applicationId": app["applicationId"]},
        )
        dokploy(
            "POST",
            "application.delete",
            json_body={"applicationId": app["applicationId"]},
        )
        print(f"  done    deleted application {node.app_name}")

    if server:
        dokploy("POST", "server.remove", json_body={"serverId": server["serverId"]})
        print(f"  done    deleted server row {node.name}")

    print(f"{node.name} removed from the fleet")


def cmd_list() -> None:
    fleet_apps = [
        a
        for a in dokploy(
            "GET",
            "application.search",
            params={
                "projectId": DOKPLOY_PROJECT_ID,
                "environmentId": DOKPLOY_ENVIRONMENT_ID,
                "limit": 100,
            },
        )["items"]
        if a["name"].startswith("portal-worker-")
    ]

    code = (
        "import asyncio, asyncpg\n"
        "from portal.settings import PortalSettings\n"
        "async def main():\n"
        "    settings = PortalSettings.from_environment()\n"
        "    pool = await asyncpg.create_pool(settings.database_dsn)\n"
        "    try:\n"
        "        rows = await pool.fetch(\n"
        "            'select worker_id, tailscale_hostname, last_seen_at, revoked_at '\n"
        "            'from portal_workers order by worker_id'\n"
        "        )\n"
        "        for row in rows:\n"
        "            print(row['worker_id'], row['tailscale_hostname'],\n"
        "                  row['last_seen_at'] or '', row['revoked_at'] or '', sep='|')\n"
        "    finally:\n"
        "        await pool.close()\n"
        "asyncio.run(main())\n"
    )

    header = f"{'worker_id':<14}{'app':<6}{'status':<10}{'last_seen_at':<36}revoked"
    print(header)
    for line in run_python_in_worker_api(code).strip().splitlines():
        worker_id, _hostname, last_seen, revoked = line.split("|")
        node_name = worker_id.rsplit("-", 1)[0]
        app = next(
            (a for a in fleet_apps if a["name"] == f"portal-worker-{node_name}"), None
        )
        status = app["applicationStatus"] if app else "-"
        print(
            f"{worker_id:<14}{'yes' if app else 'no':<6}{status:<10}"
            f"{last_seen or '-':<36}{revoked or '-'}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Show every fleet node's state in one shot.")

    add_parser = subparsers.add_parser("add", help="Add a node to the fleet.")
    add_parser.add_argument("name")
    add_parser.add_argument("tailnet_ip")
    add_parser.add_argument("--dry-run", action="store_true")

    remove_parser = subparsers.add_parser(
        "remove", help="Remove a node from the fleet."
    )
    remove_parser.add_argument("name")
    remove_parser.add_argument("--dry-run", action="store_true")
    remove_parser.add_argument("--yes", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        cmd_list()
    elif args.command == "add":
        cmd_add(Node(args.name, args.tailnet_ip), dry_run=args.dry_run)
    elif args.command == "remove":
        cmd_remove(Node(args.name), dry_run=args.dry_run, yes=args.yes)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(f"command failed: {error.stderr or error}")
    except httpx.HTTPStatusError as error:
        sys.exit(
            f"Dokploy API error: {error.response.status_code} {error.response.text}"
        )
