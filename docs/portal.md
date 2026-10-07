# Portal user guide

Use the portal to share lookup jobs and results with a team. Use
[`fetch`](../packages/cli/readme.md) for an unattended command-line run.

## Access and roles

The first administrator and team come from `portal-admin provision`. Finish TOTP
or passkey setup at `/security`; a site administrator needs a second factor
before using the portal. Invitations create an account and assign it to a team.

| Role               | Access                                                                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Team member        | Search team results and view job progress.                                                                                                    |
| Team leader        | Submit and cancel jobs, download results, manage members, and manage proxy connections.                                                       |
| Site administrator | Without belonging to a team: view, cancel, and download any team's jobs, manage any team's members, and see worker and proxy-provider health. |

Global search requires site-admin access or a team's paid global-search
entitlement.

## Prepare a team

A team leader adds and validates a provider connection, then invites members.
Provider credentials are encrypted and are not shown again after saving.

## Submit a lookup

1. Choose **Nueva consulta** for the team.
2. Upload a CSV with one identifier in its first column. See the
   [input format](input-format.md) for accepted forms.
3. Select the sources and a validated provider connection.
4. Review accepted, excluded, reusable, and new items.
5. Choose **Reutilizar y consultar solo lo nuevo** or **Consultar todo de
   nuevo**.

Duplicates and invalid rows are excluded before a worker receives them. Reuse
avoids a new lookup for an existing document and source. A lookup with no
matching records is still a valid result. A result is reusable only while it is
within the source's freshness window and has a successful or explicitly
not-found outcome. Expired results return to the new-items list.

## Follow and download

Open the job to see progress and item counts. A team leader or a site
administrator can cancel a queued or running job with **Cancelar consulta**. A
worker holding the job stops writing as soon as it is cancelled. Leaders and
site administrators can download results after the job publishes them. Members
can view individual published items.

The terminal states are `completed`, `failed`, and `cancelled`. Use the
[Troubleshooting](troubleshooting.md) when a job fails.

## Search and manage a team

Team search reads collected results; it does not start a provider lookup. Global
search spans the installation. Team leaders manage members, invitations, proxy
connections, and team jobs. Notifications show terminal job activity visible to
the signed-in user.

## Administer the installation

A site administrator works from `/admin`:

- **Members:** open a team and use its members page,
  `/teams/<team id>/settings/members`, to add a member, send an invitation, or
  remove someone. A team keeps at least one leader.
- **Jobs:** open any team's job to follow or cancel it.
- **Health:** `/admin/system` shows the queue, each worker, and for every site
  and proxy provider whether it is available or blocked until a time after
  repeated failures. A blocked pair is not assigned to any worker until the time
  passes.

Workers and master keys are administered from the command line. See
[Portal deployment](portal-deployment.md). For system boundaries, see
[Architecture](architecture.md).
