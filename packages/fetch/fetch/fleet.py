from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fetch.domain.policy import MAX_TOTAL_ATTEMPTS
from fetch.domain.types import Status
from fetch.proxy.registry import PROVIDERS
from fetch.sites.registry import get_sites
from fetch.store.export import export_all
from fetch.store.outcomes import OutcomeRecord, OutcomeStore, state_path_for_output
from fetch.store.payload import decode_rows
from fetch.store.plan import read_docs


if TYPE_CHECKING:
    from collections.abc import Iterable


MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Shard:
    name: str
    host: str
    input_path: str
    input_sha256: str
    output_path: str
    state_path: str


@dataclass(frozen=True)
class JobManifest:
    version: int
    revision: str
    sites: tuple[str, ...]
    providers: tuple[str, ...]
    shards: tuple[Shard, ...]


def load_manifest(path: Path) -> JobManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"cannot read manifest {path}: {exc}"
        raise ValueError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"manifest {path} is not valid JSON: {exc}"
        raise ValueError(msg) from exc

    if raw.get("version") != MANIFEST_VERSION:
        msg = f"manifest {path} must use version {MANIFEST_VERSION}"
        raise ValueError(msg)

    try:
        shards = tuple(Shard(**entry) for entry in raw["shards"])
        manifest = JobManifest(
            version=raw["version"],
            revision=str(raw["revision"]),
            sites=tuple(raw["sites"]),
            providers=tuple(raw["providers"]),
            shards=shards,
        )
    except (KeyError, TypeError) as exc:
        msg = f"manifest {path} has an invalid shape"
        raise ValueError(msg) from exc

    _validate_manifest(manifest)
    return manifest


def write_manifest(path: Path, manifest: JobManifest) -> None:
    _validate_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": manifest.version,
                "revision": manifest.revision,
                "sites": list(manifest.sites),
                "providers": list(manifest.providers),
                "shards": [asdict(shard) for shard in manifest.shards],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def status(manifest: JobManifest) -> str:
    expected = _expected_pairs(manifest)
    observed = _read_all_outcomes(Path(shard.state_path) for shard in manifest.shards)
    unexpected = set(observed).difference(expected)
    if unexpected:
        sample = ", ".join(f"{site}:{doc}" for site, doc in sorted(unexpected)[:3])
        msg = f"state database contains pair(s) outside the manifest: {sample}"
        raise ValueError(msg)

    uncovered = expected.difference(observed)
    counts = Counter(record.status.value for record in observed.values())
    retryable = sum(
        record.status is Status.FAILED and record.attempt_count < MAX_TOTAL_ATTEMPTS
        for record in observed.values()
    )
    terminal = sum(
        record.status is Status.FAILED and record.attempt_count >= MAX_TOTAL_ATTEMPTS
        for record in observed.values()
    )

    return "\n".join(
        (
            f"Manifest revision: {manifest.revision}",
            f"Expected pairs: {len(expected)}",
            f"Observed pairs: {len(observed)}",
            f"Uncovered pairs: {len(uncovered)}",
            f"ok: {counts['ok']}",
            f"not_found: {counts['not_found']}",
            f"failed and retryable: {retryable}",
            f"failed and terminal: {terminal}",
        )
    )


def merge(manifest: JobManifest, output: Path) -> None:
    expected = _expected_pairs(manifest)
    outcomes = _read_all_outcomes(Path(shard.state_path) for shard in manifest.shards)
    missing = expected.difference(outcomes)
    if missing:
        msg = f"cannot merge with {len(missing)} uncovered pair(s)"
        raise ValueError(msg)
    retryable = sum(
        outcome.status is Status.FAILED and outcome.attempt_count < MAX_TOTAL_ATTEMPTS
        for outcome in outcomes.values()
    )
    if retryable:
        msg = f"cannot merge with {retryable} retryable failure(s)"
        raise ValueError(msg)

    state_path = state_path_for_output(output)
    if state_path.exists():
        msg = f"merge destination state database already exists: {state_path}"
        raise ValueError(msg)
    existing_exports = list(output.parent.glob(f"{output.stem}.*{output.suffix}"))
    if existing_exports:
        msg = f"merge destination already has export(s): {existing_exports[0]}"
        raise ValueError(msg)

    sites = get_sites(list(manifest.sites))
    with OutcomeStore(state_path) as destination:
        for outcome in outcomes.values():
            destination.record_snapshot(outcome)
        export_all(store=destination, output_csv=output, sites=sites)


def _expected_pairs(manifest: JobManifest) -> set[tuple[str, str]]:
    sites = get_sites(list(manifest.sites))
    expected: set[tuple[str, str]] = set()

    for shard in manifest.shards:
        input_path = Path(shard.input_path)
        if not input_path.exists():
            msg = f"shard {shard.name} input is not available: {input_path}"
            raise ValueError(msg)
        if _sha256(input_path) != shard.input_sha256:
            msg = f"shard {shard.name} input no longer matches its recorded SHA-256"
            raise ValueError(msg)

        docs, _ = read_docs(input_path, dedupe=True)
        pairs = {
            (site.name, str(doc)) for site in sites for doc in docs if site.accepts(doc)
        }
        duplicates = expected.intersection(pairs)
        if duplicates:
            sample = ", ".join(f"{site}:{doc}" for site, doc in sorted(duplicates)[:3])
            msg = f"duplicate document ownership across shards: {sample}"
            raise ValueError(msg)
        expected.update(pairs)

    return expected


def _read_all_outcomes(paths: Iterable[Path]) -> dict[tuple[str, str], OutcomeRecord]:
    outcomes: dict[tuple[str, str], OutcomeRecord] = {}
    for path in paths:
        if not path.exists():
            msg = f"state database is not available: {path}"
            raise ValueError(msg)
        for record in _read_outcomes(path):
            key = (record.site, record.doc)
            if key in outcomes:
                msg = f"duplicate outcome ownership across state databases: {key[0]}:{key[1]}"
                raise ValueError(msg)
            outcomes[key] = record
    return outcomes


def _read_outcomes(path: Path) -> Iterable[OutcomeRecord]:
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(outcomes)")
        }
        provider = "provider" if "provider" in columns else "'' AS provider"
        rows = connection.execute(
            f"""
            SELECT site, doc, status, payload, error_code, error_detail, attempt_count,
                   session_id, proxy_id, {provider}, finished_at
              FROM outcomes
             ORDER BY site, doc
            """
        )
        for row in rows:
            yield OutcomeRecord(
                site=str(row[0]),
                doc=str(row[1]),
                status=Status(str(row[2])),
                rows=decode_rows(str(row[3])),
                error_code=str(row[4]),
                error_detail=str(row[5]),
                attempt_count=int(row[6]),
                session_id=str(row[7]),
                proxy_id=str(row[8]),
                provider=str(row[9]),
                finished_at=str(row[10]),
            )


def _validate_manifest(manifest: JobManifest) -> None:
    if not manifest.revision:
        msg = "manifest revision must not be empty"
        raise ValueError(msg)
    if not manifest.sites:
        msg = "manifest must select at least one site"
        raise ValueError(msg)
    get_sites(list(manifest.sites))
    if not manifest.providers:
        msg = "manifest must list at least one provider"
        raise ValueError(msg)
    unknown_providers = sorted(set(manifest.providers).difference(PROVIDERS))
    if unknown_providers:
        msg = f"manifest lists unknown provider(s): {','.join(unknown_providers)}"
        raise ValueError(msg)
    if "geonode" in manifest.providers and len(manifest.shards) > 1:
        msg = "GeoNode cannot run across standalone shards without a shared slot allocator"
        raise ValueError(msg)
    if not manifest.shards:
        msg = "manifest must list at least one shard"
        raise ValueError(msg)

    names = [shard.name for shard in manifest.shards]
    if len(names) != len(set(names)):
        msg = "manifest shard names must be unique"
        raise ValueError(msg)
    states = [shard.state_path for shard in manifest.shards]
    if len(states) != len(set(states)):
        msg = "manifest state paths must be unique"
        raise ValueError(msg)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _create(args: argparse.Namespace) -> None:
    shards = []
    for name, host, input_raw, output_raw in args.shard:
        input_path = Path(input_raw)
        if not input_path.exists():
            msg = f"--shard {name}: input file not found: {input_path}"
            raise SystemExit(msg)
        output_path = Path(output_raw)
        shards.append(
            Shard(
                name=name,
                host=host,
                input_path=str(input_path),
                input_sha256=_sha256(input_path),
                output_path=str(output_path),
                state_path=str(state_path_for_output(output_path)),
            )
        )

    manifest = JobManifest(
        version=MANIFEST_VERSION,
        revision=args.revision,
        sites=tuple(
            site.strip().lower() for site in args.sites.split(",") if site.strip()
        ),
        providers=tuple(
            provider.strip().lower()
            for provider in args.providers.split(",")
            if provider.strip()
        ),
        shards=tuple(shards),
    )
    try:
        write_manifest(args.manifest, manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fetch-fleet")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a multi-host job manifest")
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument("--revision", required=True)
    create.add_argument("--sites", required=True)
    create.add_argument("--providers", required=True)
    create.add_argument(
        "--shard",
        action="append",
        nargs=4,
        metavar=("NAME", "HOST", "INPUT", "OUTPUT"),
        required=True,
    )

    inspect = commands.add_parser("status", help="reconcile every shard in a manifest")
    inspect.add_argument("--manifest", required=True, type=Path)

    merge_command = commands.add_parser(
        "merge", help="merge complete shard state safely"
    )
    merge_command.add_argument("--manifest", required=True, type=Path)
    merge_command.add_argument("--output", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "create":
        _create(args)
        return

    try:
        manifest = load_manifest(args.manifest)
        if args.command == "status":
            print(status(manifest))
        else:
            merge(manifest, args.output)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
