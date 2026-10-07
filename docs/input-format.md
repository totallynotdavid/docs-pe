# Input format

The command-line runners read identifiers from the first column of a CSV. They
strip surrounding whitespace and ignore blank or invalid rows, including a
header row. `browser` and `capture` always remove duplicates. `fetch` does by
default and keeps them with `--no-dedupe`.

```csv
2953322
20100000001
```

| Runner    | Accepted identifier forms                                                                                                       |
| --------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `fetch`   | Seven- or eight-digit DNI, or eleven-digit RUC.                                                                                 |
| `browser` | Nine-digit mobile number starting with 9, seven- or eight-digit DNI, or eleven-digit RUC. A site may accept only some of these. |
| `capture` | Eleven-digit RUC.                                                                                                               |

A seven-digit DNI omits the leading zero used by the canonical eight-digit form.
`fetch` and `browser` normalize `2953322` to `02953322` before deduplication and
lookup. This matters when the same person appears once in each form.

`fetch` sends each identifier only to the sites that accept it: `sunat` takes
RUCs that do not start with 20 (natural persons), `sunat_reps` takes RUCs that
start with 20 (legal entities), and `osiptel` takes any DNI or RUC.
