"""The HTTP and database steps of scripts/e2e-portal.sh, the end-to-end portal test.

Secrets arrive in the environment, never as arguments. Each command prints
only the value the hook keeps, and logs to stderr.

    dokploy-session                DOKPLOY_URL, E2E_DOKPLOY_EMAIL, E2E_DOKPLOY_PASSWORD
    enrol-totp ORIGIN CONNECT      E2E_ADMIN_EMAIL, E2E_ADMIN_PASSWORD
    submit ORIGIN CONNECT SOURCE DOCUMENT
                                   the same, and E2E_ADMIN_TOTP_SECRET
    check-job JOB WORKER           inside the core's web container
    check-revoked WORKER           inside the core's web container

CONNECT is the HOST:PORT that reaches the portal at ORIGIN, which stays the
URL, Host, SNI name and Origin of every request. The last two commands read
the portal's own settings, so the hook streams this file into
`python -` in the web container.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time

from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import aioboto3
import asyncpg
import httpx
import pyotp


if TYPE_CHECKING:
    from collections.abc import Sequence


# Cloudflare's test secret accepts this token, which its always-pass test site
# key hands a browser.
TURNSTILE_TEST_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

# djlint wraps and reorders attributes, so name and value are found separately
# within each tag.
INPUT_TAG = re.compile(r"<input\b[^>]*>")
INPUT_NAME = re.compile(r'\bname="([^"]+)"')
INPUT_VALUE = re.compile(r'\bvalue="([^"]*)"')
TEAM_PATH = re.compile(r"/teams/([0-9a-f-]{36})")
JOB_PATH = re.compile(r"/teams/[0-9a-f-]{36}/jobs/([0-9a-f-]{36})$")
OTPAUTH_URI = re.compile(r"otpauth://totp/[^\s\"<]+")

JOB_ITEMS = """
SELECT i.document, i.source, i.state, e.status,
       o.container, o.object_key, o.sha256,
       array(SELECT DISTINCT a.worker_id FROM portal_lookup_attempts a
             WHERE a.job_item_id = i.id) AS workers
  FROM portal_job_items i
  LEFT JOIN portal_entries e ON e.id = i.entry_id
  LEFT JOIN portal_object_references o ON o.id = i.result_object_id
 WHERE i.job_id = $1 AND i.source IS NOT NULL
"""


class E2EError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def env(name: str) -> str:
    value = os.environ.get(name, "")

    if not value:
        msg = f"{name} is not set"
        raise E2EError(msg)

    return value


def expect(response: httpx.Response, step: str, status: int = 200) -> httpx.Response:
    if response.status_code != status:
        msg = f"{step}: HTTP {response.status_code}: {response.text[:500]}"
        raise E2EError(msg)

    return response


def dokploy_session(client: httpx.Client, email: str, password: str) -> str:
    """Sign up Dokploy's first user, the owner of its organization, and return
    the Cookie header value of the session that sign-up opens.

    ops/deploy.py authenticates with that session like the dashboard does. An
    API key would need Dokploy's internal user.createApiKey route, because a key
    from the public better-auth route is rate limited to 10 requests a day."""
    expect(
        client.post(
            "/api/auth/sign-up/email",
            json={
                "email": email,
                "password": password,
                "name": "e2e",
                "lastName": "e2e",
            },
        ),
        "sign up",
    )

    # The call ops/deploy.py makes first, so a session Dokploy does not accept
    # fails here.
    expect(client.get("/api/project.all"), "Dokploy session")

    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)


class ConnectTo(httpx.BaseTransport):
    """Connects to a fixed address, while URL, Host and SNI keep the origin's
    name, as they would for a browser that resolved it there."""

    def __init__(self, connect: str) -> None:
        host, _, port = connect.rpartition(":")
        self._host = host
        self._port = int(port)
        self._transport = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # A copy, because the client keys cookies and redirects on the
        # request it sent.
        connected = httpx.Request(
            request.method,
            request.url.copy_with(host=self._host, port=self._port),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return self._transport.handle_request(connected)

    def close(self) -> None:
        self._transport.close()


def portal_client(origin: str, connect: str) -> httpx.Client:
    return httpx.Client(base_url=origin, transport=ConnectTo(connect), timeout=60)


def input_values(page: str) -> dict[str, str]:
    """A repeated input name keeps its last value."""
    inputs = {}

    for tag in INPUT_TAG.findall(page):
        name = INPUT_NAME.search(tag)
        value = INPUT_VALUE.search(tag)
        if name and value:
            inputs[name[1]] = unescape(value[1])

    return inputs


def form_value(html: str, name: str, step: str) -> str:
    value = input_values(html).get(name)

    if not value:
        msg = f"{step}: the page has no {name} field"
        raise E2EError(msg)

    return value


def post(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response:
    origin = f"{client.base_url.scheme}://{client.base_url.netloc.decode()}"
    return client.post(
        path, headers={"Origin": origin}, follow_redirects=False, **kwargs
    )


def wait_for_portal(client: httpx.Client, timeout_seconds: float) -> None:
    """The first request also waits for the TLS certificate to be issued."""
    deadline = time.monotonic() + timeout_seconds
    last = ""

    while time.monotonic() < deadline:
        try:
            response = client.get("/login")
        except httpx.HTTPError as error:
            last = repr(error)
        else:
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code}"
        time.sleep(5)

    msg = f"the portal did not serve /login within {timeout_seconds:.0f} s: {last}"
    raise E2EError(msg)


def sign_in(
    client: httpx.Client,
    email: str,
    password: str,
    totp: pyotp.TOTP | None = None,
) -> None:
    page = expect(client.get("/login"), "login page")
    response = post(
        client,
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": form_value(page.text, "csrf_token", "login page"),
            "turnstile_token": TURNSTILE_TEST_TOKEN,
        },
    )

    if response.headers.get("location") == "/login/mfa":
        if totp is None:
            msg = "login asked for a second factor this account should not have yet"
            raise E2EError(msg)
        response = post(client, "/login/mfa", data={"code": totp.now()})

    location = response.headers.get("location", "")
    if response.status_code != 303 or location.startswith("/login"):
        msg = f"login refused: HTTP {response.status_code} to {location or '-'}"
        raise E2EError(msg)


def enrol_totp(client: httpx.Client) -> str:
    """Enrol TOTP for the signed-in account and return its secret."""
    page = expect(client.get("/security/totp/setup"), "TOTP setup")
    uri = OTPAUTH_URI.search(page.text)

    if uri is None:
        msg = "TOTP setup: the page shows no otpauth URI"
        raise E2EError(msg)

    secret = parse_qs(urlsplit(unescape(uri[0])).query)["secret"][0]
    response = post(
        client,
        "/security/totp/confirm",
        data={
            "setup_token": form_value(page.text, "setup_token", "TOTP setup"),
            "code": pyotp.TOTP(secret).now(),
            "csrf_token": form_value(page.text, "csrf_token", "TOTP setup"),
        },
    )

    security = expect(client.get("/security"), "security page")
    if (
        response.status_code not in {200, 303}
        or "/security/totp/disable" not in security.text
    ):
        msg = f"TOTP confirmation refused: HTTP {response.status_code}"
        raise E2EError(msg)

    return secret


def submit_job(client: httpx.Client, source: str, document: str) -> str:
    """Submit one document to one source for the account's first team and
    return the job id. A reusable earlier result is looked up again, so the
    job always reaches a worker."""
    home = expect(client.get("/", follow_redirects=True), "home")
    team = TEAM_PATH.search(f"{home.url.path} {home.text}")

    if team is None:
        msg = "home: no team for this account"
        raise E2EError(msg)

    jobs = f"/teams/{team[1]}/jobs"
    form = expect(client.get(f"{jobs}/new"), "job form")
    # One credential is a hidden input, several are a select.
    credential = input_values(form.text).get("credential_version_id")
    if credential is None and (
        option := re.search(r'<option value="([^"]+)"', form.text)
    ):
        credential = option[1]

    if credential is None:
        msg = "job form: the team has no active proxy credential"
        raise E2EError(msg)

    response = post(
        client,
        jobs,
        data={
            "credential_version_id": credential,
            "csrf_token": form_value(form.text, "csrf_token", "job form"),
            "sources": source,
            "filename": "e2e.csv",
        },
        files={"input_file": ("e2e.csv", f"{document}\n".encode(), "text/csv")},
    )

    if response.status_code == 200:
        review = response.text
        response = post(
            client,
            f"{jobs}/confirm",
            data={
                "input_object_id": form_value(review, "input_object_id", "job review"),
                "credential_version_id": credential,
                "filename": "e2e.csv",
                "sources": source,
                "reuse": "rescan",
                "csrf_token": form_value(review, "csrf_token", "job review"),
            },
        )

    job = JOB_PATH.search(response.headers.get("location", ""))
    if response.status_code != 303 or job is None:
        msg = f"job submission: HTTP {response.status_code}"
        raise E2EError(msg)

    return job[1]


async def check_job(job_id: UUID, worker: str, timeout_seconds: float) -> None:
    """Wait for the job to finish, then require that the worker published every
    item and that each result object in the store matches its recorded hash."""
    connection = await asyncpg.connect(env("PORTAL_DATABASE_DSN"))

    try:
        await wait_for_job(connection, job_id, timeout_seconds)
        items = await connection.fetch(JOB_ITEMS, job_id)
    finally:
        await connection.close()

    if not items:
        msg = "the job has no items"
        raise E2EError(msg)

    async with aioboto3.Session().client(
        "s3",
        endpoint_url=env("PORTAL_OBJECT_STORAGE_ENDPOINT"),
        aws_access_key_id=env("PORTAL_OBJECT_STORAGE_ACCESS_KEY"),
        aws_secret_access_key=env("PORTAL_OBJECT_STORAGE_SECRET_KEY"),
        region_name=env("PORTAL_OBJECT_STORAGE_REGION"),
    ) as s3:
        for item in items:
            label = f"{item['source']} {item['document']}"
            if problem := item_problem(item, worker):
                msg = f"{label}: {problem}"
                raise E2EError(msg)

            stored = await s3.get_object(
                Bucket=env("PORTAL_OBJECT_STORAGE_BUCKET"),
                Key=f"{item['container']}/{item['object_key']}",
            )
            body = await stored["Body"].read()
            if hashlib.sha256(body).hexdigest() != item["sha256"]:
                msg = f"{label}: the stored result does not match its sha256"
                raise E2EError(msg)

            log(f"{label}: {item['status']} by {worker}, {len(body)} bytes stored")


async def wait_for_job(
    connection: asyncpg.Connection, job_id: UUID, timeout_seconds: float
) -> None:
    deadline = time.monotonic() + timeout_seconds
    state = ""

    while True:
        job = await connection.fetchrow(
            "SELECT state, terminal_reason FROM portal_jobs WHERE id = $1", job_id
        )
        if job is None:
            msg = f"job {job_id} does not exist"
            raise E2EError(msg)
        if job["state"] != state:
            state = job["state"]
            log(f"job {state}")
        if state == "completed":
            return
        if state in TERMINAL_JOB_STATES:
            msg = f"job ended {state}: {job['terminal_reason']}"
            raise E2EError(msg)
        if time.monotonic() > deadline:
            msg = f"job still {state} after {timeout_seconds:.0f} s"
            raise E2EError(msg)
        await asyncio.sleep(5)


def item_problem(item: Any, worker: str) -> str | None:
    if item["state"] != "published" or item["object_key"] is None:
        return f"{item['state']}, with no published result"
    if item["status"] not in {"ok", "not_found"}:
        return f"the lookup {item['status']}"
    if list(item["workers"]) != [worker]:
        return f"looked up by {list(item['workers'])}, not {worker}"
    return None


async def check_revoked(worker: str) -> None:
    connection = await asyncpg.connect(env("PORTAL_DATABASE_DSN"))

    try:
        revoked = await connection.fetchval(
            "SELECT revoked_at IS NOT NULL FROM portal_workers WHERE worker_id = $1",
            worker,
        )
    finally:
        await connection.close()

    if not revoked:
        msg = f"worker {worker} is not revoked"
        raise E2EError(msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("dokploy-session")

    for name in ("enrol-totp", "submit"):
        command = commands.add_parser(name)
        command.add_argument("origin")
        command.add_argument("connect")
        if name == "submit":
            command.add_argument("source")
            command.add_argument("document")

    job = commands.add_parser("check-job")
    job.add_argument("job", type=UUID)
    job.add_argument("worker")
    job.add_argument("--timeout", type=float, default=1200)

    revoked = commands.add_parser("check-revoked")
    revoked.add_argument("worker")

    return parser


def run(args: argparse.Namespace) -> str | None:
    if args.command == "dokploy-session":
        url = env("DOKPLOY_URL").rstrip("/")
        with httpx.Client(base_url=url, headers={"Origin": url}, timeout=60) as client:
            return dokploy_session(
                client, env("E2E_DOKPLOY_EMAIL"), env("E2E_DOKPLOY_PASSWORD")
            )

    if args.command == "check-job":
        asyncio.run(check_job(args.job, args.worker, args.timeout))
        return None

    if args.command == "check-revoked":
        asyncio.run(check_revoked(args.worker))
        return None

    with portal_client(args.origin, args.connect) as client:
        wait_for_portal(client, timeout_seconds=300)

        if args.command == "enrol-totp":
            sign_in(client, env("E2E_ADMIN_EMAIL"), env("E2E_ADMIN_PASSWORD"))
            return enrol_totp(client)

        totp = pyotp.TOTP(env("E2E_ADMIN_TOTP_SECRET"))
        sign_in(client, env("E2E_ADMIN_EMAIL"), env("E2E_ADMIN_PASSWORD"), totp)
        return submit_job(client, args.source, args.document)


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        output = run(args)
    except (E2EError, httpx.HTTPError) as error:
        sys.exit(f"error: {error}")

    if output is not None:
        print(output)


if __name__ == "__main__":
    main()
