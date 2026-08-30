# Documentation

Start with the task you need to complete. The root [README](../readme.md) is
the shortest user path; this page is the maintainer and operator index.

## Use the tools

| Task | Guide |
| --- | --- |
| Run an HTTP lookup | [`fetch`](../packages/cli/readme.md) |
| Run a browser lookup | [`browser`](../packages/browser/readme.md) |
| Discover a request | [`capture`](../packages/capture/readme.md) |
| Submit jobs through the portal | [`portal`](../packages/portal/readme.md) |

## Operate jobs and services

| Task | Guide |
| --- | --- |
| Configure proxy providers | [`Proxy configuration`](proxies.md) |
| Diagnose a standalone or portal run | [`Troubleshooting`](operations/troubleshooting.md) |
| Deploy the portal | [`Portal deployment`](operations/portal-deployment.md) |
| Add or remove a worker node | [`Worker fleet`](operations/worker-fleet.md) |
| Run a multi-host fetch | [`Sharded fetch`](operations/sharded-fetch.md) |
| Perform trusted portal SQL intervention | [`Portal SQL runbook`](../packages/portal/operations.md) |
| Read dated measurements | [`Historical results`](reports/results.md) |

## Change the system

| Task | Guide |
| --- | --- |
| Understand ownership and runtime flow | [`Architecture`](../ARCHITECTURE.md) |
| Add or change a site | [`Adding a site`](adding-a-site.md) |
| Set up and validate a change | [`Contributing`](../CONTRIBUTING.md) |
| Understand a package | [`core`](../packages/core/readme.md), [`fetch`](../packages/cli/readme.md), [`browser`](../packages/browser/readme.md), [`capture`](../packages/capture/readme.md), or [`portal`](../packages/portal/readme.md) |

## Site contracts

| Site | Runner | Contract |
| --- | --- | --- |
| Entel | Browser and capture | [`entel.md`](sites/entel.md) |
| OSIPTEL | Fetch | [`osiptel.md`](sites/osiptel.md) |
| Portabilidad | Browser | [`portabilidad.md`](sites/portabilidad.md) |
| SUNAT and representatives | Fetch | [`sunat.md`](sites/sunat.md) |
