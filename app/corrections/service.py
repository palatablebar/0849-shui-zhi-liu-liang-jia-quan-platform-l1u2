"""采样时间更正的应用服务。"""
from app.corrections.store import CorrectionStore
from app.errors import DomainIssue


def correction_store():
    from flask import current_app
    return current_app.extensions["corrections"]


def submit(store: CorrectionStore, data):
    idempotency_key = (data.get("idempotency_key") or "").strip()
    profile = data.get("profile", "clean")
    entries = data.get("corrections")
    if not idempotency_key or not isinstance(entries, list) or not entries:
        raise DomainIssue("BAD_REQUEST")
    for entry in entries:
        if not entry.get("sample_id") or not entry.get("corrected_sample_at"):
            raise DomainIssue("BAD_REQUEST")
    result = store.apply_batch(profile, entries, idempotency_key, data.get("comment", ""))
    if result.get("error") == "NO_APPROVED_BASELINE":
        raise DomainIssue("NO_APPROVED_BASELINE", 409)
    return result


def current(store: CorrectionStore, profile):
    result = store.current_results(profile)
    if result.get("error") == "NO_APPROVED_BASELINE":
        raise DomainIssue("NO_APPROVED_BASELINE", 409)
    return result
