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

with sqlite3.connect(db_path) as db:
    results = db.execute("""
        select
          case
            when proxy_id like 'dataimpulse%' then 'dataimpulse'
            else 'geonode'
          end as provider,
          status,
          count(*)
        from outcomes
        group by provider, status
    """).fetchall()

print("Outcomes by provider and status:")
for provider, status, count in results:
    print(f"  {provider:12} {status:10} {count:6}")
