def approve_clean(client):
    run = client.post("/api/runs", json={"profile": "clean"}).get_json()
    client.post(f"/api/runs/{run['run_id']}/review",
                json={"decision": "APPROVED", "comment": "基线签发", "expected_revision": 0})
    return run


def submit(client, key, corrections, profile="clean", comment="更正"):
    return client.post("/api/corrections", json={
        "idempotency_key": key, "profile": profile, "comment": comment, "corrections": corrections})


def test_correction_requires_approved_baseline(client):
    response = submit(client, "k1", [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}])
    assert response.status_code == 409
    assert response.get_json()["code"] == "NO_APPROVED_BASELINE"


def test_bad_request_shapes_are_rejected(client):
    approve_clean(client)
    assert submit(client, "k", []).status_code == 400
    response = submit(client, "k", [{"sample_id": "", "corrected_sample_at": "2026-12-01 08:00"}])
    assert response.status_code == 400


def test_cross_month_correction_publishes_linked_months(client):
    run = approve_clean(client)
    # clean.csv 全部样本在 2026-11；W001 迁到 12 月，S1 的 11/12 月构成关联组件
    response = submit(client, "batch-1", [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}])
    assert response.status_code == 200
    batch = response.get_json()
    assert batch["baseline_run_id"] == run["run_id"]
    published = {(p["station_id"], p["month"]) for p in batch["published"]}
    assert ("S1", "2026-11") in published
    assert ("S1", "2026-12") in published
    assert batch["held"] == []


def test_unrelated_month_keeps_current_version(client):
    approve_clean(client)
    submit(client, "batch-1", [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}])
    current = client.get("/api/corrections/current?profile=clean").get_json()
    by_key = {(r["station_id"], r["month"]): r for r in current["results"]}
    # S2/S3 未被更正牵连，仍为 v1 现行版
    assert by_key[("S2", "2026-11")]["version"] == 1
    # S1 两月发布 v2（12 月为新建 v1）
    assert by_key[("S1", "2026-11")]["version"] == 2
    assert by_key[("S1", "2026-12")]["version"] == 1


def test_current_results_show_flow_weighted_values(client):
    approve_clean(client)
    current = client.get("/api/corrections/current?profile=clean").get_json()
    s1_nov = next(r for r in current["results"] if r["station_id"] == "S1" and r["month"] == "2026-11")
    # 基线：W001(1.0,q1) W002(0.2,q2) W003(1.2,q1) → (1+0.4+1.2)/4 = 0.65
    assert round(s1_nov["mean"], 4) == 0.65
    assert s1_nov["review_state"] == "CURRENT"


def test_idempotent_retry_does_not_apply_twice(client):
    approve_clean(client)
    payload = [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}]
    first = submit(client, "dup-key", payload).get_json()
    second = submit(client, "dup-key", payload).get_json()
    assert second.get("idempotent_replay") is True
    assert first["batch_id"] == second["batch_id"]
    current = client.get("/api/corrections/current?profile=clean").get_json()
    # 重试不重复生效：12 月只有一条迁入样本
    s1_dec = next(r for r in current["results"] if r["station_id"] == "S1" and r["month"] == "2026-12")
    assert s1_dec["count"] == 1
    assert s1_dec["version"] == 1


def test_version_chain_keeps_old_evidence(client):
    approve_clean(client)
    submit(client, "batch-1", [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}])
    versions = client.get("/api/stations/S1/months/2026-11/versions").get_json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    old, new = versions
    assert old["is_current"] is False
    assert new["is_current"] is True
    # 旧版证据仍可查
    evidence = client.get(f"/api/month-versions/{old['id']}/evidence")
    assert evidence.status_code == 200
    artifact = evidence.get_json()
    assert artifact["schema"] == "month-evidence/v1"
    assert artifact["payload"]["count"] == 3  # 旧版 11 月仍含 W001
    # 新版不再含 W001
    new_evidence = client.get(f"/api/month-versions/{new['id']}/evidence").get_json()
    assert new_evidence["payload"]["count"] == 2


def test_original_run_evidence_still_served(client):
    run = approve_clean(client)
    submit(client, "batch-1", [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}])
    response = client.get(f"/api/runs/{run['run_id']}/evidence")
    assert response.status_code == 200
    assert response.get_json()["run_id"] == run["run_id"]


def test_emptied_month_holds_old_and_flags_pending(client):
    approve_clean(client)
    # S3 的 11 月只有 W006 一条，迁走后 11 月为空 → 重算失败，组件保留旧版并标待核
    response = submit(client, "empty-batch", [
        {"sample_id": "W006", "corrected_sample_at": "2026-12-15 08:00"}])
    batch = response.get_json()
    held = {(h["station_id"], h["month"]) for h in batch["held"]}
    assert ("S3", "2026-11") in held
    assert ("S3", "2026-12") in held
    assert batch["published"] == []
    current = client.get("/api/corrections/current?profile=clean").get_json()
    by_key = {(r["station_id"], r["month"]): r for r in current["results"]}
    # 现行版仍是旧版 v1，但带待核标记
    assert by_key[("S3", "2026-11")]["version"] == 1
    assert by_key[("S3", "2026-11")]["review_state"] == "PENDING_REVIEW"
    assert "MONTH_EMPTY" in by_key[("S3", "2026-11")]["pending_reason"]
    # 12 月没有发布新版本
    assert ("S3", "2026-12") not in by_key


def test_unknown_version_and_month_are_404(client):
    approve_clean(client)
    assert client.get("/api/month-versions/nope/evidence").status_code == 404
    assert client.get("/api/stations/S9/months/2026-01/versions").status_code == 404


def test_batch_can_be_read_back(client):
    approve_clean(client)
    batch = submit(client, "batch-1", [
        {"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}]).get_json()
    response = client.get(f"/api/corrections/batches/{batch['batch_id']}")
    assert response.status_code == 200
    record = response.get_json()
    assert record["corrections"][0]["sample_id"] == "W001"
    assert record["corrections"][0]["old_sample_at"].startswith("2026-11-01")


def test_pending_flag_clears_after_successful_publish(client):
    approve_clean(client)
    # 先制造待核
    submit(client, "hold", [{"sample_id": "W006", "corrected_sample_at": "2026-12-15 08:00"}])
    current = client.get("/api/corrections/current?profile=clean").get_json()
    s3 = next(r for r in current["results"] if r["station_id"] == "S3")
    assert s3["review_state"] == "PENDING_REVIEW"
    # 再提交一笔可签发的同站更正（W006 改到同月另一天，11 月不再为空）
    ok = submit(client, "fix", [{"sample_id": "W006", "corrected_sample_at": "2026-11-20 08:00"}])
    assert ok.status_code == 200
    current2 = client.get("/api/corrections/current?profile=clean").get_json()
    s3_after = next(r for r in current2["results"] if r["station_id"] == "S3" and r["month"] == "2026-11")
    assert s3_after["review_state"] == "CURRENT"
    assert s3_after["version"] == 2
