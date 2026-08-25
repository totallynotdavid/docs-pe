# Portal operations

Manual SQL for inspecting or changing portal state when the web UI cannot do it.
Run these commands from a trusted host. Portal tables use the `portal_` prefix.
Membership roles are `team_leader` and `team_member`.

## Add a team member

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  insert into portal_team_memberships (team_id, user_id, role)
  select t.id, u.id, 'team_leader'
  from portal_teams t, portal_users u
  where t.slug = 'equipo-lima'
    and u.email = 'newuser@example.org';
"
```

## Inspect jobs

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  select j.id, j.state, j.created_at
  from portal_jobs j
  join portal_teams t on t.id = j.team_id
  where t.slug = 'equipo-lima'
  order by j.created_at desc
  limit 10;
"
```

## Cancel a job

Move a job to `cancelling` and advance its lease fence. A worker holding the
previous lease then rejects its writes and retires the item itself.

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  update portal_jobs
  set state = 'cancelling',
      lease_fence = lease_fence + 1
  where id = '<job-uuid>'
    and state in ('queued', 'running');
"
```
