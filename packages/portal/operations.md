# Portal SQL runbook

Use this runbook only from a trusted host when the web application cannot
perform the operation. Set `PORTAL_DATABASE_DSN` to the same database used by
the portal and stop if a statement does not return the expected row.

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1
```

All commands below are PostgreSQL. Standalone fetch state is SQLite and is
documented in [fetch](../cli/readme.md).

## Add a team member

Substitute the team slug and user email. The membership is idempotent only if
the unique constraint rejects a duplicate, so verify the result before trying
again.

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1 \
  -v team_slug='equipo-lima' \
  -v user_email='newuser@example.org' \
  -c "
    insert into portal_team_memberships (team_id, user_id, role)
    select t.id, u.id, 'team_member'
    from portal_teams t
    cross join portal_users u
    where t.slug = :'team_slug'
      and u.email = :'user_email'
    on conflict (team_id, user_id) do nothing
    returning team_id, user_id, role;
  "
```

If no row is returned, check that both the team and user exist:

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1 \
  -v team_slug='equipo-lima' \
  -v user_email='newuser@example.org' \
  -c "
    select t.slug, u.email
    from portal_teams t
    cross join portal_users u
    where t.slug = :'team_slug' or u.email = :'user_email';
  "
```

Use the application to grant a leader role. A leader can manage team access, so
making that change in SQL should be an explicit administrative decision.

## Inspect recent jobs

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1 \
  -v team_slug='equipo-lima' \
  -c "
    select j.id, j.state, j.created_at
    from portal_jobs j
    join portal_teams t on t.id = j.team_id
    where t.slug = :'team_slug'
    order by j.created_at desc
    limit 10;
  "
```

## Inspect fleet breaker state

The breaker is keyed by `(source, provider)`. `open_until` is null or in the
past when the pair may be claimed.

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1 -c "
  select source, provider, consecutive_failures, level, open_until, updated_at
  from portal_circuit_breakers
  order by source, provider;
"
```

## Cancel a job

Cancellation changes the job state and advances its lease fence. Workers with
the previous fence must reject their later writes. The `returning` clause is a
postcondition check, not a request to retry blindly.

```sh
psql "$PORTAL_DATABASE_DSN" -v ON_ERROR_STOP=1 \
  -v job_id='00000000-0000-0000-0000-000000000000' \
  -c "
    update portal_jobs
       set state = 'cancelling',
           lease_fence = lease_fence + 1
     where id = :'job_id'
       and state in ('queued', 'running')
    returning id, state, lease_fence;
  "
```

The worker sweeper completes the cancellation. Re-query the job before reporting
it as finished.
