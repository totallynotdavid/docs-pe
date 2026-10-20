"""An in-memory Dokploy v0.30.7 API, covering the procedures ops.deploy calls.

Required inputs and response shapes follow Dokploy's routers and openapi.json
for that release. A deployment finishes on the first poll after it is queued.
"""

from __future__ import annotations

import itertools
import json

from dataclasses import dataclass, field
from typing import Any, NoReturn

import httpx


Row = dict[str, Any]

REQUIRED: dict[str, tuple[str, ...]] = {
    "project.create": ("name",),
    "compose.create": ("name", "environmentId"),
    "compose.one": ("composeId",),
    "compose.update": ("composeId",),
    "compose.deploy": ("composeId",),
    "compose.stop": ("composeId",),
    "compose.delete": ("composeId", "deleteVolumes"),
    "deployment.allByCompose": ("composeId",),
    "deployment.allByType": ("id", "type"),
    "domain.byComposeId": ("composeId",),
    "domain.create": ("host",),
    "domain.update": ("host", "domainId"),
    "sshKey.generate": ("type",),
    "sshKey.create": ("name", "privateKey", "publicKey", "organizationId"),
    "server.create": (
        "name", "description", "ipAddress", "port", "username", "sshKeyId", "serverType",
    ),
    "server.update": (
        "name", "description", "serverId", "ipAddress", "port", "username",
        "sshKeyId", "serverType",
    ),
    "server.validate": ("serverId",),
    "server.remove": ("serverId",),
    "schedule.list": ("id", "scheduleType"),
    "schedule.create": ("name", "cronExpression", "command"),
    "schedule.runManually": ("scheduleId",),
    "schedule.delete": ("scheduleId",),
    "registry.create": (
        "registryName", "username", "password", "registryUrl", "registryType",
    ),
    "registry.update": ("registryId",),
    "registry.remove": ("registryId",),
}  # fmt: skip


class RejectedError(Exception):
    pass


def reject(message: str) -> NoReturn:
    raise RejectedError(message)


@dataclass
class FakeDokploy:
    api_key: str = "test-key"
    session_cookie: str = "better-auth.session_token=test-session"
    organization_id: str = "org"
    docker_hosts: set[str] = field(default_factory=set)
    failing_composes: set[str] = field(default_factory=set)
    failing_commands: set[str] = field(default_factory=set)

    projects: list[Row] = field(default_factory=list)
    composes: dict[str, Row] = field(default_factory=dict)
    deployments: list[Row] = field(default_factory=list)
    domains: list[Row] = field(default_factory=list)
    ssh_keys: list[Row] = field(default_factory=list)
    servers: list[Row] = field(default_factory=list)
    schedules: list[Row] = field(default_factory=list)
    registries: list[Row] = field(default_factory=list)
    # Each `docker login` Dokploy ran: (serverId or None for the Dokploy
    # host, registry URL, username).
    logins: list[tuple[str | None, str, str]] = field(default_factory=list)
    refused_passwords: set[str] = field(default_factory=set)
    queued: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)

    _ids: itertools.count[int] = field(default_factory=itertools.count)

    @property
    def credentials(self) -> dict[str, str]:
        return {"x-api-key": self.api_key}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def mutations(self) -> list[str]:
        return [call for call in self.calls if call.startswith("POST ")]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if (
            request.headers.get("x-api-key") != self.api_key
            and request.headers.get("cookie") != self.session_cookie
        ):
            return httpx.Response(401, json={"message": "Unauthorized"})

        procedure = request.url.path.removeprefix("/api/")
        if request.method == "GET":
            data: Row = dict(request.url.params)
        else:
            data = json.loads(request.content or b"{}")
        self.calls.append(f"{request.method} {procedure}")

        router, _, name = procedure.partition(".")
        method = getattr(self, f"{router}_{name}", None) if name else None
        if method is None:
            return httpx.Response(404, json={"message": f"no procedure {procedure}"})

        if missing := [key for key in REQUIRED.get(procedure, ()) if key not in data]:
            return httpx.Response(400, json={"message": f"missing {missing}"})

        try:
            result = method(data)
        except RejectedError as error:
            return httpx.Response(400, json={"message": str(error)})

        return httpx.Response(200, json=result)

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def _environment(self, environment_id: str) -> Row:
        for project in self.projects:
            for environment in project["environments"]:
                if environment["environmentId"] == environment_id:
                    return environment
        reject(environment_id)

    def organization_active(self, _: Row) -> Row:
        return {"id": self.organization_id, "name": "Organization"}

    def project_all(self, _: Row) -> list[Row]:
        return self.projects

    def project_create(self, data: Row) -> Row:
        environment = {
            "environmentId": self._id("env"),
            "name": "production",
            "isDefault": True,
            "compose": [],
        }
        project = {
            "projectId": self._id("project"),
            "name": data["name"],
            "environments": [environment],
        }
        self.projects.append(project)
        return {"project": project, "environment": environment}

    def compose_create(self, data: Row) -> Row:
        app_name = f"{data['appName']}-abc123"
        if any(compose["appName"] == app_name for compose in self.composes.values()):
            reject("Service with this 'AppName' already exists")

        if "serverId" in data and not any(
            server["serverId"] == data["serverId"] for server in self.servers
        ):
            reject("no such server")

        compose = {
            "composeId": self._id("compose"),
            "name": data["name"],
            "appName": app_name,
            "environmentId": data["environmentId"],
            "serverId": data.get("serverId"),
            "sourceType": "github",
            "composeFile": "",
            "env": None,
            "composeStatus": "idle",
        }
        self.composes[compose["composeId"]] = compose
        self._environment(data["environmentId"])["compose"].append(
            {key: compose[key] for key in ("composeId", "name", "composeStatus")}
        )
        return compose

    def compose_one(self, data: Row) -> Row:
        return self.composes[data["composeId"]]

    def compose_update(self, data: Row) -> Row:
        compose = self.composes[data["composeId"]]
        if data.get("sourceType") not in {None, "git", "github", "raw"}:
            reject("sourceType")
        compose.update(data)
        return compose

    def compose_deploy(self, data: Row) -> Row:
        self.queued.append(data["composeId"])
        return {"success": True, "message": "Deployment queued"}

    def compose_stop(self, data: Row) -> Row:
        self.composes[data["composeId"]]["composeStatus"] = "idle"
        return {}

    def compose_delete(self, data: Row) -> Row:
        compose = self.composes.pop(data["composeId"])
        environment = self._environment(compose["environmentId"])
        environment["compose"] = [
            row
            for row in environment["compose"]
            if row["composeId"] != compose["composeId"]
        ]
        self.schedules = [
            row for row in self.schedules if row["composeId"] != compose["composeId"]
        ]
        return compose

    def deployment_allByCompose(self, data: Row) -> list[Row]:
        self._run_queue()
        rows = [
            row for row in self.deployments if row.get("composeId") == data["composeId"]
        ]
        return rows[::-1]

    def deployment_allByType(self, data: Row) -> list[Row]:
        if data["type"] != "schedule":
            reject(data["type"])
        rows = [row for row in self.deployments if row.get("scheduleId") == data["id"]]
        return rows[::-1]

    def _run_queue(self) -> None:
        for compose_id in self.queued:
            compose = self.composes[compose_id]
            status = "error" if compose["name"] in self.failing_composes else "done"
            self.deployments.append(
                {
                    "deploymentId": self._id("deployment"),
                    "composeId": compose_id,
                    "status": status,
                    "env": compose["env"],
                    "composeFile": compose["composeFile"],
                }
            )
            compose["composeStatus"] = status
            for environment in (
                env for project in self.projects for env in project["environments"]
            ):
                for row in environment["compose"]:
                    if row["composeId"] == compose_id:
                        row["composeStatus"] = status
        self.queued.clear()

    def domain_byComposeId(self, data: Row) -> list[Row]:
        return [row for row in self.domains if row["composeId"] == data["composeId"]]

    def domain_create(self, data: Row) -> Row:
        if data.get("domainType") != "compose" or not data.get("serviceName"):
            reject("a compose domain needs a service")
        domain = {"domainId": self._id("domain"), **data}
        self.domains.append(domain)
        return domain

    def domain_update(self, data: Row) -> Row:
        domain = next(
            row for row in self.domains if row["domainId"] == data["domainId"]
        )
        domain.update(data)
        return domain

    def sshKey_all(self, _: Row) -> list[Row]:
        return self.ssh_keys

    def sshKey_generate(self, data: Row) -> Row:
        return {
            "privateKey": f"private-{data['type']}",
            "publicKey": f"ssh-{data['type']} public dokploy",
        }

    def sshKey_create(self, data: Row) -> None:
        if data["organizationId"] != self.organization_id:
            reject("organizationId")
        self.ssh_keys.append(
            {
                "sshKeyId": self._id("key"),
                "name": data["name"],
                "publicKey": data["publicKey"],
                "organizationId": data["organizationId"],
            }
        )

    def server_all(self, _: Row) -> list[Row]:
        return self.servers

    def server_create(self, data: Row) -> Row:
        if not any(key["sshKeyId"] == data["sshKeyId"] for key in self.ssh_keys):
            reject("no such ssh key")
        server = {"serverId": self._id("server"), "serverStatus": "active", **data}
        self.servers.append(server)
        return server

    def server_update(self, data: Row) -> Row:
        server = next(
            row for row in self.servers if row["serverId"] == data["serverId"]
        )
        server.update(data)
        return server

    def server_validate(self, data: Row) -> Row:
        server = next(
            row for row in self.servers if row["serverId"] == data["serverId"]
        )
        return {
            "docker": {
                "enabled": server["ipAddress"] in self.docker_hosts,
                "version": "",
            },
            "isDokployNetworkInstalled": False,
            "isSwarmInstalled": False,
            "isMainDirectoryInstalled": False,
        }

    def server_remove(self, data: Row) -> Row:
        if any(
            compose["serverId"] == data["serverId"]
            for compose in self.composes.values()
        ):
            reject("Server has active services, please delete them first")
        server = next(
            row for row in self.servers if row["serverId"] == data["serverId"]
        )
        self.servers.remove(server)
        return server

    def schedule_list(self, data: Row) -> list[Row]:
        if data["scheduleType"] != "compose":
            reject(data["scheduleType"])
        return [row for row in self.schedules if row["composeId"] == data["id"]]

    def schedule_create(self, data: Row) -> Row:
        if data.get("scheduleType") != "compose" or data.get("shellType") not in {
            "bash",
            "sh",
        }:
            reject("scheduleType or shellType")
        schedule = {"scheduleId": self._id("schedule"), **data}
        self.schedules.append(schedule)
        return schedule

    def schedule_runManually(self, data: Row) -> Row:
        schedule = next(
            row for row in self.schedules if row["scheduleId"] == data["scheduleId"]
        )
        self.commands_run.append(f"{schedule['serviceName']}: {schedule['command']}")
        status = "error" if schedule["command"] in self.failing_commands else "done"
        deployment = {
            "deploymentId": self._id("deployment"),
            "scheduleId": schedule["scheduleId"],
            "status": status,
        }
        self.deployments.append(deployment)
        if status != "done":
            reject("Command failed")
        return {"status": status, "deploymentId": deployment["deploymentId"]}

    def schedule_delete(self, data: Row) -> bool:
        self.schedules = [
            row for row in self.schedules if row["scheduleId"] != data["scheduleId"]
        ]
        return True

    def registry_all(self, _: Row) -> list[Row]:
        return self.registries

    def registry_create(self, data: Row) -> Row:
        if data["registryType"] != "cloud":
            reject("registryType")
        # Dokploy stores no server on the entry: the serverId only picks the
        # host the login runs on.
        entry = {
            "registryId": self._id("registry"),
            **{key: value for key, value in data.items() if key != "serverId"},
        }
        self.registries.append(entry)
        self._login(data)
        return entry

    def registry_update(self, data: Row) -> bool:
        entry = next(
            row for row in self.registries if row["registryId"] == data["registryId"]
        )
        entry.update({key: value for key, value in data.items() if key != "serverId"})
        self._login({**entry, **data})
        return True

    def registry_remove(self, data: Row) -> Row:
        entry = next(
            row for row in self.registries if row["registryId"] == data["registryId"]
        )
        self.registries.remove(entry)
        return entry

    def _login(self, data: Row) -> None:
        server_id = data.get("serverId")
        if server_id is not None and not any(
            server["serverId"] == server_id for server in self.servers
        ):
            reject("no such server")
        if data["password"] in self.refused_passwords:
            reject("docker login was refused")
        self.logins.append((server_id, data["registryUrl"], data["username"]))
