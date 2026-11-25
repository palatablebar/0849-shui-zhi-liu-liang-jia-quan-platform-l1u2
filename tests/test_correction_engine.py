from tests.domain_cases import row
from app.corrections.engine import build_plan
from app.domain.prepare import prepare_rows


def prepared(rows):
    items, _ = prepare_rows(rows)
    return items


def two_month_rows():
    """S1 在 10 月与 11 月各两条样本，S2 只在 11 月。"""
    return [
        row(sample_id="A1", station_id="S1", sample_at="2026-10-10 08:00", concentration="1.0", flow_m3s="2"),
        row(sample_id="A2", station_id="S1", sample_at="2026-10-20 08:00", concentration="2.0", flow_m3s="2"),
        row(sample_id="B1", station_id="S1", sample_at="2026-11-05 08:00", concentration="1.0", flow_m3s="1"),
        row(sample_id="B2", station_id="S1", sample_at="2026-11-15 08:00", concentration="3.0", flow_m3s="3"),
        row(sample_id="C1", station_id="S2", sample_at="2026-11-05 08:00", concentration="0.5", flow_m3s="4"),
    ]


def test_cross_month_sample_counts_only_in_new_month():
    baseline = prepared(two_month_rows())
    # A1 从 10 月迁到 11 月
    plan = build_plan(baseline, [{"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"}])
    indexed = {(r["station_id"], r["month"]): r for r in plan["results"]}
    # 10 月只剩 A2 一条
    assert indexed[("S1", "2026-10")]["count"] == 1
    # 11 月为 B1,B2 + 迁入的 A1 共三条
    assert indexed[("S1", "2026-11")]["count"] == 3
    # 守恒：总样本数不变
    assert sum(r["count"] for r in plan["results"]) == len(baseline)
    assert plan["conserved"] is True


def test_touched_months_cover_old_and_new():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [{"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"}])
    touched = {(t["station_id"], t["month"]) for t in plan["touched"]}
    assert touched == {("S1", "2026-10"), ("S1", "2026-11")}


def test_emptied_month_blocks_component_and_holds_old():
    baseline = prepared(two_month_rows())
    # 把 10 月两条全部迁走 → 10 月重算失败，关联组件整体待核
    plan = build_plan(baseline, [
        {"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"},
        {"sample_id": "A2", "corrected_sample_at": "2026-11-03 08:00"},
    ])
    component = plan["components"][0]
    assert component["action"] == "HOLD_OLD"
    assert component["blocked"] is True
    codes = {r["code"] for r in component["reasons"]}
    assert "MONTH_EMPTY" in codes
    months = {m["month"]: m for m in component["months"]}
    assert months["2026-10"]["status"] == "BLOCKED"
    # 11 月本身可算，但因同组件失败一起保留旧版
    assert months["2026-11"]["status"] == "BLOCKED"


def test_reject_gate_blocks_component():
    # 构造 11 月均值超 5 的阻断
    rows = [
        row(sample_id="D1", station_id="S9", sample_at="2026-10-10 08:00", concentration="1.0", flow_m3s="1"),
        row(sample_id="D2", station_id="S9", sample_at="2026-10-20 08:00", concentration="1.0", flow_m3s="1"),
        row(sample_id="E1", station_id="S9", sample_at="2026-11-05 08:00", concentration="12.0", flow_m3s="1"),
    ]
    baseline = prepared(rows)
    plan = build_plan(baseline, [{"sample_id": "D1", "corrected_sample_at": "2026-11-02 08:00"}])
    component = plan["components"][0]
    assert component["action"] == "HOLD_OLD"
    assert any(r["code"] == "MEAN_OVER_5" for r in component["reasons"])


def test_connected_station_month_shares_single_component():
    # A1: 10→11，B1: 11→12，同站串联 10/11/12 三个月
    rows = [
        row(sample_id="A1", station_id="S1", sample_at="2026-10-10 08:00"),
        row(sample_id="B1", station_id="S1", sample_at="2026-11-05 08:00"),
        row(sample_id="F1", station_id="S1", sample_at="2026-12-05 08:00"),
    ]
    baseline = prepared(rows)
    plan = build_plan(baseline, [
        {"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"},
        {"sample_id": "B1", "corrected_sample_at": "2026-12-02 08:00"},
    ])
    assert len(plan["components"]) == 1
    component = plan["components"][0]
    assert len(component["station_months"]) == 3


def test_independent_months_form_separate_components():
    # S1 与 S2 互不关联 → 两个组件
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [
        {"sample_id": "A1", "corrected_sample_at": "2026-10-11 08:00"},  # 同月改时间，仅触碰 10 月
    ])
    # 同月迁移不产生 link，仍按受影响月独立成组
    assert all(len(c["station_months"]) == 1 for c in plan["components"])


def test_ready_component_publishes_all_linked_months():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [{"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"}])
    component = next(c for c in plan["components"] if c["action"] == "PUBLISH")
    assert {m["month"] for m in component["months"]} == {"2026-10", "2026-11"}
    assert all(m["status"] == "READY" for m in component["months"])


def test_duplicate_correction_entry_is_deduped():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [
        {"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"},
        {"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"},
    ])
    assert plan["problems"] == []
    indexed = {(r["station_id"], r["month"]): r for r in plan["results"]}
    assert indexed[("S1", "2026-11")]["count"] == 3


def test_unknown_sample_is_reported_not_applied():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [{"sample_id": "NOPE", "corrected_sample_at": "2026-11-02 08:00"}])
    assert any(p["code"] == "CORRECTION_SAMPLE_UNKNOWN" for p in plan["problems"])
    # 没有任何受影响月份
    assert plan["touched"] == []


def test_bad_datetime_is_rejected():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [{"sample_id": "A1", "corrected_sample_at": "not-a-date"}])
    assert any(p["code"] == "CORRECTION_BAD_DATETIME" for p in plan["problems"])


def test_weighted_mean_moves_with_sample():
    baseline = prepared(two_month_rows())
    plan = build_plan(baseline, [{"sample_id": "A1", "corrected_sample_at": "2026-11-02 08:00"}])
    after = {(r["station_id"], r["month"]): r for r in plan["results"]}
    # 10 月只剩 A2(2.0, 流量2) → 加权均值 2.0
    assert float(after[("S1", "2026-10")]["mean"]) == 2.0
    # 11 月：A1(1.0,q2),B1(1.0,q1),B2(3.0,q3) → (2+1+9)/6 = 2.0
    assert float(after[("S1", "2026-11")]["mean"]) == 2.0
