# Portal operations

SQL for inspecting or manually intervening on portal state directly, for when
the web UI isn't enough. Table names are prefixed `portal_`; there's no bare
`teams`, `jobs`, or `users`. Membership `role` is `team_leader` or
`team_member`.

To add a user to a team:

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  insert into portal_team_memberships (team_id, user_id, role)
  select t.id, u.id, 'team_leader'
  from portal_teams t, portal_users u
  where t.slug = 'equipo-lima'
    and u.email = 'newuser@example.org';
"
```

To list jobs for a team:

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

To manually cancel a running job, don't set `state = 'cancelled'` directly: that
bypasses lease fencing, and a worker holding the old lease can still write
results after you think you've stopped it. Do what `JobsRepository.cancel` does
instead: move to `cancelling` and bump the fence so writes from the current
lease are rejected. The worker retires the item and moves it to `cancelled`
itself.

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  update portal_jobs
  set state = 'cancelling',
      lease_fence = lease_fence + 1
  where id = '<job-uuid>'
    and state in ('queued', 'running');
"
```
