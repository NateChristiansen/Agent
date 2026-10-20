"""The schema-1 usage record and checkpoint validators."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import yaml

from agent_cli.usage import checkpoint_problems, record_problems
from usage_helpers import sample_checkpoint, sample_record


def test_good_record_passes():
    assert record_problems(sample_record()) == []


def test_good_record_passes_after_a_yaml_round_trip():
    updated = datetime(2026, 10, 10, 9, 15, 2, tzinfo=timezone(timedelta(hours=-6)))
    parsed = yaml.safe_load(yaml.safe_dump(sample_record(date=date(2026, 10, 9), updated=updated)))
    assert isinstance(parsed["updated"], datetime)
    assert record_problems(parsed) == []


def test_missing_field_is_refused():
    record = sample_record()
    del record["tokens_worker"]
    assert "tokens_worker must be a non-negative integer" in record_problems(record)


def test_class_sum_mismatch_is_refused():
    record = sample_record(tokens_other=201)
    assert record_problems(record) == ["tokens_* sum to 1501, not total_tokens 1500"]


def test_model_sum_mismatch_is_refused():
    record = sample_record()
    record["models"]["gpt-6-sol"]["cache_read"] = 401
    assert record_problems(record) == ["models sum to 1501 tokens, not total_tokens 1500"]


def test_zero_written_for_an_unavailable_source_is_refused():
    record = sample_record(status="partial")
    record["sources"]["codex"] = {"status": "unavailable", "reason": "no access", "files": 0, "requests": 0}
    problems = record_problems(record)
    assert "sources.codex is unavailable and must not carry files" in problems
    assert "sources.codex is unavailable and must not carry requests" in problems


def test_unavailable_source_left_out_passes():
    record = sample_record(status="partial", requests=2, total_tokens=1000, tokens_other=0, tokens_worker=200)
    del record["models"]["gpt-6-sol"]
    record["sources"]["codex"] = {"status": "unavailable", "reason": "no access"}
    assert record_problems(record) == []


def test_unavailable_source_needs_a_reason():
    record = sample_record(status="partial")
    record["sources"]["claude"] = {"status": "unavailable"}
    assert "sources.claude is unavailable and needs a reason" in record_problems(record)


def test_no_token_source_read_means_no_token_fields():
    record = sample_record(status="partial")
    record["sources"]["claude"] = {"status": "unavailable", "reason": "a"}
    record["sources"]["codex"] = {"status": "unavailable", "reason": "b"}
    assert "no token source was read, so total_tokens must be absent" in record_problems(record)


def test_status_must_follow_the_sources():
    record = sample_record()
    record["sources"]["codex"] = {
        "status": "partial",
        "reason": "1 file unreadable",
        "files": 1,
        "requests": 1,
    }
    assert record_problems(record) == ["status must be partial for these sources, not 'complete'"]


def test_costs_unavailable_means_no_cost_fields():
    record = sample_record(status="partial")
    record["sources"]["costs"] = {"status": "unavailable", "reason": "unreadable"}
    assert "costs are unavailable, so dispatch_usd must be absent" in record_problems(record)


def test_good_checkpoint_passes():
    assert checkpoint_problems(sample_checkpoint()) == []


def test_checkpoint_covered_through_must_be_the_minimum():
    checkpoint = sample_checkpoint()
    checkpoint["sources"]["codex"]["covered_through"] = "2026-10-10T08:00:00-06:00"
    assert checkpoint_problems(checkpoint) == ["covered_through must be the minimum over the sources"]


def test_checkpoint_needs_an_offset():
    checkpoint = sample_checkpoint(covered_through="2026-10-10T09:00:00")
    assert "covered_through must be a timestamp with a UTC offset" in checkpoint_problems(checkpoint)


def _absent_codex(**changes: object) -> dict[str, object]:
    record = sample_record()
    record["sources"]["codex"] = {
        "status": "complete",
        "files": 0,
        "requests": 1,
        "absent": True,
        "path": "/h/.codex/sessions",
    }
    record["sources"]["codex"].update(changes)
    return record


def test_an_absent_folder_is_complete_zero_files_with_its_path():
    assert record_problems(_absent_codex()) == []
    assert record_problems(_absent_codex(files=1)) == [
        "sources.codex is absent, so it was read as zero files, not unavailable"
    ]
    assert record_problems(_absent_codex(path="")) == ["sources.codex is absent and needs the path checked"]
    assert record_problems(_absent_codex(absent=False)) == [
        "sources.codex.absent must be true when present, with path"
    ]
