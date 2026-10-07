# Sharded fetch jobs

Use a manifest when one job runs on more than one host. It records each shard's
input hash, selected sites, providers, host, output path, state path, and
revision. Merge with `fetch-fleet`. Give every process its own output path.

Create the manifest before starting the shards:

```sh
uv run fetch-fleet create \
  --manifest results/job.json \
  --revision "$(git rev-parse HEAD)" \
  --sites sunat \
  --providers dataimpulse \
  --shard north worker-north inputs/north.csv results/north.csv \
  --shard south worker-south inputs/south.csv results/south.csv
```

Run each shard on its host with its own input and output path. The manifest
records the shard's state database, the output path with `.state.sqlite3` in
place of its extension (`results/north.state.sqlite3` here). Copy that file to
the host that holds the manifest if that host cannot read it directly.

```sh
uv run --env-file .env fetch \
  --input inputs/north.csv \
  --output results/north.csv \
  --sites sunat
```

Reconcile before merging:

```sh
uv run fetch-fleet status --manifest results/job.json
uv run fetch-fleet merge --manifest results/job.json --output results/final.csv
```

Status rejects changed inputs, duplicate document ownership, unknown state rows,
and missing outcomes. Merge also rejects incomplete jobs and existing output,
then produces one state database and its CSV projections atomically per file.

`fetch-fleet create` rejects GeoNode with more than one shard. See
[Sticky slots](proxies.md#sticky-slots).
