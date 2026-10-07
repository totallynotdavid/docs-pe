# Documentation

The documents below are listed in reading order. The root [README](../readme.md)
is the shortest introduction.

1. [Input format](input-format.md): the identifiers each tool accepts.
2. [Proxy configuration](proxies.md): providers, credentials, sticky slots, and
   preflight.
3. [Sharded fetch](sharded-fetch.md): run one `fetch` job across hosts and merge
   the results.
4. [Troubleshooting](troubleshooting.md): read a run's state and tell failure
   classes apart.
5. [Portal](portal.md): submit lookups, follow jobs, manage a team, and
   administer the installation.
6. [Portal deployment](portal-deployment.md): release a production portal and
   its workers.
7. [Portal topology](portal-topology.md): the Compose layout, ports, and network
   boundary.
8. [Architecture](architecture.md): packages, boundaries, the code map, and the
   outcome model.
9. [Adding a site](adding-a-site.md): discover, implement, and document a new
   site.
10. [OSIPTEL](osiptel.md), [SUNAT](sunat.md), [Entel](entel.md), and
    [Portabilidad](portabilidad.md): each site's wire protocol, gates, and
    failure modes.

Each package has its own readme: [core](../packages/core/readme.md),
[fetch](../packages/cli/readme.md), [browser](../packages/browser/readme.md),
[capture](../packages/capture/readme.md), and
[portal](../packages/portal/readme.md). The disposable AWS servers for a
whole-portal test are in [AWS test servers](../infra/aws/readme.md). To change
the repository, read [Contributing](../.github/CONTRIBUTING.md).
