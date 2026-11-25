# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 采样时间更正

- `POST /api/corrections`：提交更正批次。请求体：
  `{"idempotency_key": "幂等键", "profile": "clean", "comment": "说明",
    "corrections": [{"sample_id": "W001", "corrected_sample_at": "2026-12-01 08:00"}]}`。
  跨月水样只计入更正后的月份；同站同月的多笔更正按并查集串成关联组件。
  组件内任一月重算失败（`MONTH_EMPTY`）或签发阻断（`REJECT`）时，牵连月份
  全部保留旧版并标记 `PENDING_REVIEW`；可签发的关联月份在同一批次一起发布。
  同一 `idempotency_key` 重试返回首次结论（`idempotent_replay=true`），不重复生效。
- `GET /api/corrections/batches/{batch_id}`：回读更正批次及其条目。
- `GET /api/corrections/current?profile=clean`：工作台查询现行流量加权结果，
  含版本号与待核标记（`review_state` 为 `CURRENT` 或 `PENDING_REVIEW`）。
- `GET /api/stations/{station_id}/months/{month}/versions`：列出该站点-月份的版本链，
  旧版 `is_current=false` 但保留完整快照。
- `GET /api/month-versions/{version_id}/evidence`：下载指定版本证据（旧版仍可查）。

原 `GET /api/runs/{id}/evidence` 不受更正影响，按原签发始终可查到旧版证据。
