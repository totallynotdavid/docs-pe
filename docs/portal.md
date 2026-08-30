# Portal user guide

Use the portal to share lookup jobs and results with a team. Use
[`fetch`](../packages/cli/readme.md) for an unattended command-line run.

## Access and roles

The first administrator and team come from `portal-admin provision`. Finish
TOTP or passkey setup at `/security/setup`; a site administrator needs a second
factor before using the portal. Invitations create an account and assign it to
a team.

| Role | Access |
| --- | --- |
| Team member | Search team results and view job progress. |
| Team leader | Submit and cancel jobs, download results, manage members, and manage proxy connections. |

Site administrators manage the installation and teams. Global search requires
site-admin access or a team's paid global-search entitlement.

## Prepare a team

A team leader adds and validates a provider connection, then invites members.
Provider credentials are encrypted and are not shown again after saving.

## Submit a lookup

1. Choose **Nueva consulta** for the team.
2. Upload a CSV with one identifier in its first column.
3. Select the sources and a validated provider connection.
4. Review accepted, excluded, reusable, and new items.
5. Choose **Reutilizar y consultar solo lo nuevo** or **Consultar todo de nuevo**.

Duplicates and invalid rows are excluded before a worker receives them. Reuse
avoids a new lookup for an existing document and source. A lookup with no
matching records is still a valid result.

## Follow and download

Open the job to see progress and item counts. A team leader can cancel a
queued or running job. Leaders and site administrators can download results
after the job publishes them. Members can view individual published items.

The terminal states are `completed`, `failed`, and `cancelled`. Use the
[troubleshooting runbook](operations/troubleshooting.md) when a job fails.

## Search and manage a team

Team search reads collected results; it does not start a provider lookup.
Global search spans the installation and is available only to site
administrators or users with the team's entitlement. Team leaders manage
members, invitations, proxy connections, and team jobs. Notifications show
terminal job activity visible to the signed-in user.

For deployment and database operations, see the
[operator documentation](readme.md#operate-jobs-and-services). For system
boundaries, see [Architecture](../ARCHITECTURE.md).
