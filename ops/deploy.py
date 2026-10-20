r"""Deploy a portal (core plus workers) into Dokploy, finding every object by name.

Usage, with DOKPLOY_URL and DOKPLOY_API_KEY in the environment (or
DOKPLOY_SESSION_COOKIE, a Cookie header value, in place of the key):

    uv run python -m ops.deploy deploy NAME --image REF --env-file PATH \
        [--worker WORKER=ADDRESS ...] [--no-deploy]
    uv run python -m ops.deploy remove-worker NAME WORKER
    uv run python -m ops.deploy rotate-key NAME VERSION
    uv run python -m ops.deploy worker issue NAME WORKER [--tailscale-hostname HOST]
    uv run python -m ops.deploy worker revoke NAME WORKER
    uv run python -m ops.deploy list [NAME]

REGISTRY_USERNAME and REGISTRY_PASSWORD, when set, log every server that pulls
the image into its registry. docs/portal-deployment.md describes the inputs.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
CORE_COMPOSE = ROOT / "docker-compose.core.yml"
WORKER_COMPOSE = ROOT / "docker-compose.worker.yml"

CORE = "core"
WEB_SERVICE = "web"
WEB_PORT = 8000
WORKER_API_SERVICE = "worker-api"
DEFAULT_WORKER_API_PORT = "8443"

# Dokploy appends "-" and six characters to an appName, and Compose project
# names must be lowercase. These bounds keep "<name>-worker-<worker>" within
# Dokploy's 63-character appName limit.
DEPLOYMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
WORKER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

FINISHED = frozenset({"done", "error", "cancelled"})

# The init service rotates the master key before web and worker-api start.
# Compose recreates all three services when this value changes.
KEY_VERSION_VARIABLE = "PORTAL_MASTER_KEY_VERSION"
KEY_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")
REWRAP_SCHEDULE = "rewrap"

Row = dict[str, Any]


class DeployError(RuntimeError):
    pass


class Dokploy:
    """Dokploy's tRPC procedures as exposed under /api: GET for queries with
    their input in the query string, POST for mutations with a JSON body."""

    def __init__(
        self,
        url: str,
        credentials: Mapping[str, str],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=f"{url.rstrip('/')}/api/",
            headers=credentials,
            # server.validate and server.setup hold the request open while
            # they run a script on the remote server over SSH.
            timeout=httpx.Timeout(30, read=600),
            transport=transport,
        )

    def query(self, procedure: str, **params: str) -> Any:
        return self._call("GET", procedure, params=params)

    def mutate(self, procedure: str, **body: Any) -> Any:
        return self._call("POST", procedure, json=body)

    def _call(self, method: str, procedure: str, **kwargs: Any) -> Any:
        response = self._client.request(method, procedure, **kwargs)

        if response.is_error:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            msg = f"{procedure}: HTTP {response.status_code}: {detail}"
            raise DeployError(msg)

        return response.json() if response.content else None


@dataclass(frozen=True)
class Worker:
    name: str
    address: str

    @classmethod
    def parse(cls, spec: str) -> Worker:
        name, separator, address = spec.partition("=")

        if not separator or not address or not WORKER_NAME.fullmatch(name):
            msg = (
                f"worker {spec!r}: expected NAME=ADDRESS, with NAME matching "
                f"{WORKER_NAME.pattern}"
            )
            raise DeployError(msg)

        return cls(name, address)


@dataclass(frozen=True)
class Registry:
    url: str
    username: str
    password: str

    @classmethod
    def for_image(cls, image: str, environ: Mapping[str, str]) -> Registry | None:
        """Return credentials only for registry-hosted images with both values."""
        first, separator, _ = image.partition("/")
        is_host = first == "localhost" or "." in first or ":" in first
        username = environ.get("REGISTRY_USERNAME")
        password = environ.get("REGISTRY_PASSWORD")

        if not separator or not is_host or not username or not password:
            return None

        return cls(first, username, password)


@dataclass(frozen=True)
class Spec:
    name: str
    image: str
    env: dict[str, str]
    workers: tuple[Worker, ...] = ()

    def __post_init__(self) -> None:
        if not DEPLOYMENT_NAME.fullmatch(self.name):
            msg = f"deployment name {self.name!r} must match {DEPLOYMENT_NAME.pattern}"
            raise DeployError(msg)

        if "PORTAL_IMAGE" in self.env:
            msg = "the env file sets PORTAL_IMAGE; pass the image with --image instead"
            raise DeployError(msg)

        if KEY_VERSION_VARIABLE in self.env:
            msg = f"the env file sets {KEY_VERSION_VARIABLE}; `rotate-key` owns it"
            raise DeployError(msg)

        required = ["PORTAL_PUBLIC_ORIGIN"]
        if self.workers:
            required += [
                "PORTAL_WORKER_BOOTSTRAP_TOKEN",
                "PORTAL_WORKER_DATABASE_ADDRESS",
            ]

        if missing := [key for key in required if not self.env.get(key)]:
            msg = f"the env file does not set {', '.join(missing)}"
            raise DeployError(msg)

        if len({worker.name for worker in self.workers}) != len(self.workers):
            msg = "worker names must be unique"
            raise DeployError(msg)

    @property
    def public_host(self) -> str:
        host = urlsplit(self.env["PORTAL_PUBLIC_ORIGIN"]).hostname

        if not host:
            msg = "PORTAL_PUBLIC_ORIGIN has no host"
            raise DeployError(msg)

        return host

    def core_env(self) -> str:
        return render_env({"PORTAL_IMAGE": self.image, **self.env})

    def worker_env(self, worker: Worker) -> str:
        # Workers reach worker-api on the host they reach PostgreSQL on.
        address = self.env["PORTAL_WORKER_DATABASE_ADDRESS"]
        host, _, port = address.rpartition(":")
        if not port.isdigit():
            host = address
        api_port = self.env.get("WORKER_API_PUBLISHED_PORT") or DEFAULT_WORKER_API_PORT

        return render_env(
            {
                "PORTAL_IMAGE": self.image,
                "PORTAL_WORKER_API_URL": f"http://{host}:{api_port}",
                "PORTAL_WORKER_BOOTSTRAP_TOKEN": self.env[
                    "PORTAL_WORKER_BOOTSTRAP_TOKEN"
                ],
                "PORTAL_WORKER_ID": worker.name,
                "PORTAL_WORKER_TAILSCALE_HOSTNAME": worker.name,
            }
        )


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        match = ENV_LINE.fullmatch(line)
        if match is None:
            msg = f"env file line {number}: expected KEY=VALUE"
            raise DeployError(msg)

        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key] = value

    return env


def render_env(env: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in env.items())


def compose_name(worker: str) -> str:
    return f"worker-{worker}"


# Dokploy server names are global across projects. Neither name pattern allows
# "/", so the pair is recoverable and no two deployments share a server name.
SERVER_SEPARATOR = "/"


def server_name(deployment: str, worker: str) -> str:
    return f"{deployment}{SERVER_SEPARATOR}{worker}"


def revoke_schedule_name(worker: str) -> str:
    return f"revoke-{worker}"


def by_name(rows: Iterable[Row], name: str) -> Row | None:
    return next((row for row in rows if row["name"] == name), None)


class Deployer:
    def __init__(
        self,
        api: Dokploy,
        *,
        log: Callable[[str], None] = print,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 1800.0,
        registry: Registry | None = None,
    ) -> None:
        self._api = api
        self._log = log
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._registry = registry

    def deploy(self, spec: Spec, *, start: bool = True) -> None:
        """Create or update every object, then deploy what changed or never
        finished. With start False it deploys nothing, and the next run with
        start True deploys every compose that has not run."""
        environment = self._ensure_environment(spec.name)

        core_env = spec.core_env()
        if version := self._key_version(environment):
            core_env += render_env({KEY_VERSION_VARIABLE: version})

        core_id, changed = self._compose(
            environment,
            CORE,
            app_name=f"{spec.name}-{CORE}",
            compose_file=CORE_COMPOSE.read_text(encoding="utf-8"),
            env=core_env,
        )
        changed |= self._web_domain(core_id, spec.public_host)
        if start:
            self._deploy_if_needed(
                core_id, CORE, changed=changed, registry_name=spec.name
            )

        if not spec.workers:
            return

        ssh_key_id = self._ssh_key(spec.name)

        for worker in spec.workers:
            server_id = self._server(
                server_name(spec.name, worker.name), worker.address, ssh_key_id
            )
            name = compose_name(worker.name)
            compose_id, changed = self._compose(
                environment,
                name,
                app_name=f"{spec.name}-{name}",
                compose_file=WORKER_COMPOSE.read_text(encoding="utf-8"),
                env=spec.worker_env(worker),
                server_id=server_id,
            )
            if start:
                self._deploy_if_needed(
                    compose_id,
                    name,
                    changed=changed,
                    registry_name=server_name(spec.name, worker.name),
                    server_id=server_id,
                )

    def rotate_key(self, deployment: str, version: str) -> None:
        """Activate `version` and rewrap stored secrets under the new key.

        The core restarts first, so every process loads the new key before any
        row is rewrapped. A rerun finishes an interrupted rotation.
        """
        if not KEY_VERSION.fullmatch(version):
            msg = f"key version {version!r} must match {KEY_VERSION.pattern}"
            raise DeployError(msg)

        environment = self._existing_environment(deployment)
        core = self._core(environment, deployment)
        current = self._api.query("compose.one", composeId=core["composeId"])
        env = parse_env(current.get("env") or "")
        changed = env.get(KEY_VERSION_VARIABLE) != version

        if changed:
            self._api.mutate(
                "compose.update",
                composeId=core["composeId"],
                env=render_env({**env, KEY_VERSION_VARIABLE: version}),
            )
            self._log(f"update  compose {CORE}: master key {version}")

        self._deploy_if_needed(core["composeId"], CORE, changed=changed)
        self._run_schedule(
            core["composeId"],
            REWRAP_SCHEDULE,
            "portal-admin key rewrap",
            verb="rewrap",
            target="master key",
            once=False,
        )

    def remove_worker(self, deployment: str, worker: str) -> None:
        environment = self._existing_environment(deployment)
        core = self._core(environment, deployment)

        compose = by_name(environment["compose"], compose_name(worker))
        server = by_name(self._api.query("server.all"), server_name(deployment, worker))
        entry = self._registry_entry(server_name(deployment, worker))
        schedule = by_name(
            self._api.query(
                "schedule.list", id=core["composeId"], scheduleType="compose"
            ),
            revoke_schedule_name(worker),
        )

        if compose is None and server is None and schedule is None and entry is None:
            self._log(f"ok      {worker} is not deployed")
            return

        # A running agent would enroll again with the bootstrap token, which
        # clears the revocation, so it stops before the revoke.
        if compose is not None:
            self._api.mutate("compose.stop", composeId=compose["composeId"])
            self._log(f"stop    compose {compose['name']}")

        self._run_schedule(
            core["composeId"],
            revoke_schedule_name(worker),
            f"portal-admin worker revoke --worker-id {worker}",
            verb="revoke",
            target=worker,
            once=True,
        )

        if compose is not None:
            self._api.mutate(
                "compose.delete", composeId=compose["composeId"], deleteVolumes=True
            )
            self._log(f"delete  compose {compose['name']}")

        if server is not None:
            self._api.mutate("server.remove", serverId=server["serverId"])
            self._log(f"delete  server {server['name']}")

        if entry is not None:
            self._api.mutate("registry.remove", registryId=entry["registryId"])
            self._log(f"delete  registry {entry['registryName']}")

        schedule = by_name(
            self._api.query(
                "schedule.list", id=core["composeId"], scheduleType="compose"
            ),
            revoke_schedule_name(worker),
        )
        if schedule is not None:
            self._api.mutate("schedule.delete", scheduleId=schedule["scheduleId"])

    def describe(self, deployment: str | None) -> list[str]:
        """One tab-separated line per object: kind, name, appName, status."""
        lines = []

        for project in self._api.query("project.all"):
            if deployment is not None and project["name"] != deployment:
                continue

            lines.append(f"project\t{project['name']}\t\t")
            for environment in project["environments"]:
                for compose in environment["compose"]:
                    detail = self._api.query(
                        "compose.one", composeId=compose["composeId"]
                    )
                    lines.append(
                        f"compose\t{compose['name']}\t{detail['appName']}\t"
                        f"{compose['composeStatus']}"
                    )

            prefix = server_name(project["name"], "")
            lines.extend(
                f"server\t{server['name']}\t{server['ipAddress']}\t"
                f"{server.get('serverStatus', '')}"
                for server in self._api.query("server.all")
                if server["name"].startswith(prefix)
            )

        return lines

    def _environment(self, deployment: str) -> Row | None:
        project = by_name(self._api.query("project.all"), deployment)

        if project is None:
            return None

        return next(env for env in project["environments"] if env["isDefault"])

    def _ensure_environment(self, deployment: str) -> Row:
        if (environment := self._environment(deployment)) is not None:
            return environment

        created = self._api.mutate("project.create", name=deployment)
        self._log(f"create  project {deployment}")
        return {**created["environment"], "compose": []}

    def _compose(
        self,
        environment: Row,
        name: str,
        *,
        app_name: str,
        compose_file: str,
        env: str,
        server_id: str | None = None,
    ) -> tuple[str, bool]:
        existing = by_name(environment["compose"], name)

        if existing is None:
            created = self._api.mutate(
                "compose.create",
                name=name,
                environmentId=environment["environmentId"],
                composeType="docker-compose",
                appName=app_name,
                **({"serverId": server_id} if server_id else {}),
            )
            compose_id = str(created["composeId"])
            self._log(f"create  compose {name}")
        else:
            compose_id = str(existing["composeId"])

        current = self._api.query("compose.one", composeId=compose_id)
        wanted = {"sourceType": "raw", "composeFile": compose_file, "env": env}

        if all(current.get(key) == value for key, value in wanted.items()):
            return compose_id, False

        self._api.mutate("compose.update", composeId=compose_id, **wanted)
        self._log(f"update  compose {name}")
        return compose_id, True

    def _web_domain(self, compose_id: str, host: str) -> bool:
        # TLS ends before Traefik (Cloudflare's edge, or `tailscale serve`), so
        # the route is plain HTTP and Dokploy requests no certificate.
        wanted = {
            "host": host,
            "path": "/",
            "port": WEB_PORT,
            "https": False,
            "certificateType": "none",
            "serviceName": WEB_SERVICE,
            "domainType": "compose",
        }
        domains = self._api.query("domain.byComposeId", composeId=compose_id)
        domain = next(
            (row for row in domains if row["serviceName"] == WEB_SERVICE), None
        )

        if domain is None:
            self._api.mutate("domain.create", composeId=compose_id, **wanted)
            self._log(f"create  domain {host}")
            return True

        if all(domain.get(key) == value for key, value in wanted.items()):
            return False

        self._api.mutate("domain.update", domainId=domain["domainId"], **wanted)
        self._log(f"update  domain {host}")
        return True

    def _ssh_key(self, deployment: str) -> str:
        key = by_name(self._api.query("sshKey.all"), deployment)

        if key is None:
            pair = self._api.mutate("sshKey.generate", type="ed25519")
            organization = self._api.query("organization.active")
            self._api.mutate(
                "sshKey.create",
                name=deployment,
                description="Dokploy's key for this deployment's worker servers",
                privateKey=pair["privateKey"],
                publicKey=pair["publicKey"],
                organizationId=organization["id"],
            )
            self._log(f"create  ssh key {deployment}: {pair['publicKey']}")
            # sshKey.create returns nothing, so the new key is found by name.
            key = by_name(self._api.query("sshKey.all"), deployment)

        if key is None:
            msg = f"sshKey.create stored no key named {deployment!r}"
            raise DeployError(msg)

        return str(key["sshKeyId"])

    def _server(self, name: str, address: str, ssh_key_id: str) -> str:
        wanted = {
            "name": name,
            "description": "portal worker",
            "ipAddress": address,
            "port": 22,
            "username": "root",
            "sshKeyId": ssh_key_id,
            "serverType": "deploy",
        }
        server = by_name(self._api.query("server.all"), name)

        if server is None:
            server = self._api.mutate("server.create", **wanted)
            self._log(f"create  server {name}")
        elif any(server.get(key) != value for key, value in wanted.items()):
            self._api.mutate("server.update", serverId=server["serverId"], **wanted)
            self._log(f"update  server {name}")

        server_id = str(server["serverId"])

        # A worker compose needs only Docker. server.setup would also install
        # Traefik, Swarm and image builders that a worker never uses.
        status = self._api.query("server.validate", serverId=server_id)
        if not status["docker"]["enabled"]:
            msg = f"server {name} ({address}) has no Docker"
            raise DeployError(msg)

        return server_id

    def _deploy_if_needed(
        self,
        compose_id: str,
        name: str,
        *,
        changed: bool,
        registry_name: str | None = None,
        server_id: str | None = None,
    ) -> None:
        deployments = self._api.query("deployment.allByCompose", composeId=compose_id)

        if not changed and deployments and deployments[0]["status"] == "done":
            self._log(f"ok      compose {name}")
            return

        if registry_name is not None:
            self._login(registry_name, server_id)

        seen = {row["deploymentId"] for row in deployments}
        self._api.mutate("compose.deploy", composeId=compose_id)
        self._log(f"deploy  compose {name}")

        status = self._await(
            lambda: self._api.query("deployment.allByCompose", composeId=compose_id),
            seen,
            f"compose {name}",
        )
        if status != "done":
            msg = f"compose {name}: deployment ended {status}; see its log in Dokploy"
            raise DeployError(msg)

    def _run_schedule(
        self,
        core_id: str,
        name: str,
        command: str,
        *,
        verb: str,
        target: str,
        once: bool,
    ) -> None:
        """Run `command` in worker-api and retain the schedule run record.

        With `once`, a finished run means the command already happened. The
        portal treats a second revoke as an error.
        """
        schedule = by_name(
            self._api.query("schedule.list", id=core_id, scheduleType="compose"),
            name,
        )

        if schedule is None:
            schedule = self._api.mutate(
                "schedule.create",
                name=name,
                # Never due: the schedule is disabled and only run manually.
                cronExpression="0 0 1 1 *",
                enabled=False,
                scheduleType="compose",
                composeId=core_id,
                serviceName=WORKER_API_SERVICE,
                shellType="sh",
                command=command,
            )

        runs = self._api.query(
            "deployment.allByType", id=schedule["scheduleId"], type="schedule"
        )
        if once and any(run["status"] == "done" for run in runs):
            self._log(f"ok      {verb} {target}")
            return

        result = self._api.mutate(
            "schedule.runManually", scheduleId=schedule["scheduleId"]
        )
        if result["status"] != "done":
            msg = f"{verb} {target}: the schedule run ended {result['status']}"
            raise DeployError(msg)

        self._log(f"{verb:<8}{target}")

    def _login(self, name: str, server_id: str | None) -> None:
        """Log the image-pulling server into the registry through Dokploy.

        Dokploy runs `docker login` whenever it creates or updates an entry, so
        updating an unchanged entry repeats the login.
        """
        registry = self._registry
        if registry is None:
            return

        wanted = {
            "registryName": name,
            "registryType": "cloud",
            "registryUrl": registry.url,
            "username": registry.username,
            "password": registry.password,
            **({"serverId": server_id} if server_id else {}),
        }
        entry = self._registry_entry(name)

        if entry is None:
            self._api.mutate("registry.create", **wanted)
            self._log(f"create  registry {name}")
        else:
            self._api.mutate(
                "registry.update", registryId=entry["registryId"], **wanted
            )
            self._log(f"login   registry {name}")

    def _registry_entry(self, name: str) -> Row | None:
        return next(
            (
                row
                for row in self._api.query("registry.all")
                if row["registryName"] == name
            ),
            None,
        )

    def _existing_environment(self, deployment: str) -> Row:
        environment = self._environment(deployment)

        if environment is None:
            msg = f"no Dokploy project named {deployment!r}"
            raise DeployError(msg)

        return environment

    def _core(self, environment: Row, deployment: str) -> Row:
        core = by_name(environment["compose"], CORE)

        if core is None:
            msg = f"{deployment} has no {CORE} compose"
            raise DeployError(msg)

        return core

    def _key_version(self, environment: Row) -> str | None:
        """The master key version `rotate-key` last set, kept across deploys."""
        core = by_name(environment["compose"], CORE)

        if core is None:
            return None

        current = self._api.query("compose.one", composeId=core["composeId"])
        return parse_env(current.get("env") or "").get(KEY_VERSION_VARIABLE)

    def core_app_name(self, deployment: str) -> str:
        """The Compose project name of the core, which labels its containers."""
        core = self._core(self._existing_environment(deployment), deployment)
        return str(
            self._api.query("compose.one", composeId=core["composeId"])["appName"]
        )

    def _await(self, fetch: Callable[[], list[Row]], seen: set[str], label: str) -> str:
        # The queue records a deployment only when a worker picks the job up.
        deadline = time.monotonic() + self._timeout_seconds

        while time.monotonic() < deadline:
            for row in fetch():
                if row["deploymentId"] not in seen and row["status"] in FINISHED:
                    return str(row["status"])
            time.sleep(self._poll_seconds)

        msg = f"{label}: no finished deployment after {self._timeout_seconds:.0f} s"
        raise DeployError(msg)


def admin_exec(app_name: str, arguments: Sequence[str]) -> int:
    """Run `portal-admin` in the core's worker-api container and return its
    exit status. The container is found by the Compose labels of `app_name`,
    on the host the Docker CLI talks to: the local one, or DOCKER_HOST's.

    This runs through Docker rather than a Dokploy schedule because `worker
    issue` prints credentials once, and Dokploy would keep that output in its
    deployment logs."""
    filters = (
        f"label=com.docker.compose.project={app_name}",
        f"label=com.docker.compose.service={WORKER_API_SERVICE}",
    )
    command = ["docker", "ps", "--quiet"]
    for item in filters:
        command += ["--filter", item]

    try:
        found = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        msg = "docker is not installed on this host"
        raise DeployError(msg) from error

    if found.returncode != 0:
        msg = f"docker ps failed: {found.stderr.strip()}"
        raise DeployError(msg)

    containers = found.stdout.split()
    if len(containers) != 1:
        msg = (
            f"expected one running {WORKER_API_SERVICE} container in Compose "
            f"project {app_name}, found {len(containers)}. Run this on the "
            "Dokploy host, or point DOCKER_HOST at it."
        )
        raise DeployError(msg)

    return subprocess.run(
        ["docker", "exec", containers[0], "portal-admin", *arguments], check=False
    ).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ops.deploy", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    deploy = commands.add_parser("deploy", help="create or update a whole deployment")
    deploy.add_argument("name", help="the deployment name, also its Dokploy project")
    deploy.add_argument("--image", required=True, help="the portal image reference")
    deploy.add_argument(
        "--env-file", required=True, type=Path, help="the core env file"
    )
    deploy.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="NAME=ADDRESS",
        help="a worker node: its tailnet machine name and the address Dokploy SSHes to",
    )
    deploy.add_argument(
        "--no-deploy",
        action="store_true",
        help="create or update the objects but start nothing",
    )

    remove = commands.add_parser("remove-worker", help="revoke and delete one worker")
    remove.add_argument("name")
    remove.add_argument("worker")

    rotate = commands.add_parser(
        "rotate-key", help="activate a new master key and rewrap stored secrets"
    )
    rotate.add_argument("name")
    rotate.add_argument("version", help="the new key's version, such as v2")

    worker = commands.add_parser(
        "worker", help="issue or revoke a worker's fixed credentials"
    )
    actions = worker.add_subparsers(dest="action", required=True)
    issue = actions.add_parser("issue", help="issue credentials, shown once")
    issue.add_argument(
        "--tailscale-hostname",
        help="the node's tailnet name (default: the worker name)",
    )
    actions.add_parser("revoke", help="revoke a worker's credentials")
    for action in actions.choices.values():
        action.add_argument("name")
        action.add_argument("worker")

    listing = commands.add_parser("list", help="print the objects of deployments")
    listing.add_argument("name", nargs="?")

    return parser


def dokploy_credentials(environ: Mapping[str, str]) -> dict[str, str]:
    """The API key header, else the Cookie header of a signed-in session. The
    session lets the end-to-end test deploy without an API key, whose creation
    has no stable public route."""
    if api_key := environ.get("DOKPLOY_API_KEY"):
        return {"x-api-key": api_key}

    if cookie := environ.get("DOKPLOY_SESSION_COOKIE"):
        return {"cookie": cookie}

    return {}


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    args = build_parser().parse_args(argv)

    url = os.environ.get("DOKPLOY_URL")
    credentials = dokploy_credentials(os.environ)
    if not url or not credentials:
        sys.exit(
            "DOKPLOY_URL and DOKPLOY_API_KEY (or DOKPLOY_SESSION_COOKIE) must be set."
        )

    registry = (
        Registry.for_image(args.image, os.environ) if args.command == "deploy" else None
    )
    deployer = Deployer(
        Dokploy(url, credentials, transport=transport), registry=registry
    )

    try:
        if args.command == "deploy":
            spec = Spec(
                name=args.name,
                image=args.image,
                env=parse_env(args.env_file.read_text(encoding="utf-8")),
                workers=tuple(Worker.parse(spec) for spec in args.worker),
            )
            deployer.deploy(spec, start=not args.no_deploy)
        elif args.command == "remove-worker":
            deployer.remove_worker(args.name, args.worker)
        elif args.command == "rotate-key":
            deployer.rotate_key(args.name, args.version)
        elif args.command == "worker":
            admin = ["worker", args.action, "--worker-id", args.worker]
            if args.action == "issue":
                admin += [
                    "--tailscale-hostname",
                    args.tailscale_hostname or args.worker,
                ]
            sys.exit(admin_exec(deployer.core_app_name(args.name), admin))
        else:
            print("\n".join(deployer.describe(args.name)))
    except (DeployError, httpx.HTTPError) as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
