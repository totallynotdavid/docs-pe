# Portal

Web interface for managing fetch jobs. Teams upload documents, configure proxy
credentials, track job progress, and download results, backed by PostgreSQL and
a worker fleet. Portal is for teams that want a web UI instead of running fetch
commands: user authentication and team management, persistent job history and
results, proxy credential management (safely stored, not copied to `.env`),
real-time job status via Server-Sent Events, and result download.

```sh
mise run dev
```

Two processes serve two audiences. `portal web` is the public listener behind
Cloudflare; `portal worker-api` is an internal listener that only the worker
fleet can reach over Tailscale. They share a package and a database, never a
listener. See [docs/portal-deployment.md](../../docs/portal-deployment.md) for
the Dokploy topology, edge configuration, master key handling, and worker
enrollment.

## Getting started (local)

Set these in `.env`:

```env
PORTAL_DATABASE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
PORTAL_ENVIRONMENT=development
PORTAL_PUBLIC_ORIGIN=http://localhost:8000
PORTAL_TLS_TERMINATED_UPSTREAM=false
PORTAL_MASTER_KEY_FILE=.data/master.key
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME="Equipo Lima"
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
```

Start the portal:

```sh
mise run dev
```

This writes a local master key if none exists, starts PostgreSQL in the
foreground, applies the schema, provisions the admin and first team, and runs
the app. Bootstrap never generates or prints a second factor: it creates the
admin account pending its own enrollment, and prints where to finish that.
Sign in with the bootstrap password and the app sends you to `/security/setup`
to add a TOTP app or a passkey yourself, because a site administrator cannot
exist without a second factor and only the account owner should ever see it.
Ctrl+C stops everything. To reset local state, run `mise run reset` then
`mise run dev` again.

Leaving the Turnstile keys empty skips the human check. That is a development
convenience and `PortalSettings.validate()` refuses it when
`PORTAL_ENVIRONMENT=production`.

Tests (`uv run pytest tests/portal`) each create, migrate, and drop their own
PostgreSQL database, and run in parallel since they share no database state.
They load a real key file from a temp directory rather than a stub, and they run
against an https origin so the Secure, `__Host-` prefixed cookie path is the one
under test.

## Commands

One entry point, because the workspace shares a virtualenv with `fetch` and
`portal-` repeated on seven scripts is the same word said seven times.

```
portal web            serve the browser-facing app
portal worker-api     serve the tailnet-only worker API
portal worker         claim and run work on a worker node
portal migrate        apply pending schema migrations
portal provision      create or verify the initial installation
portal bootstrap      provision from PORTAL_BOOTSTRAP_* (local dev)
portal enroll-worker  issue or revoke a worker credential
portal new-key        print a master key line for the key file
portal rewrap         move stored secrets onto the active master key
```

## Provisioning (production)

The portal has no public registration. Create the first admin and team:

```sh
uv run --env-file .env portal provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Optionally, create proxy credentials from environment variables:

```sh
uv run --env-file .env portal provision ... --proxy-provider geonode
```

This reads `PORTAL_PROVISION_GEONODE_USERNAME`,
`PORTAL_PROVISION_GEONODE_PASSWORD`, etc., and creates credentials for the team.

The command applies migrations and creates or updates the admin, team,
membership, and proxy credentials. It never prints, generates, or sees a second
factor: an admin created here is pending until they sign in and enroll TOTP or
a passkey themselves at `/security/setup`. Rerunning provisioning against an
installation that already has a second factor leaves it alone.

## Architecture

```
web              Litestar app for browsers: routes, auth, CSRF, SSE
  ├─ routes/
  ├─ deps.py    session extraction, same-origin, per-actor cap
  ├─ trace.py   client address from CF-Connecting-IP, and nothing else
  ├─ components/ JinjaX components, one .jinja plus its .css
  └─ pages/      JinjaX page and htmx-fragment templates

worker           Both sides of the fleet, and the wire between them
  ├─ protocol.py   claim and publish payloads, imported by both sides
  ├─ api.py        the Litestar app the fleet calls, tailnet only
  ├─ routes.py     claim and publish, per-worker bearer credential
  ├─ agent.py      the process that claims, runs fetch, publishes
  └─ enrollment.py issue and revoke worker credentials

application      Team access, job submission, cancellation, login
  ├─ service.py    : teams, credentials, jobs
  ├─ login.py      : the login pipeline, TOTP/passkey MFA, logout
  ├─ provisioning.py : team/user admin, self-service second-factor enrollment
  ├─ sessions.py   : cookie sessions and one-time tokens
  └─ throttle.py   : login lockout and per-actor mutation caps

domain           Planning and state rules
  ├─ models.py  : Job, Team, Credential, audit types
  └─ planning.py: plan_submission (routes documents to sites)

repository       PostgreSQL modules
  ├─ auth.py, teams.py, credentials.py, jobs.py
  ├─ workers.py : per-worker identities
  └─ audit.py   : append-only audit log

credentials      Envelope encryption
  ├─ masterkey.py : the versioned keyring loaded from a file
  └─ secrets.py   : seal and open payloads under a per-payload data key

ephemeral.py     Expiring keyed state: sessions, counters, one-time tokens
storage          Immutable object references (file uploads)
cli.py           One command, dispatched to the module that owns it
security.py      Password, session, TOTP, and token primitives
```

## Signing in

```
POST /login
  1. Turnstile token verified server-side, fail closed
  2. Login CSRF token consumed from the store (single use by construction)
  3. Two rate-limit counters read, one per account and one per source address
  4. Password verified with Argon2id, with a dummy verify on unknown accounts
     so a miss costs the same as a wrong password
  5. With a second factor: a pending token goes into a Strict, single-use
     cookie and the browser is sent to /login/mfa
  6. TOTP verified (RFC 6238, 30s step, 6 digits, one step of drift), a
     recovery code spent, or a passkey assertion verified instead
  7. Session minted in the store, keyed by the hash of the cookie value
  8. Set-Cookie: __Host-portal-id; Secure; HttpOnly; SameSite=Strict; Path=/
```

A passkey can also skip steps 1-5 entirely: `POST /login/passkey/options` with
no `pending_mfa` cookie issues a discoverable (usernameless) challenge, and a
successful assertion establishes a session directly. This is deliberate, not a
weaker path: a `userVerification: required` assertion already combines
possession of the device with the biometric or PIN check that unlocked it, so
it satisfies the second factor on its own (OWASP's Multifactor Authentication
Cheat Sheet reasons about passkeys the same way), and WebAuthn's
challenge/origin binding resists the credential-relay phishing that both a
password and a TOTP code remain vulnerable to.

Argon2id parameters are pinned in `security.py` (m=19 MiB, t=2, p=1) rather than
taken from `PasswordHash.recommended()`, which can move with a library release.
Passwords need 12 characters and nothing else: length over composition, per NIST
800-63B.

A wrong TOTP code spends the pending token and sends the browser back to the
password step. That is deliberate, not an oversight: it bounds code guesses at
one per password verification. Both the code and the password count against the
account's lockout, a fixed five-minute window that expires on its own; attempts
made while it holds are refused without extending it, so nobody who knows an
address can keep its owner locked out on a timer. A passkey assertion is not
throttled the same way: forging one without the private key is not a guessing
problem, so there is nothing a rate limit would bound.

Every authenticated request reloads the session from the store, refreshes its
idle TTL, and re-reads the account from Postgres, so removing an administrator
takes effect on their next request. A session also ends at an absolute cap of
seven days no matter how active it is; the sliding TTL cannot express that, so
the application enforces it.

State-changing requests pass a same-origin check, then the synchronizer CSRF
token held with the session, then a per-actor cap counted per route family.

`security.py` contains password, session, TOTP, WebAuthn, and token primitives
shared by the web and provisioning code. It is the place to review auth
assumptions, and the only module that imports the `webauthn` package directly.

## Second factors

Every signed-in user has a `/security` page to add or remove their own TOTP app
and passkeys; only site administrators are ever required to hold one
(`portal_admin_requires_second_factor`, a deferred constraint trigger since a
factor can now live in `portal_webauthn_credentials` as well as
`portal_users.mfa_enabled`, which a plain `CHECK` cannot see across tables).

Enrollment is always confirm-gated and always self-service: nothing generates a
second factor on anyone's behalf, and nothing shows a fresh secret to anyone
but the account owner, in their own browser.

- **TOTP**: `/security/totp/setup` renders a QR code (inline SVG, `segno`, no
  external image request) and asks for a live code before the secret is
  enabled. A wrong code does not burn the setup token, unlike a wrong code at
  login: the QR is not a guessable target, so a typo should mean "try again,"
  not "scan a new code."
- **Passkey**: `/security` posts to `/security/passkey/options` for a
  registration challenge, calls `navigator.credentials.create()` client-side
  (hand-rolled base64url glue in `static/portal.js`, no added JS dependency),
  and confirms at `/security/passkey/register`. Multiple passkeys per account
  are supported; `portal_webauthn_credentials.sign_count` is bumped with an
  optimistic-concurrency `WHERE`, which is what turns a replayed or cloned
  assertion into a rejected write.

Recovery codes are issued once, the first time an account gains any second
factor (not on every factor added afterward), and can be spent in place of a
TOTP code at `/login/mfa`. Removing a factor is blocked if it is a site
admin's last one (`Reason.LAST_SECOND_FACTOR`), enforced at the application
layer and, for the concurrent-request case, by the same constraint trigger
that enforces the invariant at promotion.

Promoting someone from `/admin/users` never touches their second factor:
`ProvisioningService.promote_to_site_admin` promotes immediately if the target
already self-enrolled something, or marks `pending_site_admin` and waits.
Either way the promoting admin never sees the target's TOTP secret, QR code, or
passkey ceremony. A pending user is sent to `/security` (no skip option) the
moment they next sign in with just their password, since there is no factor
yet to challenge.

## Ephemeral state

Sessions, rate-limit counters, and one-time tokens are rows in
`portal_ephemeral`, not keys in Redis. Every operation is a single statement, so
an expired row is indistinguishable from a missing one in the same statement
that replaces it, and two requests racing on a key cannot both win. A background
task deletes expired rows once a minute; reads already ignore them, so the sweep
only reclaims space.

Redis is the better tool in the abstract and the wrong one here. The heaviest
path is the job progress stream, which already polls Postgres and reloads the
session on every poll, so twenty team leads watching jobs is on the order of ten
primary-key lookups a second. Against that, Redis is a second stateful service
to run, back up, and upgrade, and a new hard failure mode where nobody can sign
in. In-process state would be worse than either: rolling deploys run two
containers at once, so every deploy would sign everyone out.

## Stored secrets

Proxy credentials and TOTP secrets are enveloped: a fresh AES-256-GCM data key
per payload, wrapped by a master key from `PORTAL_MASTER_KEY_FILE`, with only
the wrapped key stored beside the ciphertext. The keyring is versioned, so
rotation prepends a key and `portal rewrap` moves stored data keys onto it
without reading or rewriting a single payload. Handling and rotation procedure
are in [docs/portal-deployment.md](../../docs/portal-deployment.md#the-master-key).

A passkey's stored public key is not enveloped: it is public by definition,
there for verifying a signature, not for producing one. Only the credential id
and public key ever reach the database; the private key never leaves the
authenticator.

## How jobs run

The unit of work is one `(doc, site)` pair (`portal_job_items`), not one job:
this is what lets a single job's documents fan out across the whole fleet
automatically, with no per-job worker assignment anywhere.

1. Submit: the user uploads a CSV, selects sites and proxy credentials. The
   portal stores the file and plans the job (routes documents to sites), then
   checks which `(document, source)` pairs this team already has a fresh
   answer for (`PostgresEntryRepository.reusable_for_team`, windowed per
   source by `domain/planning.SOURCE_FRESHNESS`). If any are reusable, the
   leader sees a review step (`JobReview.jinja`) before anything is created,
   choosing to reuse them or force a full rescan. Reused items are inserted
   already published, pointing at the existing entry, and never reach a
   worker; everything else queues pending as before.
2. Claim: every worker node runs a fixed number of concurrent lanes
   (`--concurrency`, `WorkerAgent._loop`), each polling `/claim` independently.
   `/claim` leases one pending item, from any running job, to whichever lane
   asks first (`FOR UPDATE SKIP LOCKED`, ordered by `queue_sequence, ordinal`),
   filtered to the lane's `--sources` and to `(site, provider)` pairs whose
   circuit breaker isn't currently open. Its bearer credential is checked
   against `portal_workers`, and the claim is the only place a stored proxy
   credential is decrypted.
3. Run: the lane looks the document up in-process via `fetch.pipeline.fetch_one`
   (not a subprocess). A lane keeps its provider session open across
   consecutive claims that land on the same `(source, credential_version_id)` —
   passed back to `/claim` as an affinity hint so the queue prefers handing the
   lane more of the same pair's work — so `fetch.pipeline.session`'s
   `session_budget` amortizes the way it does in the standalone `fetch` CLI,
   instead of every claimed document paying full session-open cost. The lane
   releases the session on a source/credential change, after a run of idle
   polls, or on lane shutdown.
4. Publish: `/publish` upserts `portal_entries` (the queryable content: status,
   columns, rows, keyed by `(document, source)` and deduplicated across every
   team that has ever confirmed it), archives the raw result to object storage
   for audit/replay, marks the item published pointing at that entry, and (via
   `portal_circuit_breakers`) records the outcome against that
   `(site, provider)` pair's fleet-wide circuit breaker — the shared backstop
   ten consecutive failures anywhere trips, parking every node's lanes for that
   pair, not just the node that hit the threshold.
5. Recover: if a lane crashes or its item's lease expires, another lane claims
   it (an incremented lease fence prevents lost writes).

Every queue transition locks the singleton `portal_queue_control` row, which
enforces a global limit of five active jobs across all web and worker processes
— jobs beyond that wait `queued` even if the fleet has idle lane capacity, since
only a running job's items are claimable. Cancellation increments the job's
lease fence before retiring active items, so writes from older leases are
rejected and you can cancel a job without racing against a slow worker. Expired
item leases are recovered while claiming work; items that repeatedly expire, or
jobs that finish without every item published, are marked failed. See
[operations.md](operations.md) for the SQL to inspect or manually cancel a job.

The worker agent holds no database credentials. That is the reason the claim and
publish API exists at all rather than letting workers take work from Postgres
directly: a compromised browser automation node gets the item it is holding and
the proxy credential for that item's job, and nothing else.

## Search and entries

`portal_entries` is the deduplicated, cross-team store of what the portal
currently knows: one row per `(document, source)`, holding `columns`/`rows` in
the same generic shape the worker agent already sends over the wire
(`{document, source, status, columns, rows, error_code}`, mirroring
`fetch.domain.types.Site.columns` and `Result.rows`). Storing that shape
verbatim, instead of a typed per-source schema, keeps this table ignorant of
what a source's fields are named or mean — the same boundary
`portal.worker.routes` already draws for the raw result blob — so a new fetch
site never requires a portal migration.

`portal_job_items` is the separate, append-only per-team ledger: who confirmed
what, when, in which job. The two combine into the two searches the UI offers:

- Team search (`PostgresEntryRepository.search_team`) joins through this
  team's own `portal_job_items` rows, so a team only ever sees entries it
  itself has confirmed — even though the underlying row may be physically
  shared with other teams that scanned the same document. Storage dedup and
  reuse rights are deliberately separate: a document only counts as "already
  known" for the submission review step (above) if *this* team's own ledger
  confirms it, never another team's.
- Global search (`PostgresEntryRepository.search_global`) reads `portal_entries`
  directly, no team filter. Gated by `@site_admin_or_global_search` (site
  admins, or any team with the paid `portal_teams.has_global_search`
  entitlement, toggled from `/admin/teams`) — a customer entitlement, not a
  platform role, so it is checked separately from `is_site_admin`.

Both render through the same `EntryDetail`/`AdminEntryDetail` pages: a generic
table over `entry.columns`/`entry.rows`, so a document with several rows (an
OSIPTEL DNI with multiple lines, a RUC-20 with several legal reps) shows them
together rather than one at a time.

## Audit log

`portal_audit_log` is insert-only: a trigger refuses UPDATE and DELETE, and the
schema revokes both from `portal_app` where that role exists. It records login
success and failure, session destruction, MFA enrollment, every refusal, every
administrative change, every credential configure and reveal, and every worker
credential issued or revoked. Each row carries the client address and Cloudflare
ray id where the request had one.

## Structure

| Module          | Purpose                                              |
| --------------- | ---------------------------------------------------- |
| `web/`          | Litestar routes, session handling, templates         |
| `worker/`       | Worker API, worker agent, and the protocol they share |
| `application/`  | Service layer (teams, credentials, jobs, login)      |
| `domain/`       | Types (Job, Team, Credential) and business rules     |
| `repository/`   | PostgreSQL access                                    |
| `credentials/`  | Master keyring and envelope encryption               |
| `ephemeral.py`  | Expiring keyed state                                 |
| `storage/`      | File upload abstraction                              |
| `cli.py`        | Subcommand dispatch                                  |
| `security.py`   | Password hashing, session, TOTP, WebAuthn, and token primitives |

## Web UI

The design tokens in `web/static/tokens.css` are a subset of the shared brand
sheet (`crm/apps/web/src/app.css`), copied name for name and value for value.
A token that exists there keeps its name and its number here; the portal never
redefines one to a different value, and adds its own only for something the
shared sheet has no name for. The reset and base layers live in that file
inside `@layer reset, base`, so unlayered component CSS always outranks them
without needing `!important`.

Every component is `Name.jinja` plus `Name.css`, and its stylesheet may only
style what that template renders. `List.css` used to own the badge, the meta
text, the empty state and the pagination as well; each of those is now its own
component. `assets.py` concatenates `components/*.css` into one content-hashed
bundle, so the split costs nothing at request time. Three components are a
deliberate exception, each owning a tightly-coupled family rather than one
template each: `Nav.css` covers `NavSection` and `NavItem`, `Metrics.css`
covers `Metric`, and `DataTable.css` covers `TableHeader`/`TableBody`/
`TableRow`/`TableHead`/`TableCell`. A new member of one of these families joins
the family sheet; a genuinely new table-like or nav-like component still gets
its own `Name.jinja` + `Name.css`.

The shell mirrors the shared app frame: a fixed `100dvh` grid that never
scrolls, a transparent drawer sitting on `--background-app`, and a `page-card`
rounded into the drawer's corner only. Only `MainContent` scrolls, so the
drawer and page header stay put. Inside the card, a page places up to three
siblings in order: `TopBar` (always — the page's one real `<h1>`, an optional
breadcrumb, and the page's one primary action, right-aligned), an optional
`SecondaryBar` for filters/search/view controls, and `MainContent`. A
multi-step flow's own submit button is the one exception to "primary action
lives in the header": it stays at the end of the flow, not the header, because
there is no single page-level action until the last step. `PageIntro` is a
second, unrelated header idiom for the no-sidebar onboarding archetype
(`FirstTeam`, `Security`'s pre-setup branch) — it has no drawer to sit beside,
so it isn't a `TopBar` replacement to reconcile, just a different shell for a
different kind of page.

Names collide with HTML. djlint lowercases any component tag that matches a
real element, which silently turned `<Table>` into a bare `<table>` and
`<MenuItem>` into `<menuitem>`. Hence `DataTable` and `Dropdown`. Check a new
component's name against the HTML element list before adding it.

`Icon.jinja` holds the whole icon set, lifted from the shared set at a 24px
grid and `stroke-width: 2`. Add a name to it rather than inlining an `svg` in
a page.

Use the scale. Raw pixels are for what the scale has no name for: a dropzone's
minimum height, a status dot, a hairline.

## Configuration

| Variable                       | Meaning                                                          |
| ------------------------------ | ---------------------------------------------------------------- |
| `PORTAL_DATABASE_DSN`          | PostgreSQL connection string                                     |
| `PORTAL_ENVIRONMENT`           | `development` or `production`; gates development conveniences only |
| `PORTAL_PUBLIC_ORIGIN`         | Scheme and host; decides HTTPS, Secure cookies, and host checking |
| `PORTAL_TLS_TERMINATED_UPSTREAM` | True when a proxy terminates TLS, so the app does not redirect  |
| `PORTAL_MASTER_KEY_FILE`       | Versioned keyring that wraps every stored data key               |
| `PORTAL_TURNSTILE_SITE_KEY`    | Widget key for `/login`; required in production                  |
| `PORTAL_TURNSTILE_SECRET`      | Server-side siteverify key; required in production               |
| `PORTAL_WORKER_API_HOST`       | Address `portal worker-api` binds to                             |
| `PORTAL_WORKER_API_PORT`       | Port `portal worker-api` listens on                              |
| `PORTAL_BOOTSTRAP_*`           | Admin and team created on first run (local dev only)             |

On a worker node, set `PORTAL_WORKER_API_URL`, `PORTAL_WORKER_CREDENTIAL`, and
`PORTAL_WORKER_ID`. See `portal/application/provisioning.py` for provisioning
variables (e.g. `PORTAL_PROVISION_GEONODE_USERNAME`).
