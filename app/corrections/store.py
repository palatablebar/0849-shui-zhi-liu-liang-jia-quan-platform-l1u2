"""采样时间更正的持久层：版本链、待核标记与幂等控制。"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.corrections.engine import build_plan
from app.domain.decision import decide_rows
from app.domain.calculate import calculate_rows
from app.domain.prepare import prepare_rows
from app.intake.service import load


def _now():
    return datetime.now(timezone.utc).isoformat()


class CorrectionStore:
    def __init__(self, path):
        self.path = path
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS month_versions(
              id TEXT PRIMARY KEY,
              station_id TEXT NOT NULL,
              month TEXT NOT NULL,
              version INTEGER NOT NULL,
              status TEXT NOT NULL,
              is_current INTEGER NOT NULL,
              source_run_id TEXT,
              batch_id TEXT,
              created_at TEXT NOT NULL,
              payload TEXT NOT NULL,
              UNIQUE(station_id, month, version)
            );
            CREATE TABLE IF NOT EXISTS month_flags(
              station_id TEXT NOT NULL,
              month TEXT NOT NULL,
              state TEXT NOT NULL,
              batch_id TEXT,
              reason TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(station_id, month)
            );
            CREATE TABLE IF NOT EXISTS correction_batches(
              id TEXT PRIMARY KEY,
              idempotency_key TEXT UNIQUE NOT NULL,
              profile TEXT NOT NULL,
              baseline_run_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS corrections(
              id TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL,
              sample_id TEXT NOT NULL,
              old_sample_at TEXT NOT NULL,
              corrected_sample_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(sample_id, corrected_sample_at)
            );
            ''')

    # ---------- 基线 ----------
    def latest_approved_run(self, profile):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE profile=? AND status='APPROVED' ORDER BY created_at DESC, revision DESC LIMIT 1",
                (profile,)).fetchone()
        return dict(row) if row else None

    def baseline_items(self, run_or_profile):
        """取得基线样本：优先用已签发批次携带的同源快照，旧批次回退到样例重放。"""
        if isinstance(run_or_profile, dict):
            run = run_or_profile
            payload = json.loads(run["payload"])
            snapshot = payload.get("baseline_samples")
            if snapshot:
                items, _ = prepare_rows(snapshot)
                return items
            profile = run["profile"]
        else:
            profile = run_or_profile
        intake = load(profile)
        prepared, _ = prepare_rows(intake["rows"])
        return prepared

    def ensure_baseline_versions(self, run):
        """首次更正前把已签发批次固化为各站点-月份的 v1 版本。"""
        with self.connect() as db:
            existing = db.execute("SELECT 1 FROM month_versions WHERE source_run_id=? LIMIT 1",
                                  (run["id"],)).fetchone()
        if existing:
            return
        prepared = self.baseline_items(run)
        results = decide_rows(calculate_rows(prepared))
        stamp = _now()
        with self.connect() as db:
            for item in results:
                version_id = uuid.uuid4().hex[:12]
                db.execute(
                    "INSERT INTO month_versions(id,station_id,month,version,status,is_current,"
                    "source_run_id,batch_id,created_at,payload) VALUES(?,?,?,1,'PUBLISHED',1,?,?,?,?)",
                    (version_id, item["label"], item["month"], run["id"], None, stamp,
                     json.dumps(_jsonable(item), ensure_ascii=False)))

    # ---------- 更正批次 ----------
    def find_batch(self, idempotency_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM correction_batches WHERE idempotency_key=?",
                             (idempotency_key,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["batch_id"] = data["id"]
        data["summary"] = json.loads(data["summary"])
        return data

    def get_batch(self, batch_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM correction_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["batch_id"] = data["id"]
        data["summary"] = json.loads(data["summary"])
        return data

    def apply_batch(self, profile, entries, idempotency_key, comment=""):
        existing = self.find_batch(idempotency_key)
        if existing:
            # 同一更正重试不重复生效：直接回放首次结论
            existing["idempotent_replay"] = True
            return existing

        run = self.latest_approved_run(profile)
        if run is None:
            return {"error": "NO_APPROVED_BASELINE"}
        self.ensure_baseline_versions(run)

        baseline = self.baseline_items(run)
        plan = build_plan(baseline, entries)

        batch_id = uuid.uuid4().hex[:12]
        stamp = _now()
        published, held = [], []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 登记更正条目；重复条目依赖唯一约束静默跳过
            for entry in entries:
                sample_id = (entry.get("sample_id") or "").strip()
                corrected = (entry.get("corrected_sample_at") or "").strip()
                base = next((row for row in baseline if row["sample_id"] == sample_id), None)
                if base is None or not corrected:
                    continue
                try:
                    db.execute(
                        "INSERT INTO corrections(id,batch_id,sample_id,old_sample_at,"
                        "corrected_sample_at,created_at) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:12], batch_id, sample_id,
                         base["sample_at"].isoformat(), corrected, stamp))
                except sqlite3.IntegrityError:
                    continue

            for component in plan["components"]:
                keys = [(m["station_id"], m["month"]) for m in component["station_months"]]
                if component["action"] == "PUBLISH":
                    for month in component["months"]:
                        self._publish_month(db, month, run["id"], batch_id, stamp)
                        self._clear_flag(db, month["station_id"], month["month"])
                        published.append({"station_id": month["station_id"], "month": month["month"]})
                else:
                    for key in keys:
                        station_id, month = key
                        self._mark_pending(db, station_id, month, batch_id, component["reasons"], stamp)
                        held.append({"station_id": station_id, "month": month})

            summary = {
                "batch_id": batch_id,
                "idempotency_key": idempotency_key,
                "profile": profile,
                "baseline_run_id": run["id"],
                "comment": comment.strip(),
                "conserved": plan["conserved"],
                "problems": plan["problems"],
                "published": published,
                "held": held,
                "components": plan["components"],
            }
            db.execute(
                "INSERT INTO correction_batches(id,idempotency_key,profile,baseline_run_id,created_at,summary)"
                " VALUES(?,?,?,?,?,?)",
                (batch_id, idempotency_key, profile, run["id"], stamp,
                 json.dumps(_jsonable(summary), ensure_ascii=False)))
        return summary

    def _publish_month(self, db, month, run_id, batch_id, stamp):
        row = db.execute(
            "SELECT MAX(version) AS v FROM month_versions WHERE station_id=? AND month=?",
            (month["station_id"], month["month"])).fetchone()
        next_version = (row["v"] or 0) + 1
        db.execute("UPDATE month_versions SET is_current=0 WHERE station_id=? AND month=?",
                   (month["station_id"], month["month"]))
        version_id = uuid.uuid4().hex[:12]
        payload = dict(month["result"])
        payload["comment"] = ""
        db.execute(
            "INSERT INTO month_versions(id,station_id,month,version,status,is_current,"
            "source_run_id,batch_id,created_at,payload) VALUES(?,?,?,?,'PUBLISHED',1,?,?,?,?)",
            (version_id, month["station_id"], month["month"], next_version,
             run_id, batch_id, stamp, json.dumps(_jsonable(payload), ensure_ascii=False)))

    def _mark_pending(self, db, station_id, month, batch_id, reasons, stamp):
        reason = ";".join(f"{item['code']}@{item['station_id']}-{item['month']}" for item in reasons)
        db.execute(
            "INSERT INTO month_flags(station_id,month,state,batch_id,reason,updated_at)"
            " VALUES(?,?, 'PENDING_REVIEW',?,?,?)"
            " ON CONFLICT(station_id,month) DO UPDATE SET state='PENDING_REVIEW',"
            " batch_id=excluded.batch_id, reason=excluded.reason, updated_at=excluded.updated_at",
            (station_id, month, batch_id, reason, stamp))

    def _clear_flag(self, db, station_id, month):
        db.execute("DELETE FROM month_flags WHERE station_id=? AND month=?", (station_id, month))

    # ---------- 查询 ----------
    def current_results(self, profile):
        run = self.latest_approved_run(profile)
        if run is None:
            return {"error": "NO_APPROVED_BASELINE"}
        self.ensure_baseline_versions(run)
        with self.connect() as db:
            rows = db.execute(
                "SELECT v.*, f.state AS flag_state, f.reason AS flag_reason FROM month_versions v"
                " LEFT JOIN month_flags f ON f.station_id=v.station_id AND f.month=v.month"
                " WHERE v.is_current=1 ORDER BY v.station_id, v.month").fetchall()
        items = []
        for row in rows:
            item = json.loads(row["payload"])
            item.update(station_id=row["station_id"], month=row["month"], version=row["version"],
                        review_state=row["flag_state"] or "CURRENT",
                        pending_reason=row["flag_reason"] or "")
            items.append(_jsonable(item))
        return {"profile": profile, "baseline_run_id": run["id"], "results": items}

    def month_versions(self, station_id, month):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM month_versions WHERE station_id=? AND month=? ORDER BY version",
                (station_id, month)).fetchall()
        return [_serialize_version(row) for row in rows]

    def version_evidence(self, version_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM month_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            return None
        data = _serialize_version(row)
        data["schema"] = "month-evidence/v1"
        return data

    def list_corrections(self, batch_id=None):
        with self.connect() as db:
            if batch_id:
                rows = db.execute("SELECT * FROM corrections WHERE batch_id=? ORDER BY created_at",
                                  (batch_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM corrections ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]


def _serialize_version(row):
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["is_current"] = bool(data["is_current"])
    return data


def _jsonable(value):
    from decimal import Decimal
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
