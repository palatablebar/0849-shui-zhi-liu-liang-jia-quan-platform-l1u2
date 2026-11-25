"""采样时间更正的纯领域引擎。

规则要点：
- 跨月水样只计入更正后的月份：对应用更正后的完整数据集重算，
  只有受影响的“站点-月份”才允许产生新版本。
- 多笔更正牵涉同站同月时用并查集串联为关联组件；组件内任一月
  重算失败（如月内样本被全部移走）或仍存在签发阻断（REJECT），
  组件内所有月份保留旧版并标记待核，可签发的关联月份一起发布。
"""
from copy import deepcopy

from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.domain.helpers import TS


def month_of(value):
    return value.strftime("%Y-%m")


def _validate_entries(entries):
    """校验更正条目，返回 (sample_id -> 新时间字符串, 错误列表)。"""
    corrected_at, problems = {}, []
    seen = set()
    for index, entry in enumerate(entries):
        sample_id = (entry.get("sample_id") or "").strip()
        new_at = (entry.get("corrected_sample_at") or "").strip()
        if not sample_id:
            problems.append({"index": index, "sample_id": "", "code": "CORRECTION_KEY_MISSING"})
            continue
        try:
            parsed = TS(new_at, "CORRECTION_BAD_DATETIME")
        except ValueError:
            problems.append({"index": index, "sample_id": sample_id, "code": "CORRECTION_BAD_DATETIME"})
            continue
        dedupe = (sample_id, month_of(parsed))
        if dedupe in seen:
            # 同一更正重试不重复生效
            continue
        seen.add(dedupe)
        corrected_at[sample_id] = parsed
    return corrected_at, problems


def _union_find(links):
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left, right in links:
        union(left, right)
    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), set()).add(node)
    return list(groups.values())


def build_plan(baseline_rows, entries):
    """根据基线样本与更正条目构造发布计划（纯函数，不写库）。

    返回结构包含每个关联组件的受影响月份、重算结果、阻断原因与发布动作。
    """
    corrected_at, problems = _validate_entries(entries)
    by_sample = {row["sample_id"]: row for row in baseline_rows}

    unknown = sorted(set(corrected_at) - set(by_sample))
    for sample_id in unknown:
        problems.append({"index": None, "sample_id": sample_id, "code": "CORRECTION_SAMPLE_UNKNOWN"})

    # 仅保留能定位到基线样本的更正
    corrected_at = {key: value for key, value in corrected_at.items() if key in by_sample}

    # 应用更正：复制基线行，替换采样时间
    adjusted = []
    touched = set()  # 受影响的 (station_id, month)
    links = []       # 同一笔更正串联旧月与新月
    for row in baseline_rows:
        item = deepcopy(row)
        sample_id = row["sample_id"]
        old_month = month_of(row["sample_at"])
        if sample_id in corrected_at:
            new_time = corrected_at[sample_id]
            item["sample_at"] = new_time
            new_month = month_of(new_time)
            station_id = row["station_id"]
            touched.add((station_id, old_month))
            touched.add((station_id, new_month))
            if old_month != new_month:
                links.append(((station_id, old_month), (station_id, new_month)))
        adjusted.append(item)

    # 多笔更正牵涉同站同月 → 并查集关联组件
    components = _union_find(links) if links else []
    grouped = []
    covered = set()
    for component in components:
        grouped.append(component)
        covered.update(component)
    for node in sorted(touched - covered):
        grouped.append({node})

    prepared, prepare_issues = prepare_rows(adjusted)
    results = decide_rows(calculate_rows(prepared))
    result_index = {(item["label"], item["month"]): item for item in results}

    # 守恒：更正前后样本数（含整理隔离）必须一致
    conserved = len(prepared) + len(prepare_issues) == len(baseline_rows)

    plan_components = []
    for component in grouped:
        months = []
        blocked = False
        reasons = []
        for station_id, month in sorted(component):
            key = (station_id, month)
            result = result_index.get(key)
            failure = None
            if result is None:
                # 月内样本被全部移走 → 重算失败
                failure = "MONTH_EMPTY"
            if failure:
                blocked = True
                reasons.append({"station_id": station_id, "month": month, "code": failure})
                months.append({"station_id": station_id, "month": month, "status": "BLOCKED",
                               "reason": failure, "result": None})
                continue
            verdict = result.get("verdict")
            if verdict == "REJECT":
                blocked = True
                reasons.append({"station_id": station_id, "month": month, "code": result.get("reason", "GATE_BLOCKED")})
                months.append({"station_id": station_id, "month": month, "status": "BLOCKED",
                               "reason": result.get("reason"), "result": _result_brief(result)})
            else:
                months.append({"station_id": station_id, "month": month, "status": "READY",
                               "reason": result.get("reason"), "result": _result_brief(result)})
        # 关联组件内任一月失败或阻断：牵连月份全部保留旧版、标待核
        if blocked:
            for month in months:
                month["status"] = "BLOCKED"
        action = "HOLD_OLD" if blocked else "PUBLISH"
        plan_components.append({
            "station_months": [{"station_id": station_id, "month": month}
                               for station_id, month in sorted(component)],
            "action": action,
            "blocked": blocked,
            "reasons": reasons,
            "months": months,
        })

    return {
        "problems": problems,
        "conserved": conserved,
        "touched": [{"station_id": station_id, "month": month}
                    for station_id, month in sorted(touched)],
        "components": plan_components,
        "results": [_result_brief(item) for item in results],
    }


def _result_brief(item):
    return {
        "station_id": item["label"],
        "month": item["month"],
        "mean": item["mean"],
        "flow_m3s": item["flow_m3s"],
        "count": item["count"],
        "verdict": item.get("verdict"),
        "reason": item.get("reason"),
        "problem": item.get("problem"),
    }
