#!/usr/bin/env python

import sqlite3
import sys

from pathlib import Path

from fetch.store.outcomes import state_path_for_output


output_path = Path("out.csv")
db_path = state_path_for_output(output_path)

if not db_path.exists():
    print(f"State database not found at {db_path}")
    sys.exit(1)

with sqlite3.connect(db_path) as connection:
    results = connection.execute("""
        select
          case
            when proxy_id like 'dataimpulse%' then 'dataimpulse'
            else 'geonode'
          end as provider,
          status,
          count(*) as count
        from outcomes
        where finished_at > datetime('now', '-10 minutes')
        group by provider, status
        order by provider, status
    """).fetchall()

if not results:
    print("No recent outcomes (last 10 minutes)")
else:
    print("Recent outcomes by provider (last 10 minutes):")
    for provider, status, count in results:
        print(f"  {provider:12} {status:10} {count:6}")
