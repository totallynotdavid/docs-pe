# osiptel

Tools for bulk-looking-up public data about Peruvian RUCs (tax IDs) from government
and carrier sites. This is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)
holding three packages, split by the mechanism a site demands: a plain HTTP client, an
automated browser, or a human at a reputable browser.

- [`packages/fetch`](packages/fetch/readme.md) reads sites that answer a plain HTTP
  request. It bulk-looks-up OSIPTEL phone-line counts and SUNAT identity records for a
  CSV of RUCs, fanned out across proxied async lanes and backed by a resume database.
  This is the workhorse and the package you almost always want.
- [`packages/browser`](packages/browser/readme.md) reads sites that need a real browser.
  It drives Google Chrome over the DevTools protocol on a headless server, one prepared
  page per site. Entel's reCAPTCHA-v3 debt page is the first site.
- [`packages/capture`](packages/capture/readme.md) is the discovery tool you reach for
  first when adding a site. It intercepts a site's own calls from your everyday Chrome so
  you can learn its recipe, and collects through that reputable browser when automation
  cannot clear the gate. Standard library only, no browser launched.
- [`packages/jobs`](packages/jobs/readme.md) is the separately deployable authenticated
  internal jobs site. It uses only the stable HTTP `fetch` adapters and has its own
  database-backed worker lease protocol; it does not integrate with CRM.

## Working in the repo

The toolchain (uv, python, ruff) is pinned in `mise.toml`. Run tasks from the repo root:

```sh
mise install          # install the toolchain
mise run install      # sync all packages and dev dependencies
mise run format       # ruff format + ruff check --fix
mise run check        # mypy, one pass per package
mise run test         # pytest across all packages
mise run build        # build the fetch standalone binary
mise run portal:dev   # start PostgreSQL, migrate/bootstrap the portal, then run it
```

Proxy credentials for `fetch` load from a gitignored `.env`; copy `.env.example` to
start. `browser` and `capture` need no credentials.
