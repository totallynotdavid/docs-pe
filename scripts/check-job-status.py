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
    results = connection.execute(
        "select status, count(*) from outcomes group by status"
    ).fetchall()

print("Job status:")
for status, count in results:
    print(f"  {status}: {count}")
