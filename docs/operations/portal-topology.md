# Portal topology

This page describes the production network and Compose layout. It is a
reference for the person configuring Tailscale and Dokploy. Follow
[Portal deployment](portal-deployment.md) for the release procedure.

## Traffic boundaries

```text
browser -> Cloudflare -> cloudflared -> web
web -> PostgreSQL, object store, master key
worker-api -> PostgreSQL, object store, master key
worker node -> worker-api (Tailscale)
worker node -> PostgreSQL (scoped role)
```

The worker API has no public route. Workers reach it over the tailnet for
enrollment, credential reveals, and result publication. Queue, heartbeat, and
proxy-slot operations use the worker's scoped PostgreSQL role.

PostgreSQL, object storage, and `worker-api` each run behind a Tailscale
sidecar. Each sidecar advertises a `svc:` Service instead of binding a host port:

| Service | Port | Consumer |
| --- | ---: | --- |
| `svc:database` | 5432 | Worker nodes and portal processes |
| `svc:objectstorage` | 9000 | Worker-api and portal processes |
| `svc:worker-api` | 8443 | Worker nodes |

The ACL policy grants these services to `tag:worker-fleet` only. Put
`tag:core` on the shared-service sidecars, not on their host, and put
`tag:worker-fleet` on every worker node.

## Tailscale and Dokploy prerequisites

Define all three Services in the Tailscale admin console with their port and
`do-not-validate` in the endpoint field. These endpoints carry PostgreSQL wire,
S3, or the worker API's own HTTP protocol, so the default endpoint probe cannot
validate them. An ACL grant and a running sidecar do not create the Service
resource required by auto-approvers or service advertisement.

Give each shared-service sidecar a reusable auth key with ephemeral nodes
enabled: `TS_AUTHKEY_DATABASE`, `TS_AUTHKEY_OBJECTSTORAGE`, and
`TS_AUTHKEY_WORKER_API`.

All Compose projects use the external `dokploy-network`. The worker API Compose
file uses the container path in `PORTAL_MASTER_KEY_FILE`; the web container must
use the path at which its own read-only master-key mount appears.

## Web DNS

`web` runs as a Dokploy application, which is a Docker Swarm service. Its task
DNS does not include Tailscale's MagicDNS resolver. After every `web` deploy,
apply the resolver manually:

```sh
docker service update --dns-add 100.100.100.100 <web's Swarm service name>
```

Dokploy recreates the Swarm service from its stored specification on every
deploy, so the override is lost and `*.ts.net` lookups fail until it is applied
again. Find the service name with `docker service ls`, matching Dokploy's
`appName` for the `web` application.

`web` stays on Swarm because it needs a domain and Traefik routing. Worker nodes
use plain Compose and do not need this DNS override. The fleet uses the Dokploy
Compose resource defined by `docker-compose.worker.yml`, not `docker service`.

## Volumes and sidecar advertisement

For an existing installation, set `POSTGRES_VOLUME_NAME` and
`MINIO_VOLUME_NAME` to the current Docker volume names. Confirm them with
`docker volume ls` before starting Compose. A new name creates an empty store.

Each shared-service sidecar runs `tailscale serve --service=...` directly and
reasserts the endpoint every 10 seconds. `TS_SERVE_CONFIG` does not reliably
apply the `services:` endpoint mapping through containerboot, and a configured
endpoint does not survive a sidecar restart. If containerboot exits, the wrapper
also exits so `restart: unless-stopped` can recreate the sidecar.

Budget PostgreSQL connections for every web, worker-api, worker, admin, and
operator process. Each worker also keeps a dedicated connection for
notifications. Recheck the budget when changing worker concurrency or the
number of service processes.
