from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from fetch.domain.types import Status
from fetch.fleet import JobManifest, Shard, load_manifest, merge, status, write_manifest
from fetch.store.outcomes import OutcomeRecord, OutcomeStore, state_path_for_output


def _outcome(doc: str) -> OutcomeRecord:
    return OutcomeRecord(
        site="sunat",
        doc=doc,
        status=Status.OK,
        rows=(("DNI", "12345678", "Ada", "PERSONA NATURAL"),),
        error_code="",
        error_detail="",
        attempt_count=0,
        session_id="session",
        proxy_id="proxy",
        provider="geonode",
        finished_at="2026-08-28T00:00:00+00:00",
    )


def _manifest(tmp_path: Path, *docs: str) -> JobManifest:
    shards = []
    for index, doc in enumerate(docs, start=1):
        input_path = tmp_path / f"input-{index}.csv"
        input_path.write_text(f"{doc}\n", encoding="utf-8")
        output_path = tmp_path / f"out-{index}.csv"
        shards.append(
            Shard(
                name=f"shard-{index}",
                host=f"host-{index}",
                input_path=str(input_path),
                input_sha256=sha256(input_path.read_bytes()).hexdigest(),
                output_path=str(output_path),
                state_path=str(state_path_for_output(output_path)),
            )
        )
    return JobManifest(
        version=1,
        revision="abc123",
        sites=("sunat",),
        providers=("dataimpulse",),
        shards=tuple(shards),
    )


def _write_state(shard: Shard, outcome: OutcomeRecord) -> None:
    with OutcomeStore(Path(shard.state_path)) as store:
        store.record_snapshot(outcome)


def test_manifest_round_trip_and_status_reconciles_all_shards(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "10100000001", "10100000002")
    path = tmp_path / "job.json"
    write_manifest(path, manifest)
    assert load_manifest(path) == manifest

    for shard, doc in zip(manifest.shards, ("10100000001", "10100000002"), strict=True):
        _write_state(shard, _outcome(doc))

    report = status(manifest)
    assert "Expected pairs: 2" in report
    assert "Uncovered pairs: 0" in report
    assert "ok: 2" in report


def test_manifest_status_rejects_duplicate_document_ownership(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "10100000001", "10100000001")
    with pytest.raises(ValueError, match="duplicate document ownership"):
        status(manifest)


def test_merge_requires_complete_non_overlapping_shards_and_exports(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, "10100000001", "10100000002")
    for shard, doc in zip(manifest.shards, ("10100000001", "10100000002"), strict=True):
        _write_state(shard, _outcome(doc))

    output = tmp_path / "merged.csv"
    merge(manifest, output)

    with OutcomeStore(state_path_for_output(output)) as store:
        assert len(list(store.outcomes())) == 2
    assert output.with_name("merged.sunat.csv").exists()


def test_merge_rejects_retryable_failures(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "10100000001")
    failed = replace(_outcome("10100000001"), status=Status.FAILED, attempt_count=1)
    _write_state(manifest.shards[0], failed)

    with pytest.raises(ValueError, match="retryable failure"):
        merge(manifest, tmp_path / "merged.csv")
