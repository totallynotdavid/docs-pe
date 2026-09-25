# Worker fleet

A worker node is a host that runs `docker-compose.worker.yml`. It needs Docker
and tailnet access to its deployment's core host on the published worker API
and PostgreSQL ports (see [Portal topology](portal-topology.md)).

## Add a node

On the node, run the worker Compose file with the environment shown in
[Worker enrollment](portal-deployment.md#worker-enrollment):

```sh
docker compose -p portal-worker --env-file worker.env -f docker-compose.worker.yml up -d
```

`PORTAL_WORKER_API_URL` is `http://<core host>:<WORKER_API_PUBLISHED_PORT>`
(8443 by default). The worker enrolls when it starts. Confirm that its
heartbeat advances in `portal_workers.last_seen_at`.

## Remove a node

Revoke first, so the node's API credential and PostgreSQL login stop working
even if the container keeps running:

```sh
docker exec <appName>-worker-api-1 portal-admin worker revoke --worker-id <worker-id>
```

Then stop the worker on the node:

```sh
docker compose -p portal-worker --env-file worker.env -f docker-compose.worker.yml down
```
