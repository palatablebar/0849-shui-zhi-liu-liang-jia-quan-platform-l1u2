"""更正存储层测试：版本链、待核标记与多组件隔离。"""
import pytest

from app.corrections.store import CorrectionStore, _jsonable
from app.review.store import Store
from tests.domain_cases import row


def approved_payload(rows):
    """用整理+计算+判定构造一个可签发的批次 payload。"""
    from app.domain.decision import decide_rows
    from app.domain.calculate import calculate_rows
    from app.domain.prepare import prepare_rows
    prepared, issues = prepare_rows(rows)
    results = decide_rows(calculate_rows(prepared))
    summary = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for item in results:
        summary[item["verdict"]] += 1
    from app.catalog import SPEC
    snapshot = [{key: item.get(key) for key in SPEC["columns"]} for item in prepared]
    return {
        "run_key": "key-1", "profile": "custom", "source": {"sha256": "x"},
        "counts": {}, "summary": summary, "ledger": None, "rows": results,
        "baseline_samples": snapshot,
    }, results


def test_emptied_component_keeps_old_versions_and_flags_pending(tmp_path):
    db = tmp_path / "w.sqlite3"
    runs = Store(db)
    corrections = CorrectionStore(db)
    rows = [
        row(sample_id="D1", station_id="S9", sample_at="2026-10-10 08:00", concentration="1.0", flow_m3s="1"),
        row(sample_id="D2", station_id="S9", sample_at="2026-10-20 08:00", concentration="1.0", flow_m3s="1"),
        row(sample_id="E1", station_id="S9", sample_at="2026-11-05 08:00", concentration="1.0", flow_m3s="1"),
    ]
    payload, _ = approved_payload(rows)
    run_row = runs.create("key-1", "custom", _jsonable(payload))
    runs.review(run_row["id"], "APPROVED", "基线", 0)

    result = corrections.apply_batch("custom", [
        {"sample_id": "D1", "corrected_sample_at": "2026-11-02 08:00"},
        {"sample_id": "D2", "corrected_sample_at": "2026-11-03 08:00"},
    ], "hold-key")
    # D1/D2 全部离开 10 月 → 10 月空，组件整体待核，不产生新版本
    held = {(h["station_id"], h["month"]) for h in result["held"]}
    assert ("S9", "2026-10") in held
    versions = corrections.month_versions("S9", "2026-10")
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["is_current"] is True
    current = corrections.current_results("custom")
    oct_row = next(r for r in current["results"] if r["station_id"] == "S9" and r["month"] == "2026-10")
    assert oct_row["review_state"] == "PENDING_REVIEW"


def test_independent_components_publish_and_hold_separately(tmp_path):
    db = tmp_path / "w2.sqlite3"
    runs = Store(db)
    corrections = CorrectionStore(db)
    rows = [
        row(sample_id="D1", station_id="S9", sample_at="2026-10-10 08:00"),
        row(sample_id="D2", station_id="S9", sample_at="2026-10-20 08:00"),
        row(sample_id="E1", station_id="S8", sample_at="2026-10-05 08:00"),
        row(sample_id="E2", station_id="S8", sample_at="2026-11-05 08:00"),
        row(sample_id="E3", station_id="S8", sample_at="2026-11-15 08:00"),
    ]
    payload, _ = approved_payload(rows)
    run_row = runs.create("key-2", "custom", _jsonable(payload))
    runs.review(run_row["id"], "APPROVED", "基线", 0)
    # S9：10 月两条全部迁走 → 阻断待核；S8：11 月仍留 E3，E2 迁入 10 月 → 可发布
    result = corrections.apply_batch("custom", [
        {"sample_id": "D1", "corrected_sample_at": "2026-11-02 08:00"},
        {"sample_id": "D2", "corrected_sample_at": "2026-11-03 08:00"},
        {"sample_id": "E2", "corrected_sample_at": "2026-10-15 08:00"},
    ], "mixed-key")
    held = {(h["station_id"], h["month"]) for h in result["held"]}
    published = {(p["station_id"], p["month"]) for p in result["published"]}
    assert held == {("S9", "2026-10"), ("S9", "2026-11")}
    assert published == {("S8", "2026-10"), ("S8", "2026-11")}
    # 可签发组件的月份确实产生了新版本
    s8_oct = corrections.month_versions("S8", "2026-10")
    assert [v["version"] for v in s8_oct] == [1, 2]
    # 待核组件没有新版本
    assert [v["version"] for v in corrections.month_versions("S9", "2026-10")] == [1]


def test_repeated_correction_unique_constraint_is_ignored(tmp_path):
    db = tmp_path / "w3.sqlite3"
    runs = Store(db)
    corrections = CorrectionStore(db)
    rows = [row(sample_id="D1", station_id="S9", sample_at="2026-10-10 08:00")]
    payload, _ = approved_payload(rows)
    run_row = runs.create("key-3", "custom", _jsonable(payload))
    runs.review(run_row["id"], "APPROVED", "基线", 0)
    entry = [{"sample_id": "D1", "corrected_sample_at": "2026-11-02 08:00"}]
    first = corrections.apply_batch("custom", entry, "k-a")
    second = corrections.apply_batch("custom", entry, "k-b")
    # 第二次批次存在，但同一条更正不会重复登记
    assert first["batch_id"] != second["batch_id"]
    all_rows = corrections.list_corrections()
    assert len([r for r in all_rows if r["sample_id"] == "D1"]) == 1
