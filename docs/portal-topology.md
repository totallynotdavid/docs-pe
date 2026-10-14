# Portal topology

This page describes the Compose layout and network boundary of one portal
deployment. It is a reference for the person configuring Dokploy, host
provisioning, and the tailnet policy. Follow
[Portal deployment](portal-deployment.md) for the release procedure.

## Traffic boundaries

```text
browser -> Cloudflare (TLS ends) -> Traefik on the Dokploy host -> web
web, worker-api, init -> postgres, garage     (project network)
worker node -> worker-api                     (core host, published 8443)
worker node -> postgres                       (core host, published 5432, scoped role)
```

The worker API has no public route. Workers reach it for enrollment, credential
reveals, and result publication. Queue, heartbeat, and proxy-slot operations use
the worker's scoped PostgreSQL role.

## The core Compose file

`docker-compose.core.yml` is the whole core of a deployment:

| Service      | Image                  | Purpose                               |                          Host port |
| ------------ | ---------------------- | ------------------------------------- | ---------------------------------: |
| `postgres`   | `postgres:18-alpine`   | Portal database                       |   `POSTGRES_PUBLISHED_PORT` (5432) |
| `garage`     | `dxflrs/garage:v2.4.1` | S3 object store, single node          |                               none |
| `init`       | `${PORTAL_IMAGE}`      | One shot: master key, then migrations |                               none |
| `web`        | `${PORTAL_IMAGE}`      | Browser application on port 8000      |                               none |
| `worker-api` | `${PORTAL_IMAGE}`      | Worker API on port 8443               | `WORKER_API_PUBLISHED_PORT` (8443) |

The file deploys as raw text (Dokploy's "raw" Compose source). It binds no
repository file and builds nothing: every portal service runs the prebuilt
`PORTAL_IMAGE`, and Garage's configuration is inline in a top-level `configs:`
entry.

Garage runs `garage server --single-node --default-bucket`. It configures its
own single-node layout and creates the access key and bucket named by
`GARAGE_DEFAULT_ACCESS_KEY`, `GARAGE_DEFAULT_SECRET_KEY`, and
`GARAGE_DEFAULT_BUCKET`. Its S3 region is `garage`. Its health check passes only
once the bucket exists.

The file derives the internal addresses itself. Portal processes connect to
`postgres:5432` with the `POSTGRES_*` credentials and to `http://garage:3900`
with the Garage key. The operator does not set `PORTAL_DATABASE_DSN` or any
`PORTAL_OBJECT_STORAGE_*` variable for the core.

### Startup order

`init` starts once PostgreSQL is healthy. It installs master key `v1` if the
`master-key` volume has none, prepends the key named by
`PORTAL_MASTER_KEY_VERSION` when that is set (see
[key rotation](portal-deployment.md#key-rotation)), then runs
`portal-admin migrate`. `web` and `worker-api` start only after `init` exits
successfully and Garage is healthy.

A later `up` runs `init` again. It finds the key, finds no pending migration,
and changes nothing. The long-running services keep their containers.

### Volumes

All volumes are ordinary named volumes owned by the Compose project, so their
Docker names carry the project name as a prefix:

| Volume          | Contents                                   |
| --------------- | ------------------------------------------ |
| `postgres-data` | PostgreSQL cluster                         |
| `garage-data`   | Garage metadata and object blocks          |
| `master-key`    | `master.key`, mounted at `/app/.data/keys` |

`init` mounts `master-key` read-write. `web` and `worker-api` mount it
read-only. No other process holds the key.

## Isolation between deployments

Each deployment is its own Compose project with its own default network and its
own volumes. No service names an external network. Dokploy attaches
`dokploy-network` itself, and only to a service that has a domain, which is
`web`. PostgreSQL, Garage, and `worker-api` never join a network another project
shares, so no other project can resolve `postgres` to this deployment. When
Dokploy adds `dokploy-network` to `web`, it also adds `default`, so `web` keeps
reaching PostgreSQL and Garage on the project network.

Two deployments on one host need different `POSTGRES_PUBLISHED_PORT` and
`WORKER_API_PUBLISHED_PORT` values.

### Published ports

PostgreSQL and `worker-api` publish their ports on the host. Host provisioning
makes those ports answer only on loopback and to tailnet peers. Docker forwards
published ports through its own chains, so the restriction belongs in
`DOCKER-USER` and must match the ingress interface. A container of another
project on the same host reaches the host's address from a Docker bridge, and
without that rule it would reach the published port.

The tailnet policy grants each deployment's worker nodes TCP 5432 and 8443 on
that deployment's core host, and nothing on another deployment's core host.

## Worker-facing addresses

`worker-api` reaches PostgreSQL as `postgres:5432`, which a worker node cannot
resolve. The DSN it hands a worker at enrollment, and the one
`portal-admin worker issue` prints, takes its host and port from
`PORTAL_WORKER_DATABASE_ADDRESS` (`host` or `host:port`): the core host's
tailnet name and `POSTGRES_PUBLISHED_PORT`. The database name and options come
from the core's own DSN. Production validation refuses to start without the
setting.

Workers set `PORTAL_WORKER_API_URL` to
`http://<core host>:<published worker-api port>`, which the deploy tool derives
from `PORTAL_WORKER_DATABASE_ADDRESS` and `WORKER_API_PUBLISHED_PORT`.

## Connection budget

Budget PostgreSQL connections for every web, worker-api, worker, admin, and
operator process. Each worker also keeps a dedicated connection for
notifications. Recheck the budget when changing worker concurrency or the number
of service processes.
