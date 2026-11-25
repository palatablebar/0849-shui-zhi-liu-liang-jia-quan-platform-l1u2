from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline
from app.corrections import service as correction_service


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


@api.post("/api/corrections")
def submit_correction():
    data = request.get_json(silent=True) or {}
    return jsonify(correction_service.submit(store_corrections(), data))


@api.get("/api/corrections/batches/<batch_id>")
def get_correction_batch(batch_id):
    record = store_corrections().get_batch(batch_id)
    if not record:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    record["corrections"] = store_corrections().list_corrections(batch_id)
    return jsonify(record)


@api.get("/api/corrections/current")
def current_results():
    profile = request.args.get("profile", "clean")
    return jsonify(correction_service.current(store_corrections(), profile))


@api.get("/api/stations/<station_id>/months/<month>/versions")
def month_versions(station_id, month):
    versions = store_corrections().month_versions(station_id, month)
    if not versions:
        raise DomainIssue("VERSION_NOT_FOUND", 404)
    return jsonify(station_id=station_id, month=month, versions=versions)


@api.get("/api/month-versions/<version_id>/evidence")
def month_version_evidence(version_id):
    evidence = store_corrections().version_evidence(version_id)
    if evidence is None:
        raise DomainIssue("VERSION_NOT_FOUND", 404)
    return jsonify(evidence)


def store_corrections():
    from flask import current_app
    return current_app.extensions["corrections"]
