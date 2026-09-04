# Input format

The command-line runners read identifiers from the first column of a CSV.
They strip surrounding whitespace, ignore blank or invalid values, and remove
duplicates by default.

```csv
2953322
20100000001
```

| Runner | Accepted identifier forms |
| --- | --- |
| `fetch` | Seven- or eight-digit DNI, or eleven-digit RUC. |
| `browser` | Nine-digit mobile number, seven- or eight-digit DNI, or eleven-digit RUC. A site may accept only some of these. |
| `capture` | Eleven-digit RUC. |

A seven-digit DNI omits the leading zero used by the canonical eight-digit
form. `fetch` and `browser` normalize `2953322` to `02953322` before
deduplication and lookup. This matters when the same person appears once in
each form.
