# Hot Read Performance Evidence

Evidence date: 2026-07-20. Code range: `a2b6294` through `2c7b933`.

This record separates deterministic query/file budgets from deployment timing.
It contains no request values, account identifiers, credentials, or local media
paths.

## Deterministic budgets

| Read path | Fixture | Result | Evidence |
| --- | --- | --- | --- |
| Recording/upload list | 20 selected sessions after 500 off-page sessions, with parts, chunks, and danmaku | Route budget is count plus one summary query; summary query performs zero file calls and all child access starts from selected IDs | `test_list_session_summary_bounds_child_work_to_selected_page` and recording route contract tests |
| Retry-failed preview | Failed/paused task states | One joined scalar query; no full parts/chunks/danmaku hydration | `UploadTaskActionManager.retryable_failed_jobs` and task-action regressions |
| Room policy list | 1, 20, and 100 policies | One `fetchall`, zero `fetchone` calls | `test_policy_list_resolves_accounts_with_constant_query_budget` |
| Retention status | Live, deleted, highlight, and unknown-size parts | One aggregate database call, zero path calls | `test_status_aggregates_persisted_live_sizes_without_file_io` |
| Highlight marker counts | Linked and legacy markers across contiguous parts | At most two database calls, zero file calls | `test_marker_counts_use_persisted_links_and_legacy_boundaries_without_io` |
| Clip library | 20 and 100 rows after 500 off-page clips | Exactly one count plus one page-summary query; zero `getsize`, `stat`, or `lstat` calls | `test_clip_summary_page_uses_two_database_calls_and_no_file_io` |

The focused budget run completed with 12 tests passing. The combined backend
regression completed with 709 passing and 1 skipped; Black, isort, Flake8, and
mypy passed.

## Clip-size lifecycle

- Migration 24 adds nullable `highlight_clips.file_size_bytes`; `NULL` means an
  unmeasured legacy output, never a known zero-byte file.
- New and interrupted/recovered clips persist the exact generated artifact size.
- Retry, cancellation, deletion, failure, and other artifact-invalidating paths
  clear the persisted size.
- Worker startup performs one conditional legacy backfill with an effective
  maximum of 100 candidate paths. Missing or unreadable files remain `NULL`.
- The paged library has no request-time size fallback. Full clip detail still
  returns local paths and source ranges.

These properties are covered by `test_highlight_worker.py`,
`test_account_runtime.py`, the 20/100-row clip budget, and summary/detail route
parity tests.

## SQLite and frontend evidence

- SQLite 3.22 and the current development SQLite both select
  `recording_sessions_source_started_idx`, `upload_jobs_state_session_idx`, and
  `highlight_clips_library_idx`; result IDs and ordering are unchanged.
- Rapid filters/pages use one `switchMap` pipeline. Older HTTP responses cannot
  overwrite the latest criteria, and an error does not terminate retry handling.
- Recording rows are OnPush. A progress-only SSE update replaces only the
  matching row input; an identical update replaces none.
- Full Angular regression: 406 tests passed. Targeted changed-file ESLint passed.
  Full `ng lint` still reports exactly the five pre-existing errors outside the
  changed files.
- Production estimated transfer sizes: recording/upload list 37,199 B, highlight
  editor 17,473 B, clip library 4,642 B, and FLV runtime 53,826 B. All four are
  separate lazy chunks; the list and editor chunks contain neither the FLV
  factory nor `mpegts.js`.

## Deployment timing gate

Warm NAS p50/p95 has not yet been measured for this code revision. It must be
sampled after deployment from normalized request metrics, without query values,
paths, or account IDs. Required gates are:

| Route | Required warm NAS p95 |
| --- | ---: |
| `/api/v1/recording-sessions` (20 rows) | < 150 ms |
| `/api/v1/room-upload-policies` | < 150 ms |
| `/api/v1/recording-retention/status` | < 100 ms |
| `/api/v1/highlights/sessions/{session_id}/marker-counts` | < 100 ms |
| `/api/v1/highlights/clips` (20 and 100 rows) | < 150 ms |

Until those samples are recorded, the deterministic budgets are complete but the
NAS timing gate remains pending. No destructive endpoint or upstream Bilibili
request should be load-tested to obtain these numbers.
