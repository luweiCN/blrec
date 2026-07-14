# BLREC Built-in Bilibili Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add recoverable TV QR login, multi-part Bilibili upload, review tracking, SC/guard index comments, and native video-danmaku backfill directly to BLREC's Python/Docker application.

**Architecture:** A local SQLite journal becomes the durable source of truth between recorder events and external writes. Versioned encrypted account bundles feed a single protocol adapter whose endpoint/authentication combinations are allow-listed; leased workers upload and submit one job at a time, then run comment and danmaku branches independently after review. Every non-idempotent remote write records `prepared → in_flight → confirmed | unknown_outcome | failed_permanent`, and ambiguous writes are never retried blindly.

**Tech Stack:** Python 3.8, asyncio, aiohttp, sqlite3, cryptography AES-GCM, lxml iterparse, FastAPI/Pydantic, pytest/pytest-asyncio; Angular 15, TypeScript 4.9, Jasmine/Karma; one existing Docker image/container/process.

## Global Constraints

- Keep the implementation in this repository, in Python, in the existing Docker image; do not add Java, Rust, Redis, Celery, a sidecar, or an external database.
- The database defaults to `/cfg/blrec.sqlite3`, uses WAL and a single writer, and must reject NFS/SMB/shared-lock filesystems and multiple worker processes.
- Enabling any account or Bilibili write feature requires both `BLREC_API_KEY` and exactly one credential-key source: `BLREC_CREDENTIAL_KEY_FILE` or `BLREC_CREDENTIAL_KEY`. Missing/invalid keys leave recording available but all sensitive routes and writes fail closed.
- Optional `BLREC_CREDENTIAL_OLD_KEY_FILES` is a comma-separated `key_id=/absolute/path` read-only keyring used only to decrypt during rotation; encryption always uses the current key.
- Database, WAL, SHM, and key file permissions are `0600`; their parent directory is `0700`.
- Store all database timestamps as UTC epoch seconds; convert only at the API/UI boundary.
- Keep TV APP signing, Web Cookie/CSRF/WBI, APP device IDs, and Web `buvid3`/`buvid4`/`b_nut` separate. Never fall back to the old global browser Cookie for writes.
- Validate token `mid`, Cookie `DedeUserID`, queried account UID, and archive owner as one account before a write.
- Wait for `VideoPostprocessingCompletedEvent`/`PostprocessingCompletedEvent` or an explicit terminal postprocess failure before selecting `final_path`; `VideoFileCompletedEvent` is not upload-ready.
- Preserve part order assigned when `VideoFileCreatedEvent` arrives; never order parts by asynchronous completion.
- Do not implement AI. Automatic comments contain only SC and guard-purchase indexes.
- Queue every danmaku that passes explicit filters: no software daily cap and no per-part cap. A 500-row import/read batch is only a memory bound.
- Start at one danmaku per 25 seconds per account, allow only slower automatic adjustment during canary/rollout, and never add account pools, proxies, identity rotation, or faster catch-up.
- The uploader account is the visible sender for backfilled danmaku. Never imply that original viewers sent those video danmaku.
- `unknown_outcome` is terminal for automatic retry until remote reconciliation or an authenticated manual decision succeeds.
- Automatic upload, automatic comment, and danmaku backfill have independent emergency switches; disabling any of them must not interrupt recording.
- Support Python 3.8: no `asyncio.to_thread`, `TaskGroup`, `match`, or PEP 604 union syntax.
- Automated tests never perform a real Bilibili write. Real protocol canary is an explicit operator action after all fake-service tests pass.

---

## File Map

- Create `src/blrec/bili_upload/database.py` and `src/blrec/bili_upload/migrations/0001_initial.sql`: local database ownership, migrations, leases, and all upload tables.
- Create `src/blrec/bili_upload/models.py`: typed states and immutable DTOs shared by workers and routes.
- Create `src/blrec/bili_upload/crypto.py` and `credentials.py`: authenticated encryption and atomic versioned credential bundles.
- Create `src/blrec/bili_upload/protocol.py`, `signing.py`, and `errors.py`: allow-listed endpoint/auth matrix and sanitized transport errors.
- Create `src/blrec/bili_upload/accounts.py`: bounded TV QR sessions, credential validation, refresh, pause, and health checks.
- Create `src/blrec/bili_upload/journal.py`: durable recorder/postprocessor bridge and session/part reconciliation.
- Create `src/blrec/bili_upload/upload.py` and `upos.py`: job creation, file identity, resumable chunks, final submit, and unknown-result reconciliation.
- Create `src/blrec/bili_upload/review.py`: 15-minute review sync and CID binding.
- Create `src/blrec/bili_upload/comments.py`: deterministic SC/guard text, root/replies/pin, and reconciliation.
- Create `src/blrec/bili_upload/danmaku_import.py` and `danmaku_publish.py`: streaming XML import, filtering, fair queue, rate limits, and error-specific breakers.
- Create `src/blrec/bili_upload/service.py` and `workers.py`: lifecycle ordering, shared account write gates, watermarks, and emergency switches.
- Modify recorder/postprocessor/event files only where required to persist durable events before volatile notifications.
- Create `src/blrec/web/routers/bili_accounts.py`, `upload_policies.py`, and `uploads.py`: authenticated management API with redacted responses.
- Create `webapp/src/app/uploads/`: accounts, room policy, job list/detail, backlog, and manual reconciliation UI.
- Create `tests/bili_upload/`, `tests/web/`, and `tests/integration/`: fake protocol and crash-recovery coverage.

### Task 1: Add fail-closed configuration, database ownership, and schema

**Files:**
- Modify: `setup.cfg`
- Modify: `MANIFEST.in`
- Modify: `src/blrec/setting/models.py`
- Create: `src/blrec/bili_upload/__init__.py`
- Create: `src/blrec/bili_upload/models.py`
- Create: `src/blrec/bili_upload/database.py`
- Create: `src/blrec/bili_upload/migrations/0001_initial.sql`
- Create: `tests/bili_upload/test_database.py`
- Create: `tests/bili_upload/test_feature_gate.py`

**Interfaces:**
- Produces: `BiliUploadSettings`, `BiliUploadDatabase.open/close/read/write/claim`, `FeatureUnavailable`, `WriteState`, `JobState`, and schema version 1.
- Consumes: existing `EnvSettings.api_key` and `/cfg` volume.

- [ ] **Step 1: Add bounded dependencies and package migration data**

Add to `install_requires`:

```ini
    cryptography >= 41.0.0, < 44.0.0
```

Add to `dev`:

```ini
        pytest >= 7.4.4, < 8.0.0
        pytest-asyncio >= 0.21.2, < 0.22.0
```

Append to `MANIFEST.in`:

```text
graft src/blrec/bili_upload/migrations
```

- [ ] **Step 2: Write failing gate and migration tests**

```python
def test_write_features_fail_closed_without_both_keys(tmp_path: Path) -> None:
    settings = BiliUploadSettings(enabled=True, database_path=str(tmp_path / 'db.sqlite3'))

    with pytest.raises(FeatureUnavailable, match='BLREC_API_KEY'):
        validate_feature_gate(settings, api_key=None, credential_key=None)
    with pytest.raises(FeatureUnavailable, match='credential key'):
        validate_feature_gate(settings, api_key='12345678', credential_key=None)


@pytest.mark.asyncio
async def test_migration_enables_wal_and_constraints(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        assert await database.scalar('PRAGMA journal_mode') == 'wal'
        assert await database.scalar('PRAGMA foreign_keys') == 1
        tables = await database.table_names()
        assert REQUIRED_TABLES <= tables
        await database.execute(
            "INSERT INTO bili_accounts(id,uid,display_name,credential_ciphertext,credential_version,key_id,state,created_at,updated_at) "
            "VALUES(1,42,'u',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            "INSERT INTO recording_sessions(id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO upload_jobs(session_id,account_id,policy_snapshot_json,state,submit_state,created_at,updated_at) "
                "VALUES(1,1,'{}','invalid','prepared',1,1)"
            )
    finally:
        await database.close()
```

`REQUIRED_TABLES` is the exact set: `schema_migrations`, `event_journal`, `bili_accounts`, `qr_sessions`, `room_upload_policies`, `recording_sessions`, `recording_runs`, `upload_jobs`, `upload_parts`, `upload_chunks`, `comment_items`, `danmaku_items`, and `management_audit`. The seeded account/session ensure the assertion exercises the job-state `CHECK` rather than an unrelated foreign key.

- [ ] **Step 3: Run tests and confirm missing imports**

Run: `python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_feature_gate.py -v`

Expected: FAIL because `blrec.bili_upload` does not exist.

- [ ] **Step 4: Add exact settings and state types**

```python
class BiliUploadSettings(BaseModel):
    enabled: bool = False
    database_path: str = '/cfg/blrec.sqlite3'
    auto_upload_enabled: bool = False
    auto_comment_enabled: bool = False
    danmaku_backfill_enabled: bool = False
    upload_chunk_size: Annotated[int, Field(ge=1024 * 1024, le=32 * 1024 * 1024)] = 4 * 1024 * 1024
    upload_chunk_concurrency: Annotated[int, Field(ge=1, le=3)] = 2
    danmaku_interval_seconds: Annotated[int, Field(ge=25, le=3600)] = 25
    import_high_watermark: Annotated[int, Field(ge=10000)] = 1000000
```

Add `bili_upload` to `Settings` and `SettingsIn`. Add optional `credential_key`, `credential_key_file`, and `credential_old_key_files` env fields with aliases `BLREC_CREDENTIAL_KEY`, `BLREC_CREDENTIAL_KEY_FILE`, and `BLREC_CREDENTIAL_OLD_KEY_FILES`; validate that current inline/file sources are not both set. Parse old entries as unique `key_id=/absolute/path` pairs and reject a duplicate current key ID.

```python
class WriteState(str, Enum):
    PREPARED = 'prepared'
    IN_FLIGHT = 'in_flight'
    CONFIRMED = 'confirmed'
    UNKNOWN_OUTCOME = 'unknown_outcome'
    FAILED_PERMANENT = 'failed_permanent'


class JobState(str, Enum):
    WAITING_ARTIFACTS = 'waiting_artifacts'
    READY = 'ready'
    UPLOADING = 'uploading'
    SUBMITTING = 'submitting'
    WAITING_REVIEW = 'waiting_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    PAUSED = 'paused'
    COMPLETED = 'completed'
```

Implement the gate called by application startup and every sensitive router:

```python
def validate_feature_gate(
    settings: BiliUploadSettings,
    *,
    api_key: Optional[str],
    credential_key: Optional[bytes],
) -> None:
    if not settings.enabled:
        return
    if not api_key:
        raise FeatureUnavailable('BLREC_API_KEY is required')
    if credential_key is None:
        raise FeatureUnavailable('credential key is required')
    if len(credential_key) != 32:
        raise FeatureUnavailable('credential key must decode to 32 bytes')
```

`EnvSettings` rejects setting both key env variables. Key-file loading rejects symlinks, non-regular files, group/other permission bits, and contents that do not decode to 32 bytes; it never creates or rewrites a key file.

- [ ] **Step 5: Implement the schema with explicit constraints**

The migration must contain the complete table skeleton below and these exact claim indexes:

```sql
CREATE INDEX upload_jobs_claim_idx ON upload_jobs(state, next_attempt_at, priority, id);
CREATE INDEX comment_items_claim_idx ON comment_items(state, next_attempt_at, priority, id);
CREATE INDEX danmaku_items_claim_idx ON danmaku_items(state, next_attempt_at, priority, id);
```

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
CREATE TABLE bili_accounts (
    id INTEGER PRIMARY KEY,
    uid INTEGER NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    credential_ciphertext BLOB NOT NULL,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    key_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','paused','refresh_unknown','archived')),
    pause_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE qr_sessions (
    id TEXT PRIMARY KEY,
    manager_subject TEXT NOT NULL,
    auth_code_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('created','pending','scanned','confirmed','expired','cancelled','failed')),
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE room_upload_policies (
    room_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES bili_accounts(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    title_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    tid INTEGER NOT NULL CHECK (tid > 0),
    tags TEXT NOT NULL,
    copyright INTEGER NOT NULL CHECK (copyright IN (1,2)),
    source TEXT NOT NULL,
    auto_comment INTEGER NOT NULL CHECK (auto_comment IN (0,1)),
    danmaku_backfill INTEGER NOT NULL CHECK (danmaku_backfill IN (0,1)),
    filter_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE recording_sessions (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    broadcast_session_key TEXT NOT NULL UNIQUE,
    live_start_time INTEGER,
    state TEXT NOT NULL CHECK (state IN ('open','closed','cancelled','manual_review','skipped')),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE TABLE recording_runs (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES recording_sessions(id),
    state TEXT NOT NULL CHECK (state IN ('recording','finished','cancelled')),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE TABLE event_journal (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    room_id INTEGER NOT NULL,
    run_id TEXT REFERENCES recording_runs(id),
    path TEXT,
    payload_json TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    consumed_at INTEGER
);
CREATE TABLE upload_jobs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES recording_sessions(id),
    account_id INTEGER NOT NULL REFERENCES bili_accounts(id),
    policy_snapshot_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('waiting_artifacts','ready','uploading','submitting','waiting_review','approved','rejected','paused','completed')),
    submit_state TEXT NOT NULL CHECK (submit_state IN ('prepared','in_flight','confirmed','unknown_outcome','failed_permanent')),
    comment_branch_state TEXT NOT NULL DEFAULT 'disabled' CHECK (comment_branch_state IN ('disabled','pending','running','skipped_no_content','skipped_source_missing','completed','paused','failed')),
    danmaku_branch_state TEXT NOT NULL DEFAULT 'disabled' CHECK (danmaku_branch_state IN ('disabled','pending','importing','publishing','skipped_source_missing','completed','paused','failed')),
    aid INTEGER,
    bvid TEXT,
    review_reason TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE upload_parts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
    part_index INTEGER NOT NULL CHECK (part_index > 0),
    source_path TEXT NOT NULL,
    final_path TEXT,
    xml_path TEXT,
    file_identity TEXT,
    artifact_state TEXT NOT NULL CHECK (artifact_state IN ('recording','postprocessing','ready','failed','missing','manual_review')),
    upload_state TEXT NOT NULL DEFAULT 'prepared' CHECK (upload_state IN ('prepared','preupload','uploading','completing','confirmed','unknown_outcome','failed')),
    danmaku_import_state TEXT NOT NULL DEFAULT 'disabled' CHECK (danmaku_import_state IN ('disabled','pending','importing','waiting_capacity','missing_source','completed','failed')),
    remote_filename TEXT,
    cid INTEGER,
    upload_session_json TEXT,
    UNIQUE(job_id, part_index)
);
CREATE TABLE upload_chunks (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES upload_parts(id),
    chunk_no INTEGER NOT NULL CHECK (chunk_no >= 0),
    offset INTEGER NOT NULL CHECK (offset >= 0),
    size INTEGER NOT NULL CHECK (size > 0),
    etag TEXT,
    state TEXT NOT NULL CHECK (state IN ('prepared','in_flight','confirmed','failed')),
    attempt INTEGER NOT NULL DEFAULT 0,
    UNIQUE(part_id, chunk_no)
);
CREATE TABLE comment_items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('root','reply','pin')),
    parent_ordinal INTEGER,
    content TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    rpid INTEGER,
    state TEXT NOT NULL CHECK (state IN ('prepared','in_flight','confirmed','unknown_outcome','failed_permanent')),
    error_code INTEGER,
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, ordinal)
);
CREATE TABLE danmaku_items (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES upload_parts(id),
    xml_identity TEXT NOT NULL,
    original_index INTEGER NOT NULL CHECK (original_index >= 0),
    progress_ms INTEGER NOT NULL CHECK (progress_ms >= 0),
    mode INTEGER NOT NULL,
    fontsize INTEGER NOT NULL,
    color INTEGER NOT NULL,
    content TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    request_fingerprint TEXT NOT NULL,
    dmid INTEGER,
    state TEXT NOT NULL CHECK (state IN ('prepared','in_flight','confirmed','unknown_outcome','failed_permanent')),
    error_code INTEGER,
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(part_id, xml_identity, original_index)
);
CREATE TABLE management_audit (
    id INTEGER PRIMARY KEY,
    manager_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
```

- [ ] **Step 6: Implement serialized SQLite access and fencing**

`BiliUploadDatabase` owns one `sqlite3.Connection(check_same_thread=False, isolation_level=None)` and one `ThreadPoolExecutor(max_workers=1)`. Every public async method calls `loop.run_in_executor(self._executor, ...)`. `open()` creates parent directory `0700`, database `0600`, rejects Linux mount types `nfs`, `nfs4`, `cifs`, `smb3`, and `fuse.sshfs`, verifies an exclusive lock probe, sets `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and `busy_timeout=5000`, applies migrations in `BEGIN IMMEDIATE`, runs `PRAGMA quick_check`, then acquires a non-blocking local process lock. `claim()` uses a 120-second TTL and increments `lease_generation`; workers renew when 60 seconds or less remain. Every completion update includes `WHERE id=? AND lease_owner=? AND lease_generation=?` and requires `cursor.rowcount == 1`.

- [ ] **Step 7: Run database tests and commit**

Run: `python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_feature_gate.py -v`

Expected: PASS, including uniqueness, CHECK, WAL, permissions, and stale-generation rejection.

```bash
git add setup.cfg MANIFEST.in src/blrec/setting/models.py src/blrec/bili_upload tests/bili_upload/test_database.py tests/bili_upload/test_feature_gate.py
git commit -m "feat: add secure upload database foundation"
```

### Task 2: Encrypt and atomically version credential bundles

**Files:**
- Create: `src/blrec/bili_upload/crypto.py`
- Create: `src/blrec/bili_upload/credentials.py`
- Create: `tests/bili_upload/test_credentials.py`

**Interfaces:**
- Produces: `CredentialBundle`, `CookieRecord`, `CredentialCipher.encrypt/decrypt`, and `CredentialStore.put/get/rotate`.
- Consumes: `bili_accounts` and a 32-byte decoded master key.

- [ ] **Step 1: Write failing round-trip, wrong-key, and atomic-rotation tests**

```python
def test_bundle_round_trip_keeps_protocol_scopes() -> None:
    bundle = credential_fixture(mid=42)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')

    envelope = cipher.encrypt(bundle, account_uid=42, version=1)
    restored = cipher.decrypt(envelope, account_uid=42)

    assert restored == bundle
    assert restored.app_device_id != restored.web_buvid3


def test_wrong_key_never_overwrites_existing_ciphertext(store: CredentialStore) -> None:
    original = store.raw_ciphertext(account_id=1)
    with pytest.raises(InvalidCredentialKey):
        store.rotate(account_id=1, cipher=wrong_cipher())
    assert store.raw_ciphertext(account_id=1) == original
```

- [ ] **Step 2: Run and verify the tests fail**

Run: `python -m pytest tests/bili_upload/test_credentials.py -v`

Expected: FAIL because credential types do not exist.

- [ ] **Step 3: Implement the complete versioned model**

```python
@dataclass(frozen=True)
class CookieRecord:
    name: str
    value: str
    domain: str
    path: str
    expires_at: Optional[int]
    secure: bool
    http_only: bool


@dataclass(frozen=True)
class CredentialBundle:
    access_token: str
    refresh_token: str
    mid: int
    issued_at: int
    expires_at: int
    signing_family: str
    app_client_version: str
    web_client_version: str
    app_device_source: str
    web_device_source: str
    app_device_id: str
    app_buvid: str
    web_buvid3: str
    web_buvid4: str
    web_b_nut: str
    cookies: Tuple[CookieRecord, ...]

    @property
    def csrf(self) -> str:
        values = [cookie.value for cookie in self.cookies if cookie.name == 'bili_jct']
        if len(values) != 1:
            raise InvalidCredentialBundle('expected exactly one bili_jct cookie')
        return values[0]
```

Use a JSON encoding with sorted keys and no secret-bearing `repr`. The AES-GCM associated data is `b'blrec:bili-account:{uid}:v{version}'`; envelope fields are `format_version`, `key_id`, `credential_version`, `nonce`, and `ciphertext`. Decode the configured key from URL-safe base64 and require exactly 32 bytes.

- [ ] **Step 4: Implement transactional replacement and rotation**

`CredentialStore.put` first decrypts the newly produced envelope, validates `mid == account uid`, then updates ciphertext, key ID, credential version, and timestamp in one `BEGIN IMMEDIATE` transaction. `rotate` decrypts with current/old key maps, encrypts with the new current key, verifies the new ciphertext, then commits. Never log bundle fields or raw cryptography exceptions containing input.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest tests/bili_upload/test_credentials.py -v`

Expected: PASS for round trip, wrong key, old-key read/new-key write, corruption, and UID-associated-data mismatch.

```bash
git add src/blrec/bili_upload/crypto.py src/blrec/bili_upload/credentials.py tests/bili_upload/test_credentials.py
git commit -m "feat: encrypt versioned bilibili credentials"
```

### Task 3: Implement the allow-listed protocol and signing matrix

**Files:**
- Create: `src/blrec/bili_upload/errors.py`
- Create: `src/blrec/bili_upload/signing.py`
- Create: `src/blrec/bili_upload/protocol.py`
- Create: `docs/protocol-sources.md`
- Create: `tests/bili_upload/test_protocol_matrix.py`
- Create: `tests/bili_upload/fixtures/protocol/`

**Interfaces:**
- Produces: `BiliProtocolClient` methods `create_qr`, `poll_qr`, `oauth_info`, `refresh_token`, `preupload`, `upload_chunk`, `complete_upload`, `submit_archive`, `list_archives`, `list_replies`, `reply_detail`, `add_reply`, `top_reply`, and `post_danmaku`.
- Consumes: one immutable `CredentialBundle` version per operation.

- [ ] **Step 1: Write a parameterized failing matrix test**

```python
@pytest.mark.parametrize(
    ('operation', 'auth_mode', 'path'),
    [
        ('create_qr', 'bilitv_sign', '/x/passport-tv-login/qrcode/auth_code'),
        ('poll_qr', 'bilitv_sign', '/x/passport-tv-login/qrcode/poll'),
        ('oauth_info', 'bilitv_token_sign', '/x/passport-login/oauth2/info'),
        ('refresh_token', 'bilitv_token_sign', '/x/passport-login/oauth2/refresh_token'),
        ('preupload', 'web_cookie', '/preupload'),
        ('upload_chunk', 'upos_session', '<server-returned>'),
        ('complete_upload', 'upos_session', '<server-returned>'),
        ('submit_archive', 'bilitv_token_sign', '/x/vu/app/add'),
        ('list_archives', 'web_cookie', '/x/web/archives'),
        ('list_replies', 'web_cookie_wbi', '/x/v2/reply/main'),
        ('reply_detail', 'web_cookie_wbi', '/x/v2/reply/detail'),
        ('add_reply', 'web_cookie_csrf', '/x/v2/reply/add'),
        ('top_reply', 'web_cookie_csrf', '/x/v2/reply/top'),
        ('post_danmaku', 'web_cookie_csrf_wbi', '/x/v2/dm/post'),
    ],
)
def test_operation_has_one_auth_mode(operation: str, auth_mode: str, path: str) -> None:
    spec = PROTOCOL_MATRIX[operation]
    assert spec.auth_mode == auth_mode
    assert spec.path == path
```

Add transport assertions that TV operations never contain Web Cookie/CSRF, Web operations never contain `access_key` in query/form, UPOS chunk requests contain only server-provided `X-Upos-Auth` and `upload_id`, and sanitized errors omit query/form/Cookie/response bodies.

- [ ] **Step 2: Run and confirm missing protocol module**

Run: `python -m pytest tests/bili_upload/test_protocol_matrix.py -v`

Expected: FAIL because `PROTOCOL_MATRIX` is undefined.

- [ ] **Step 3: Implement immutable operation specs and separate signers**

```python
@dataclass(frozen=True)
class OperationSpec:
    method: str
    base: str
    path: str
    auth_mode: str
    idempotent: bool


PROTOCOL_MATRIX = {
    'create_qr': OperationSpec('POST', 'passport', '/x/passport-tv-login/qrcode/auth_code', 'bilitv_sign', True),
    'poll_qr': OperationSpec('POST', 'passport', '/x/passport-tv-login/qrcode/poll', 'bilitv_sign', True),
    'oauth_info': OperationSpec('GET', 'passport', '/x/passport-login/oauth2/info', 'bilitv_token_sign', True),
    'refresh_token': OperationSpec('POST', 'passport', '/x/passport-login/oauth2/refresh_token', 'bilitv_token_sign', False),
    'preupload': OperationSpec('GET', 'member', '/preupload', 'web_cookie', True),
    'upload_chunk': OperationSpec('PUT', 'server_returned', '<server-returned>', 'upos_session', True),
    'complete_upload': OperationSpec('POST', 'server_returned', '<server-returned>', 'upos_session', False),
    'submit_archive': OperationSpec('POST', 'member_api', '/x/vu/app/add', 'bilitv_token_sign', False),
    'list_archives': OperationSpec('GET', 'member_api', '/x/web/archives', 'web_cookie', True),
    'list_replies': OperationSpec('GET', 'api', '/x/v2/reply/main', 'web_cookie_wbi', True),
    'reply_detail': OperationSpec('GET', 'api', '/x/v2/reply/detail', 'web_cookie_wbi', True),
    'add_reply': OperationSpec('POST', 'api', '/x/v2/reply/add', 'web_cookie_csrf', False),
    'top_reply': OperationSpec('POST', 'api', '/x/v2/reply/top', 'web_cookie_csrf', False),
    'post_danmaku': OperationSpec('POST', 'api', '/x/v2/dm/post', 'web_cookie_csrf_wbi', False),
}
```

`BiliTvSigner` owns only the pinned BiliTV APPKEY/secret pair and canonical sorted query signing. Do not import or call BLREC's existing Android `AppApi.signed`. `WebSessionBuilder` reconstructs an aiohttp `CookieJar` from domain/path records and derives CSRF from the same bundle. `WbiSigner` is used only by operations whose matrix mode includes `_wbi` and refreshes public WBI keys through the existing read adapter.

`docs/protocol-sources.md` records the exact pinned commits from the design, which behavior/code was adapted, and the Apache-2.0/MIT notices. Copy no Java implementation wholesale; port only the tested protocol behavior and retain attribution for any translated code.

- [ ] **Step 4: Implement request execution with explicit send boundaries**

For each non-idempotent call, use aiohttp request tracing to set a local `headers_sent` flag. A connect/DNS failure before the trace fires raises `DefinitelyNotSent`; a timeout, disconnect, `5xx`, or unparsable response after it fires raises `RemoteOutcomeUnknown`. API business errors raise `BiliApiError(code, public_message)` with sanitized metadata. Do not put tenacity retry decorators on non-idempotent methods. Dynamic UPOS URLs must match the HTTPS host returned by the immediately preceding preupload response; reject redirects and never forward Web Cookie/TV token to that host.

- [ ] **Step 5: Validate pinned fixtures and commit**

Run: `python -m pytest tests/bili_upload/test_protocol_matrix.py -v`

Expected: PASS against the checked-in, redacted request/response fixtures for every operation.

```bash
git add src/blrec/bili_upload/errors.py src/blrec/bili_upload/signing.py src/blrec/bili_upload/protocol.py docs/protocol-sources.md tests/bili_upload/test_protocol_matrix.py tests/bili_upload/fixtures/protocol
git commit -m "feat: add bilibili protocol contract matrix"
```

### Task 4: Add bounded TV QR login, account validation, and refresh

**Files:**
- Create: `src/blrec/bili_upload/accounts.py`
- Create: `tests/bili_upload/test_accounts.py`

**Interfaces:**
- Consumes: protocol methods from Task 3 and credential store from Task 2.
- Produces: `AccountManager.create_qr/status/cancel`, `refresh_due_accounts`, `refresh_account`, and `AccountWriteGate.for_account(account_id)`.

- [ ] **Step 1: Write failing bounded-state tests**

```python
@pytest.mark.asyncio
async def test_one_poller_expires_after_180_seconds() -> None:
    clock = FakeClock()
    protocol = ScriptedQrProtocol(['pending', 'scanned', 'pending'])
    manager = AccountManager(protocol, store, clock=clock, qr_ttl_seconds=180)

    session = await manager.create_qr(manager_subject='admin')
    duplicate = await manager.status(session.id, manager_subject='admin')
    clock.advance(181)
    expired = await manager.status(session.id, manager_subject='admin')

    assert duplicate.poller_id == session.poller_id
    assert expired.state == 'expired'
    assert protocol.max_concurrent_pollers == 1


@pytest.mark.asyncio
async def test_confirm_rejects_mismatched_uid() -> None:
    protocol = confirmed_protocol(token_mid=42, cookie_uid=43, account_uid=42)
    manager = AccountManager(protocol, store)

    with pytest.raises(AccountIdentityMismatch):
        await manager.finish_confirmed_login(protocol.result)

    assert store.account_count() == 0
```

Add refresh tests for “less than 72 hours,” one daily health check, whole-bundle atomic replacement, `RemoteOutcomeUnknown → refresh_unknown`, and account gate serialization across refresh/submit/comment/danmaku.

- [ ] **Step 2: Run and verify the account tests fail**

Run: `python -m pytest tests/bili_upload/test_accounts.py -v`

Expected: FAIL because `AccountManager` does not exist.

- [ ] **Step 3: Implement the QR state machine and manager binding**

Use the exact transitions `created → pending → scanned → confirmed` with terminal `expired/cancelled/failed`. Store only a SHA-256 hash of auth code in `qr_sessions`; hold the raw code in the bounded in-memory poller and discard it at terminal state. `status` and `cancel` require the same authenticated `manager_subject` that created the session. Poll no longer than 180 seconds and cancel the task on application shutdown. On restart, mark persisted nonterminal QR rows `cancelled` because the raw auth code is deliberately unavailable; the operator starts a new QR session.

- [ ] **Step 4: Validate and save the indivisible bundle**

On confirmation, build one `CredentialBundle`; query OAuth info and Web nav/account identity, then require:

```python
if not (token_mid == cookie_dede_user_id == queried_uid == bundle.mid):
    raise AccountIdentityMismatch('token, cookie, and account uid differ')
```

Only after this check call `CredentialStore.put`. Persist APP and Web device fields with their original protocol scopes; never synthesize one from another.

- [ ] **Step 5: Implement refresh and unknown-result policy**

Run a health check at most once per UTC day and refresh when `expires_at - now < 72 * 3600`. Execute refresh under the per-account `asyncio.Lock`, permit one retry only for `DefinitelyNotSent`, validate the returned entire bundle, then replace atomically. On `RemoteOutcomeUnknown`, set account state `refresh_unknown`, pause all writes, retain old ciphertext, and require login/manual recovery.

`AccountWriteGate` owns one `asyncio.Lock` per account ID and exposes `async with gate.hold(expected_credential_version)`. After acquiring, it reloads account state/version and raises `AccountPaused` or `CredentialVersionChanged` before the caller marks a write `in_flight`. Refresh, UPOS complete/archive submit, comment/reply pin, and danmaku post all use this same gate registry.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_accounts.py tests/bili_upload/test_credentials.py tests/bili_upload/test_protocol_matrix.py -v`

Expected: PASS, with no parallel pollers or partial credential updates.

```bash
git add src/blrec/bili_upload/accounts.py tests/bili_upload/test_accounts.py
git commit -m "feat: add bounded bilibili account login and refresh"
```

### Task 5: Persist recorder events and assemble stable sessions/parts

**Files:**
- Create: `src/blrec/bili_upload/journal.py`
- Create: `tests/bili_upload/test_journal.py`
- Modify: `src/blrec/core/recorder.py`
- Modify: `src/blrec/postprocess/postprocessor.py`
- Modify: `src/blrec/task/task.py`
- Modify: `src/blrec/event/models.py`
- Modify: `src/blrec/event/event_submitters.py`

**Interfaces:**
- Produces: `RecordingJournalBridge.recording_started/finished/cancelled/video_created/video_completed/danmaku_completed/video_postprocessed/postprocessing_completed` and `reconcile_open_sessions()`.
- Consumes: database Task 1 and existing recorder/postprocessor callbacks.

- [ ] **Step 1: Write failing ordering and crash-recovery tests**

```python
@pytest.mark.asyncio
async def test_part_order_is_creation_order_not_completion_order(journal) -> None:
    run_id = await journal.recording_started(room_info(live_start_time=100))
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=101)
    await journal.video_created(run_id, '/rec/p2.flv', record_start_time=102)
    await journal.video_completed(run_id, '/rec/p2.flv')
    await journal.video_completed(run_id, '/rec/p1.flv')

    parts = await journal.parts_for_run(run_id)
    assert [(part.part_index, part.source_path) for part in parts] == [
        (1, '/rec/p1.flv'),
        (2, '/rec/p2.flv'),
    ]


@pytest.mark.asyncio
async def test_remux_path_becomes_final_only_after_postprocess(journal) -> None:
    run_id = await prepared_run_with_part(journal, '/rec/p1.flv')
    await journal.video_completed(run_id, '/rec/p1.flv')
    assert (await journal.parts_for_run(run_id))[0].final_path is None

    await journal.video_postprocessed(run_id, '/rec/p1.mp4', source_path='/rec/p1.flv')
    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path == '/rec/p1.mp4'
    assert part.artifact_state == 'ready'
```

The remaining journal tests end with these exact assertions:

```python
assert restarted_run.session_id == first_run.session_id
assert surrogate_key == reopened_surrogate_key
assert cancelled_session.state == 'cancelled'
assert cancelled_session.upload_job_id is None
assert replayed_event_count == 1
assert remuxed_part.source_exists is False
assert remuxed_part.final_path.endswith('.mp4')
assert orphan.artifact_state == 'manual_review'
assert upload_service.status().journal_degraded is True
recorder.stop.assert_not_awaited()
```

- [ ] **Step 2: Run and verify journal tests fail**

Run: `python -m pytest tests/bili_upload/test_journal.py -v`

Expected: FAIL because `RecordingJournalBridge` does not exist.

- [ ] **Step 3: Implement stable correlation and idempotent event writes**

`recording_started` uses `room_id:live_start_time` when the timestamp is positive; otherwise it reuses one open surrogate key per room or creates `room_id:local:<uuid4>`. It inserts a UUID `recording_run_id`. `video_created` allocates `MAX(part_index)+1` under `BEGIN IMMEDIATE` and stores record start time. Every method first inserts an event UUID into `event_journal`; `INSERT ... ON CONFLICT(id) DO NOTHING` makes replay harmless.

- [ ] **Step 4: Put durable calls before volatile event emission**

Add an optional bridge to `Recorder` and `Postprocessor`. In each existing callback, await the bridge before `_emit`; for example:

```python
async def on_video_file_created(self, path: str, record_start_time: int) -> None:
    if self._journal is not None:
        try:
            await self._journal.video_created(
                self._recording_run_id, path, record_start_time
            )
        except JournalUnavailable as exc:
            self._journal.pause_automation(exc)
            submit_exception(exc)
    await self._emit('video_file_created', self, path)
```

`Recorder._prepare` stores the run ID returned by `recording_started`; normal cleanup calls `recording_finished`, forced/interrupted cleanup calls `recording_cancelled`. Postprocessor completion passes both input/source and final paths so remux deletion is unambiguous. A journal failure marks upload automation degraded and raises an operator alert, but the recorder callback continues; the resulting files require startup/manual reconciliation and cannot be auto-uploaded by assumption. Keep the current RxPY events for WebSocket/Webhook compatibility.

- [ ] **Step 5: Reconcile only provable relationships**

At startup, replay unconsumed journal rows, bind paths by run and exact source/final relationship, and mark the session closed only when a run finished normally and every created part is `ready` or terminal `failed`. A cancelled session remains `cancelled` until an authenticated `finish`, `merge`, or `skip` decision. Never merge by filename timestamp alone.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_journal.py -v`

Expected: PASS for event ordering, restart, cancellation, remux, and duplicate replay.

```bash
git add src/blrec/bili_upload/journal.py src/blrec/core/recorder.py src/blrec/postprocess/postprocessor.py src/blrec/task/task.py src/blrec/event/models.py src/blrec/event/event_submitters.py tests/bili_upload/test_journal.py
git commit -m "feat: persist recording sessions and final artifacts"
```

### Task 6: Create recoverable multi-part jobs and UPOS uploads

**Files:**
- Create: `src/blrec/bili_upload/upos.py`
- Create: `src/blrec/bili_upload/upload.py`
- Create: `tests/bili_upload/test_upos.py`
- Create: `tests/bili_upload/test_upload.py`

**Interfaces:**
- Consumes: ready sessions/parts, policy snapshots, account gate, protocol preupload/chunk/complete/submit calls, and database leases.
- Produces: `FileIdentity`, `UposUploader.upload_part`, `UploadCoordinator.create_ready_jobs/run_once/reconcile_submission`, AID/BVID, and `unknown_outcome` handling.

- [ ] **Step 1: Write failing file-identity and resumable-chunk tests**

```python
def test_file_identity_detects_replaced_final_file(tmp_path: Path) -> None:
    path = tmp_path / 'part.mp4'
    path.write_bytes(b'a' * (2 * 1024 * 1024))
    first = FileIdentity.from_path(str(path))
    path.write_bytes(b'b' * (2 * 1024 * 1024))
    second = FileIdentity.from_path(str(path))

    assert first != second


@pytest.mark.asyncio
async def test_restart_skips_confirmed_chunks(database, fake_protocol, video_file) -> None:
    part_id = await prepared_part(database, video_file, chunk_size=4)
    await database.confirm_chunk(part_id, chunk_no=0, etag='etag-0')
    uploader = UposUploader(database, fake_protocol, chunk_size=4, concurrency=2)

    await uploader.upload_part(part_id)

    assert [call.chunk_no for call in fake_protocol.chunk_calls] == [1, 2]
```

The additional UPOS tests use these exact assertions:

```python
assert protocol.preupload_calls_by_part == {part_1: 1, part_2: 2}
assert (await database.get_part(part_2)).upload_state == 'unknown_outcome'
assert protocol.complete_calls_by_part[part_2] == 1
assert late_worker_update.rowcount == 0
assert len({claim.id for claim in simultaneous_claims if claim is not None}) == 1
assert (await database.get_job(job_id)).state == 'paused'
assert protocol.chunk_calls == []  # changed file identity stops before upload
```

- [ ] **Step 2: Write failing ambiguous-submit tests**

```python
@pytest.mark.asyncio
async def test_lost_submit_response_is_not_retried(database, protocol) -> None:
    job_id = await ready_uploaded_job(database)
    protocol.submit_error = RemoteOutcomeUnknown('connection closed after send')
    coordinator = UploadCoordinator(database, protocol, account_gates)

    await coordinator.run_once()
    await coordinator.run_once()

    job = await database.get_job(job_id)
    assert job.state == 'paused'
    assert job.submit_state == 'unknown_outcome'
    assert protocol.submit_calls == 1
```

- [ ] **Step 3: Run and confirm both modules are missing**

Run: `python -m pytest tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py -v`

Expected: FAIL because `FileIdentity`, `UposUploader`, and `UploadCoordinator` do not exist.

- [ ] **Step 4: Implement bounded file identity and streaming chunks**

```python
@dataclass(frozen=True)
class FileIdentity:
    canonical_path: str
    size: int
    mtime_ns: int
    head_digest: str
    tail_digest: str

    @classmethod
    def from_path(cls, path: str, sample_size: int = 1024 * 1024) -> 'FileIdentity':
        canonical = os.path.realpath(path)
        stat = os.stat(canonical)
        with open(canonical, 'rb') as file:
            head = file.read(sample_size)
            file.seek(max(0, stat.st_size - sample_size))
            tail = file.read(sample_size)
        return cls(
            canonical_path=canonical,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            head_digest=hashlib.blake2b(head, digest_size=16).hexdigest(),
            tail_digest=hashlib.blake2b(tail, digest_size=16).hexdigest(),
        )
```

Read the video with `open(..., 'rb')`, `seek(offset)`, and `read(size)` in the bounded executor. Never load a full recording into memory. Persist preupload response, UPOS URL, `X-Upos-Auth`, `upload_id`, each chunk offset/size/ETag, and completion result. Chunk PUT retries are allowed only when the server contract identifies the chunk by `(upload_id, chunk_no)`; every retry rechecks file identity and lease generation. Mark UPOS complete `in_flight` before the request; `RemoteOutcomeUnknown` becomes `upload_parts.upload_state='unknown_outcome'` and pauses for session reconciliation/manual restart instead of repeating completion automatically.

- [ ] **Step 5: Create a job only from a complete policy snapshot**

`create_ready_jobs` selects sessions that are normally closed and whose every part is `ready`. It validates: account active; final files stable for 30 seconds; comment enabled implies open comment setting and SC/guard recording; backfill enabled implies open video danmaku; title/description/tid/tags/copyright/source are concrete. Insert one job via `UNIQUE(session_id)` and copy the full policy JSON. Do not move or delete source/XML files.

- [ ] **Step 6: Implement globally serialized upload and final submit**

Claim at most one `READY/UPLOADING/SUBMITTING` job globally. Upload parts in `part_index` order; chunk concurrency stays within setting 1–3. Under `AccountWriteGate`, mark submit `in_flight` before calling `submit_archive` with all remote filenames in part order. On `DefinitelyNotSent`, return to `prepared` with bounded exponential retry; on `RemoteOutcomeUnknown`, persist `unknown_outcome`, pause, and do not auto-claim again.

`reconcile_submission` calls the read-only recent archive list and matches account owner, job fingerprint, exact remote filename set, part count, and part order. One match confirms AID/BVID; zero or multiple matches remains manual.

- [ ] **Step 7: Run and commit**

Run: `python -m pytest tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py -v`

Expected: PASS for streaming, resume, session renewal, fencing, part order, and lost-response behavior.

```bash
git add src/blrec/bili_upload/upos.py src/blrec/bili_upload/upload.py tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py
git commit -m "feat: add recoverable multi-part uploads"
```

### Task 7: Synchronize review state and bind CIDs safely

**Files:**
- Create: `src/blrec/bili_upload/review.py`
- Create: `tests/bili_upload/test_review.py`

**Interfaces:**
- Consumes: waiting-review jobs, `list_archives`, one approved-archive `archive_view`, policy snapshot, account UID, remote filenames, and part order.
- Produces: `PostReviewBranch.create(job_id)`, `ReviewWatcher(database, protocol, account_uid, comment_branch, danmaku_branch)`, `run_once`, approved/rejected job state, verified per-part CID, and independent branch creation.

- [ ] **Step 1: Write failing owner/order/rejection tests**

```python
@pytest.mark.asyncio
async def test_review_binds_cids_by_remote_filename_not_array_position(database) -> None:
    job_id = await waiting_review_job(database, filenames=['p1', 'p2'])
    protocol = archive_protocol(
        owner_uid=42,
        pages=[{'filename': 'p2', 'cid': 202}, {'filename': 'p1', 'cid': 101}],
    )
    watcher = ReviewWatcher(
        database, protocol, account_uid=42,
        comment_branch=AsyncMock(), danmaku_branch=AsyncMock()
    )

    await watcher.run_once()

    assert await database.part_cids(job_id) == {1: 101, 2: 202}


@pytest.mark.asyncio
async def test_owner_mismatch_pauses_without_creating_children(database) -> None:
    job_id = await waiting_review_job(database, filenames=['p1'])
    watcher = ReviewWatcher(
        database, archive_protocol(owner_uid=99), account_uid=42,
        comment_branch=AsyncMock(), danmaku_branch=AsyncMock()
    )

    await watcher.run_once()

    assert (await database.get_job(job_id)).state == 'paused'
    assert await database.child_item_count(job_id) == 0
```

Add exact cases for missing/extra filename, duplicate filename, reordered pages, rejected archive reason, still-reviewing archive, and approved job with comment failure not suppressing danmaku items.

- [ ] **Step 2: Run and confirm the watcher is missing**

Run: `python -m pytest tests/bili_upload/test_review.py -v`

Expected: FAIL because `ReviewWatcher` does not exist.

- [ ] **Step 3: Implement 15-minute grouped reads and strict matching**

Group waiting jobs by account and call `list_archives` no more than once per account per 900 seconds. Match first by known AID/BVID or job fingerprint. Because the list response may return `Videos: null` and `mid: 0`, use the already UID-bound account Cookie as the owner scope and, only after approval, call `archive_view` once for that job. Require an exact one-to-one mapping between local `remote_filename` and detail pages. Never bind CID by list index alone. Missing, duplicate, or extra pages pause with a public mismatch reason.

- [ ] **Step 4: Create post-review branches independently**

Define `PostReviewBranch` as a `typing_extensions.Protocol` with `async def create(job_id: int) -> None`. On rejection, store the upstream public reason and create no children. On approval, transactionally save AID/BVID/CIDs and mark `APPROVED`; invoke the two injected branches independently. Tasks 8 and 9 provide the concrete comment and danmaku implementations. A failure in one records that branch's error but leaves the other runnable. The account gate gives comment the next write opportunity without making it a dependency for backfill.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest tests/bili_upload/test_review.py -v`

Expected: PASS for owner, filename, order, rejection, and branch independence.

```bash
git add src/blrec/bili_upload/review.py tests/bili_upload/test_review.py
git commit -m "feat: verify archive review and part cids"
```

### Task 8: Generate and publish SC/guard index comments

**Files:**
- Create: `src/blrec/bili_upload/comments.py`
- Create: `tests/bili_upload/test_comments.py`

**Interfaces:**
- Consumes: XML paths, verified AID/BVID, account Web Cookie/CSRF, and account write gate.
- Produces: `CommentPlanner.create(job_id)` implementing `PostReviewBranch`, deterministic comment segments, root/reply/pin items, remote reconciliation, and permanent/manual states.

- [ ] **Step 1: Write failing deterministic rendering tests**

```python
def test_comment_contains_only_sc_and_guard_sorted_by_part_and_time(fixtures_dir: Path) -> None:
    planner = CommentPlanner(max_chars=1000)
    segments = planner.render(
        [part_xml(2, 'second.xml'), part_xml(1, 'first.xml')]
    )

    assert segments[0].startswith('SC 和上舰列表\n')
    assert segments[0].index('1#00:03:05') < segments[0].index('2#00:12:34')
    assert '普通弹幕' not in ''.join(segments)
    assert all(len(segment) <= 1000 for segment in segments)


def test_no_sc_or_guard_returns_explicit_skip() -> None:
    assert CommentPlanner().render([ordinary_only_xml()]) == []
```

The split test uses these exact assertions:

```python
items = planner.create_items(records)
assert items[0].ordinal == 0 and items[0].kind == 'root'
assert all(item.kind == 'reply' for item in items[1:-1])
assert items[-1].kind == 'pin'
assert all(len(item.content) <= 1000 for item in items[:-1])
assert long_but_fitting_line in items[2].content
assert abbreviated_line.endswith('……（内容过长已截断）')
```

- [ ] **Step 2: Write failing unknown-result and pin tests**

```python
@pytest.mark.asyncio
async def test_reply_timeout_reconciles_before_any_retry(database, protocol) -> None:
    item_id = await prepared_root_comment(database, content='SC 和上舰列表\n...')
    protocol.add_reply_error = RemoteOutcomeUnknown('lost response')
    publisher = CommentPublisher(database, protocol, gates)

    await publisher.run_once()
    await publisher.run_once()

    assert (await database.get_comment(item_id)).state == 'unknown_outcome'
    assert protocol.add_reply_calls == 1


@pytest.mark.asyncio
async def test_pin_failure_does_not_repost_root(database, protocol) -> None:
    await confirmed_root_pending_pin(database, rpid=123)
    protocol.top_reply_error = BiliApiError(code=12015, public_message='challenge')
    await CommentPublisher(database, protocol, gates).run_once()
    assert protocol.add_reply_calls == 0
```

- [ ] **Step 3: Run and confirm comment module is missing**

Run: `python -m pytest tests/bili_upload/test_comments.py -v`

Expected: FAIL because `CommentPlanner` and `CommentPublisher` do not exist.

- [ ] **Step 4: Implement streaming SC/guard extraction and item creation**

Use `lxml.etree.iterparse(path, events=('end',), tag=('sc', 'guard'))`, clear each element, normalize control characters, and sort records by `(part_index, ts, source_index)`. Render exact line shapes:

```python
def render_sc(part: int, timestamp: str, user: str, price: int, text: str) -> str:
    return '{}#{}  {}发送了{}元留言：{}'.format(part, timestamp, user, price, text)


def render_guard(part: int, timestamp: str, user: str, giftname: str) -> str:
    return '{}#{}  {}开通了{}'.format(part, timestamp, user, giftname)
```

Create ordinal 0 as kind `root` and ordinals 1..N as kind `reply` with `parent_ordinal=0`. After all text items confirm, create one kind `pin` item at ordinal N+1 pointing at root; its content stores the root fingerprint, not credentials. If XML exists but has no SC/guard records, persist `skipped_no_content`; if required XML is absent, persist `skipped_source_missing`. Neither state makes a remote request or blocks the separate danmaku branch. Request fingerprint is SHA-256 over operation kind, account UID, AID, parent ordinal, and full UTF-8 content.

- [ ] **Step 5: Implement publish, reconciliation, and error mapping**

Mark `in_flight` before `add_reply`; success saves RPID. Unknown response queries recent comments and matches owner, AID, parent RPID, and exact full content; exactly one match confirms, otherwise stays manual. After all segments confirm, attempt pin once for root. Code `12015`/challenge pauses the comment branch; comments disabled or permission denied become permanent; pin failure never recreates root/replies.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_comments.py -v`

Expected: PASS for formatting, splitting, no-content, root/replies, pin, challenge, and unknown reconciliation.

```bash
git add src/blrec/bili_upload/comments.py tests/bili_upload/test_comments.py
git commit -m "feat: publish sc and guard index comments"
```

### Task 9: Preserve filter metadata and import every eligible danmaku

**Files:**
- Modify: `src/blrec/core/danmaku_receiver.py`
- Modify: `src/blrec/core/danmaku_dumper.py`
- Modify: `src/blrec/core/models.py`
- Modify: `src/blrec/danmaku/models.py`
- Modify: `src/blrec/danmaku/io.py`
- Create: `src/blrec/bili_upload/danmaku_import.py`
- Create: `tests/bili_upload/test_danmaku_xml.py`
- Create: `tests/bili_upload/test_danmaku_import.py`

**Interfaces:**
- Produces: backward-compatible optional XML attributes, `DanmakuFilter`, `DanmakuImporter.create(job_id)` implementing `PostReviewBranch`, `import_part`, 500-row transactions, source-event identity, and priority rows.
- Consumes: approved part CID, policy filters, XML final path, database high watermark, and existing `SpaceSettings.space_threshold`.

- [ ] **Step 1: Write failing backward-compatibility and no-cap tests**

```python
def test_old_xml_without_optional_attributes_is_kept(old_xml: Path) -> None:
    rows = list(DanmakuImporter.parse(old_xml, DanmakuFilter()))
    assert len(rows) == 3
    assert rows[0].user_level is None
    assert rows[0].fan_medal_level is None


@pytest.mark.asyncio
async def test_import_has_no_daily_or_per_part_cap(database, large_xml: Path) -> None:
    importer = DanmakuImporter(database, insert_batch_size=500)

    imported = await importer.import_part(part_id=1, xml_path=str(large_xml))

    assert imported == 1201
    assert await database.danmaku_count(part_id=1) == 1201
    assert database.max_insert_batch == 500
```

The filter/watermark tests use these exact assertions:

```python
assert imported_texts == ['same text', 'same text']  # different source indexes survive
assert 'lottery' not in imported_texts
assert 'system' not in imported_texts
assert 'blocked phrase' not in imported_texts
assert imported_source_ids.count('event-1') == 1
assert DanmakuFilter().minimum_user_level is None
assert DanmakuFilter().minimum_fan_medal_level is None
assert (await database.get_part(part_id)).danmaku_import_state == 'waiting_capacity'
assert xml_path.exists()
recorder.stop.assert_not_awaited()
```

- [ ] **Step 2: Run and confirm optional fields/importer are missing**

Run: `python -m pytest tests/bili_upload/test_danmaku_xml.py tests/bili_upload/test_danmaku_import.py -v`

Expected: FAIL before the new optional fields and importer exist.

- [ ] **Step 3: Extend models and XML without breaking old readers**

Add optional `source_event_id`, `is_system`, `is_lottery`, `user_level`, `fan_medal_name`, and `fan_medal_level` fields with `None` defaults to `Danmu` and the parsed `DanmuMsg` boundary. In `_serialize_danmu`, append only non-`None` values as named XML attributes; keep the existing `p`, `uid`, and `user` fields unchanged. `DanmakuReader` reads missing attributes as `None`. Add serializer/reader round-trip tests for both old and new XML.

- [ ] **Step 4: Stream parse and batch insert all eligible records**

`DanmakuImporter` uses synchronous `iterparse` in the bounded executor and yields normalized records one at a time. Every 500 accepted records, execute one database transaction. The unique identity is `(part_id, xml_identity, original_index)`; `xml_identity` is the canonical path, size, mtime, and head/tail digest. Do not deduplicate by text. Ordinary records keep progress, mode, size, and color; SC/guard become top-mode text with username/amount/level and priority 100, not fake paid-SC objects.

- [ ] **Step 5: Apply only explicit filters and watermarks**

Default filters remove `is_lottery is True`, `is_system is True`, configured blacklist matches, and repeated nonempty `source_event_id`. Missing optional attributes are not treated as a match. User-level and fan-medal filters are disabled unless policy JSON explicitly enables thresholds. Missing XML sets part `missing_source` and job branch `skipped_source_missing` without affecting upload/comment. At `import_high_watermark` or when free space on the database/XML filesystem is at or below existing `SpaceSettings.space_threshold`, leave the part in `waiting_capacity`, retain XML, and return; recorder code and event journal have no dependency on importer capacity.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_danmaku_xml.py tests/bili_upload/test_danmaku_import.py -v`

Expected: PASS with 1,201 rows imported, maximum batch 500, and no software quantity trimming.

```bash
git add src/blrec/core/danmaku_receiver.py src/blrec/core/danmaku_dumper.py src/blrec/core/models.py src/blrec/danmaku/models.py src/blrec/danmaku/io.py src/blrec/bili_upload/danmaku_import.py tests/bili_upload/test_danmaku_xml.py tests/bili_upload/test_danmaku_import.py
git commit -m "feat: queue all eligible recorded danmaku"
```

### Task 10: Publish danmaku fairly with conservative account breakers

**Files:**
- Create: `src/blrec/bili_upload/danmaku_publish.py`
- Create: `tests/bili_upload/test_danmaku_publish.py`

**Interfaces:**
- Consumes: prepared danmaku rows, verified CID, account bundle/gate, protocol `post_danmaku`, 25-second minimum interval, and emergency switch.
- Produces: fair account/job scheduling, backlog metrics, `36703/36704/36715` handling, and manual unknown-outcome decisions.

- [ ] **Step 1: Write failing fairness, rate, and no-cap tests**

```python
@pytest.mark.asyncio
async def test_jobs_round_robin_after_priority_items(database, fake_clock) -> None:
    await seed_danmaku(database, job_id=1, priorities=[100, 0, 0])
    await seed_danmaku(database, job_id=2, priorities=[100, 0])
    publisher = DanmakuPublisher(database, protocol, gates, clock=fake_clock)

    for _ in range(5):
        await publisher.run_once()
        fake_clock.advance(25)

    assert protocol.sent_job_ids == [1, 2, 1, 2, 1]


@pytest.mark.asyncio
async def test_interval_never_automatically_goes_below_25_seconds(database, fake_clock) -> None:
    publisher = DanmakuPublisher(database, protocol, gates, interval_seconds=25, clock=fake_clock)
    await publisher.run_once()
    fake_clock.advance(24.9)
    await publisher.run_once()
    assert protocol.post_calls == 1
```

The volume/error tests use these exact assertions:

```python
assert await database.schedulable_count(part_id) == 10000
assert breaker.delay_after(36703) >= 25
assert await database.part_cid_is_stale(part_id) is True  # 36704
assert breaker.next_probe_at >= fake_clock.now + 24 * 3600  # 36715
assert accounts.refresh_requested is True
assert permanent_item.state == 'failed_permanent'
assert lost_response_item.state == 'unknown_outcome'
assert protocol.calls_for(lost_response_item.id) == 1
```

- [ ] **Step 2: Run and confirm publisher is missing**

Run: `python -m pytest tests/bili_upload/test_danmaku_publish.py -v`

Expected: FAIL because `DanmakuPublisher` does not exist.

- [ ] **Step 3: Implement one-account pacing and fair selection**

Maintain `next_send_at` per account. Select the next account whose gate and breaker are open, then round-robin among that account's jobs; within a job sort by priority descending, original progress, and ID. Mark one row `in_flight` under a fenced lease, then call `post_danmaku` under `AccountWriteGate`. The configured interval may be increased by breaker logic or the operator but code clamps it with `max(25, configured_interval)`.

- [ ] **Step 4: Implement exact remote-result rules**

```python
if error.code == 36703:
    breaker.pause_bucket(min(previous_delay * 2, 24 * 3600))
elif error.code == 36704:
    database.mark_part_cid_stale(item.part_id)
    database.reschedule_without_attempt(item.id)
elif error.code == 36715:
    breaker.pause_until(clock() + 24 * 3600, probe_once=True)
elif error.is_token_expired:
    accounts.pause_writes_and_refresh(item.account_id)
elif error.is_permanent:
    database.fail_permanent(item.id, error.code, error.public_message)
```

`RemoteOutcomeUnknown` stores `unknown_outcome` and never auto-requeues because there is no reliable per-item remote reconciliation. Manual actions are exactly `assume_success` or `retry_accept_duplicate_risk`, require API authentication, record actor/time/reason, and make risk explicit in the UI.

Three consecutive `36703` responses across distinct items without an intervening success escalate from the danmaku bucket to an account write pause. A confirmed success resets that consecutive counter. Escalation never changes account, proxy, token family, or device identity.

- [ ] **Step 5: Expose backlog rates without changing limits**

For each account calculate accepted/imported per hour, confirmed per hour, net backlog delta, oldest age, and ETA `backlog / confirmed_rate` when rate is positive. A growing backlog is an operational rollout blocker; it does not authorize dropping rows, sending faster, or switching accounts.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_danmaku_publish.py -v`

Expected: PASS with ≥25-second spacing, round-robin jobs, correct error states, and all 10,000 rows remaining schedulable.

```bash
git add src/blrec/bili_upload/danmaku_publish.py tests/bili_upload/test_danmaku_publish.py
git commit -m "feat: backfill danmaku with fair conservative pacing"
```

### Task 11: Orchestrate workers without coupling them to recording

**Files:**
- Create: `src/blrec/bili_upload/workers.py`
- Create: `src/blrec/bili_upload/service.py`
- Create: `tests/bili_upload/test_service_lifecycle.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/main.py`

**Interfaces:**
- Consumes: database, journal, account manager, upload/review/comment/danmaku components, and settings emergency switches.
- Produces: `BiliUploadService(settings, database, journal, accounts, supervisor, settings_manager)`, `start/stop/status/set_emergency_switch`, ordered application startup/shutdown, and bounded worker loops.

- [ ] **Step 1: Write failing startup/shutdown ordering tests**

```python
@pytest.mark.asyncio
async def test_upload_service_starts_before_record_tasks_and_stops_after_them() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._bili_upload_service = OrderedUploadService(calls)
    app._task_manager = OrderedTaskManager(calls)

    await app._start_runtime()
    await app._stop_runtime(force=False)

    assert calls == [
        'database.open',
        'journal.start',
        'reconcile.run',
        'workers.start',
        'tasks.load',
        'workers.stop_claiming',
        'tasks.stop',
        'tasks.destroy',
        'workers.checkpoint_stop',
        'database.checkpoint',
        'database.close',
    ]


@pytest.mark.asyncio
async def test_all_write_switches_off_leave_recording_running() -> None:
    recorder = AsyncMock()
    service = BiliUploadService(
        BiliUploadSettings(enabled=True),
        database=AsyncMock(),
        journal=AsyncMock(),
        accounts=AsyncMock(),
        supervisor=AsyncMock(),
        settings_manager=AsyncMock(),
    )
    await service.set_emergency_switch('upload', False)
    await service.set_emergency_switch('comment', False)
    await service.set_emergency_switch('danmaku', False)
    recorder.stop.assert_not_awaited()
    assert service.status().accepting_recording_events is True


@pytest.mark.asyncio
async def test_missing_feature_keys_do_not_abort_task_loading() -> None:
    app = object.__new__(Application)
    app._bili_upload_service = AsyncMock()
    app._bili_upload_service.start.side_effect = FeatureUnavailable(
        'BLREC_API_KEY is required'
    )
    app._task_manager = AsyncMock()
    await app._start_runtime()
    assert app.get_bili_upload_status().available is False
    app._task_manager.load_all_tasks.assert_awaited_once()
```

Define the exact lifecycle fakes in the same test file:

```python
class OrderedUploadService:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def start(self) -> None:
        self._calls.extend([
            'database.open', 'journal.start', 'reconcile.run', 'workers.start'
        ])

    async def stop_claiming(self) -> None:
        self._calls.append('workers.stop_claiming')

    async def stop(self) -> None:
        self._calls.extend([
            'workers.checkpoint_stop', 'database.checkpoint', 'database.close'
        ])


class OrderedTaskManager:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def load_all_tasks(self) -> None:
        self._calls.append('tasks.load')

    async def stop_all_tasks(self, force: bool = False) -> None:
        self._calls.append('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')
```

- [ ] **Step 2: Run and confirm the service is missing**

Run: `python -m pytest tests/bili_upload/test_service_lifecycle.py -v`

Expected: FAIL because `BiliUploadService` does not exist.

- [ ] **Step 3: Implement explicit worker ownership**

`WorkerSupervisor` creates named asyncio tasks for account health, journal reconciliation, job creation, one global upload worker, review polling, comment publishing, XML import, and danmaku publishing. Each loop accepts a stop event and has a bounded wakeup interval. Synchronous SQLite, XML, crypto, and file identity work goes through the single DB actor or a bounded `ThreadPoolExecutor`; no loop calls blocking work on the event loop.

- [ ] **Step 4: Implement exact lifecycle sequencing**

Add testable `Application._start_runtime()` and `_stop_runtime(force)` methods. When the feature gate is satisfied, `_start_runtime` executes database open/permissions/migration/integrity/process lock, journal bridge creation, open-session/unknown-result reconciliation, worker start, then task loading. `FeatureUnavailable` is caught, exposed in upload status, and task loading still runs with no journal/workers. On normal exit/restart, `_stop_runtime` stops management mutations and new worker claims; keeps journal/DB open; stops/destroys record tasks and awaits postprocessor callbacks; stops workers at a chunk/item checkpoint; flushes and runs `PRAGMA wal_checkpoint(TRUNCATE)`; closes DB and lock. Forced termination recovery relies on journal/chunk leases, never on treating expired `in_flight` writes as prepared.

- [ ] **Step 5: Make all three switches independent**

Switches gate only new claim functions:

```python
def can_claim(self, kind: str) -> bool:
    return {
        'upload': self._settings.auto_upload_enabled,
        'comment': self._settings.auto_comment_enabled,
        'danmaku': self._settings.danmaku_backfill_enabled,
    }[kind]
```

Journal persistence, review reads for existing jobs, manual reconciliation, and recording remain active. Switch changes are persisted through the existing settings manager and audited in the database.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/bili_upload/test_service_lifecycle.py tests/bili_upload -v`

Expected: PASS with exact lifecycle order and no recorder stop from feature switches/errors.

```bash
git add src/blrec/bili_upload/workers.py src/blrec/bili_upload/service.py src/blrec/application.py src/blrec/web/main.py tests/bili_upload/test_service_lifecycle.py
git commit -m "feat: orchestrate upload workers safely"
```

### Task 12: Add authenticated, redacted management APIs

**Files:**
- Modify: `src/blrec/web/security.py`
- Create: `src/blrec/web/routers/bili_accounts.py`
- Create: `src/blrec/web/routers/upload_policies.py`
- Create: `src/blrec/web/routers/uploads.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/web/test_bili_accounts.py`
- Create: `tests/web/test_upload_routes.py`

**Interfaces:**
- Consumes: `BiliUploadService` management methods and existing `X-API-KEY` authentication.
- Produces: `ManagementPrincipal.subject`, account/QR, room-policy, job/detail/backlog, switches, reconciliation, and manual-decision endpoints; no credential fields.

- [ ] **Step 1: Write failing fail-closed and redaction tests**

```python
def test_sensitive_routes_fail_closed_when_server_has_no_api_key(client) -> None:
    response = client.post('/api/v1/bili-accounts/qr')
    assert response.status_code == 503
    assert response.json()['detail'] == 'BLREC_API_KEY is required for Bilibili writes'


def test_account_response_never_contains_credentials(authenticated_client, account) -> None:
    response = authenticated_client.get('/api/v1/bili-accounts')
    body = json.dumps(response.json()).lower()
    for forbidden in ('access_token', 'refresh_token', 'cookie', 'bili_jct', 'csrf', 'buvid'):
        assert forbidden not in body
```

The remaining route tests use these exact assertions:

```python
assert other_client.get(qr_status_url).status_code == 403
assert owner_client.get(expired_qr_url).json()['state'] == 'expired'
assert client.put(conflicting_policy_url, json=conflict).status_code == 409
assert client.post(item_decision_url, json={
    'action': 'retry_accept_duplicate_risk', 'reason': ''
}).status_code == 422
assert client.post(item_decision_url, json={
    'action': 'assume_success', 'reason': 'remote checked manually'
}).status_code == 204
assert service.switches == {'upload': False, 'comment': True, 'danmaku': False}
```

- [ ] **Step 2: Run and confirm routes are missing**

Run: `python -m pytest tests/web/test_bili_accounts.py tests/web/test_upload_routes.py -v`

Expected: FAIL with 404/missing router imports.

- [ ] **Step 3: Add a management-only auth dependency**

```python
@dataclass(frozen=True)
class ManagementPrincipal:
    subject: str


async def require_management_auth(
    request: Request, x_api_key: Optional[str] = Header(None)
) -> ManagementPrincipal:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='BLREC_API_KEY is required for Bilibili writes',
        )
    await authenticate(request, x_api_key)
    assert request.client is not None
    key_id = hashlib.sha256(api_key.encode('utf8')).hexdigest()[:12]
    return ManagementPrincipal('{}:{}'.format(key_id, request.client.host))
```

Attach `dependencies=[Depends(require_management_auth)]` to all three new routers even when the application's global optional auth dependency is absent, and inject the returned principal into QR/manual-decision handlers. Feature-key errors return 503; invalid credentials return 401/403; disabled actions return 409 with a public explanation.

- [ ] **Step 4: Implement exact endpoint surface**

```text
GET    /api/v1/bili-accounts
POST   /api/v1/bili-accounts/qr
GET    /api/v1/bili-accounts/qr/{session_id}
DELETE /api/v1/bili-accounts/qr/{session_id}
POST   /api/v1/bili-accounts/{account_id}/refresh
POST   /api/v1/bili-accounts/{account_id}/pause
POST   /api/v1/bili-accounts/{account_id}/archive

GET    /api/v1/upload-policies
PUT    /api/v1/upload-policies/{room_id}
DELETE /api/v1/upload-policies/{room_id}

GET    /api/v1/uploads
GET    /api/v1/uploads/{job_id}
GET    /api/v1/uploads/backlog
POST   /api/v1/uploads/{job_id}/pause
POST   /api/v1/uploads/{job_id}/resume
POST   /api/v1/uploads/{job_id}/reconcile
POST   /api/v1/uploads/{job_id}/submit-decision
POST   /api/v1/uploads/{job_id}/session-decision
POST   /api/v1/uploads/items/{item_id}/decision
PUT    /api/v1/uploads/switches
```

QR responses expose a displayable QR URL/payload only until confirmation/expiry. Account DTO fields are ID, UID, display name, state, expiry, credential version, last health time, and pause reason. Job detail exposes states/errors/fingerprints but never raw requests, upstream bodies, secrets, or signed URLs.

- [ ] **Step 5: Validate policy and manual-decision schemas**

Use discriminated literals:

```python
class SessionDecision(BaseModel):
    action: Literal['finish', 'skip', 'merge']
    target_session_id: Optional[int] = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class UnknownItemDecision(BaseModel):
    action: Literal['assume_success', 'retry_accept_duplicate_risk']
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class UnknownSubmitDecision(BaseModel):
    action: Literal['reconcile_bvid', 'retry_accept_duplicate_risk', 'abandon']
    bvid: Optional[str] = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]
```

Require `target_session_id` only for merge and reject it for other actions. Require `bvid` only for `reconcile_bvid`; fetch that archive and verify owner, filenames, part count/order, and fingerprint before confirmation. A risky resubmit remains explicit; abandon never deletes local files. Record authenticated manager subject, timestamp, old/new state, and reason before mutating the item/job.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/web/test_bili_accounts.py tests/web/test_upload_routes.py -v`

Expected: PASS for auth, redaction, validation, switches, and manual audit decisions.

```bash
git add src/blrec/web/security.py src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/upload_policies.py src/blrec/web/routers/uploads.py src/blrec/web/routers/__init__.py src/blrec/web/main.py tests/web/test_bili_accounts.py tests/web/test_upload_routes.py
git commit -m "feat: add secure upload management api"
```

### Task 13: Add account, policy, job, and backlog management UI

**Files:**
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/app.component.html`
- Modify: `webapp/src/app/core/http-interceptors/auth.interceptor.ts`
- Modify: `webapp/src/app/core/http-interceptors/auth.interceptor.spec.ts`
- Create: `webapp/src/app/uploads/uploads.module.ts`
- Create: `webapp/src/app/uploads/uploads-routing.module.ts`
- Create: `webapp/src/app/uploads/shared/upload.models.ts`
- Create: `webapp/src/app/uploads/shared/upload.service.ts`
- Create: `webapp/src/app/uploads/shared/upload.service.spec.ts`
- Create: `webapp/src/app/uploads/accounts/accounts.component.ts`
- Create: `webapp/src/app/uploads/accounts/accounts.component.html`
- Create: `webapp/src/app/uploads/accounts/accounts.component.spec.ts`
- Create: `webapp/src/app/uploads/policies/policies.component.ts`
- Create: `webapp/src/app/uploads/policies/policies.component.html`
- Create: `webapp/src/app/uploads/policies/policies.component.spec.ts`
- Create: `webapp/src/app/uploads/jobs/jobs.component.ts`
- Create: `webapp/src/app/uploads/jobs/jobs.component.html`
- Create: `webapp/src/app/uploads/jobs/job-detail.component.ts`
- Create: `webapp/src/app/uploads/jobs/job-detail.component.html`
- Create: `webapp/src/app/uploads/jobs/jobs.component.spec.ts`
- Create: `webapp/src/app/uploads/backlog/backlog.component.ts`
- Create: `webapp/src/app/uploads/backlog/backlog.component.html`
- Create: `webapp/src/app/uploads/backlog/backlog.component.spec.ts`

**Interfaces:**
- Consumes: Task 12 endpoints through `UploadService`.
- Produces: lazy `/uploads` page with typed account/policy/job/backlog states and explicit manual-risk confirmation.

- [ ] **Step 1: Write failing model/service safety tests**

```typescript
it('does not retry POST management actions', () => {
  service.startQr().subscribe({ error: () => undefined });
  const request = http.expectOne('/api/v1/bili-accounts/qr');
  request.flush('failed', { status: 500, statusText: 'Server Error' });
  http.expectNone('/api/v1/bili-accounts/qr');
});


it('requires duplicate-risk text before retrying an unknown danmaku', () => {
  component.item = unknownDanmakuFixture;
  component.reason = '';
  component.retryAcceptingDuplicateRisk();
  expect(uploadService.decideItem).not.toHaveBeenCalled();
});
```

The component tests use these exact assertions:

```typescript
const text = fixture.nativeElement.textContent.toLowerCase();
for (const secret of ['access_token', 'refresh_token', 'cookie', 'csrf', 'buvid']) {
  expect(text).not.toContain(secret);
}
expect(jobFixture.nativeElement.textContent).toContain('unknown_outcome');
for (const label of ['新增速率', '完成速率', '净积压', '积压数量', '最旧等待', '预计完成']) {
  expect(backlogFixture.nativeElement.textContent).toContain(label);
}
```

- [ ] **Step 2: Run focused frontend tests and confirm missing module**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Expected: FAIL because the uploads module does not exist.

- [ ] **Step 3: Define discriminated view and item states**

```typescript
export type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'error'; message: string };

export type ExternalWriteState =
  | { state: 'prepared' }
  | { state: 'in_flight' }
  | { state: 'confirmed'; remoteId: number | null }
  | { state: 'unknown_outcome'; message: string }
  | { state: 'failed_permanent'; code: number | null; message: string };

export interface AccountSummary {
  id: number;
  uid: number;
  displayName: string;
  state: 'active' | 'paused' | 'refresh_unknown' | 'archived';
  expiresAt: number;
  credentialVersion: number;
  pauseReason: string | null;
}
```

Do not define credential/token/Cookie fields in any TypeScript interface. Represent branch states independently so comment failure and danmaku progress can coexist.

- [ ] **Step 4: Stop blanket retries for mutations**

Change the existing auth interceptor so only GET/HEAD requests use `retry(3)`; POST/PUT/DELETE pass through one time after auth header injection. A 401 may prompt for a key but must not automatically replay the mutation. Add unit tests for one failed POST and up to three GET attempts.

- [ ] **Step 5: Implement the four focused views**

Accounts: create/display QR, poll bounded session, status/expiry/pause/relogin. Policies: room/account/templates/category/tags/copyright/source plus comment/backfill/filter validation. Jobs: searchable state list and detail with parts, upload/review/comment/backfill, pause/resume/reconcile, cancelled-session decisions, and unknown-submit choices (`reconcile_bvid`, risky retry, abandon). Backlog: per-account rates/ETA and three emergency switches.

Every backfill page displays this exact warning:

```text
回灌弹幕会长期出现在投稿视频中，发送者显示为投稿账号，不是原直播观众账号。
```

The risky retry dialog displays the full item content/target and requires typing a nonempty reason before sending `retry_accept_duplicate_risk`.

- [ ] **Step 6: Register lazy routing and navigation**

Add `{ path: 'uploads', loadChildren: () => import('./uploads/uploads.module').then((m) => m.UploadsModule) }` before the wildcard route. Add one “投稿管理” navigation item. Child routes are `accounts`, `policies`, `jobs`, `jobs/:id`, and `backlog`, with `/uploads` redirecting to `/uploads/jobs`.

- [ ] **Step 7: Run frontend verification and commit**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless`

Expected: all Jasmine/Karma tests PASS.

Run: `cd webapp && npm run build`

Expected: Angular production build succeeds with no TypeScript errors and lazy uploads chunk generated.

```bash
git add webapp/src/app/app-routing.module.ts webapp/src/app/app.component.html webapp/src/app/core/http-interceptors webapp/src/app/uploads
git commit -m "feat: add built-in upload management ui"
```

### Task 14: Add CI, Docker recovery smoke tests, protocol canary, and rollout gates

**Files:**
- Modify: `Dockerfile`
- Modify: `.github/workflows/test.yml` (created by the prerequisite batch-monitor plan)
- Create: `tests/integration/test_upload_recovery.py`
- Create: `tests/integration/test_upload_docker.sh`
- Create: `docs/operations/bilibili-upload-canary.md`
- Create: `docs/operations/bilibili-upload-rollout.md`

**Interfaces:**
- Consumes: all previous tasks and the companion batch-monitor plan.
- Produces: Python 3.8/current CI, normal/forced-restart recovery evidence, explicit real-write canary procedure, and 3–5 room/full-rollout gates.

- [ ] **Step 1: Add deterministic forced-restart integration coverage**

The fake protocol scenario must stop the process after: chunk 2 confirmation; archive submit request accepted but response dropped; root comment accepted but response dropped; and danmaku accepted but response dropped. After restart assert:

```python
assert protocol.chunk_calls_by_number == {0: 1, 1: 1, 2: 1, 3: 1}
assert protocol.archive_submit_calls == 1
assert recovered_job.submit_state == 'confirmed'  # read-side archive reconciliation
assert protocol.root_comment_calls == 1
assert recovered_comment.state == 'confirmed'  # exact-content reconciliation
assert protocol.danmaku_calls == 1
assert recovered_danmaku.state == 'unknown_outcome'  # no blind retry
assert fake_recorder.interruptions == 0
```

- [ ] **Step 2: Add Docker volume/key/filesystem smoke cases**

Build one image, then test: empty `/cfg`; existing settings without DB; restart with populated DB/WAL; wrong credential key leaving ciphertext unchanged and writes paused; missing API key leaving recording up and writes disabled; two containers sharing one DB rejected by process lock; simulated unsupported shared-lock filesystem rejected; SIGTERM checkpoint; SIGKILL lease recovery. Mount `/rec` read/write without moving/deleting recordings.

- [ ] **Step 3: Add CI matrix and static checks**

The Python job runs 3.8 and the Dockerfile version, installs `.[dev]`, then runs `pytest`, `black --check`, `isort --check-only`, `flake8`, and `mypy src/blrec`. The frontend job runs `npm ci`, headless Karma, and `npm run build`. A Docker job executes `tests/integration/test_upload_docker.sh`. All protocol tests use local fixtures/fakes and block outbound write hosts.

- [ ] **Step 4: Write the one-account protocol canary procedure**

Require a disposable short video owned by the selected real uploader account, API key and key file configured, all automatic switches initially off, and an operator confirmation before each action. Execute exactly: TV QR login/identity check; one preupload/chunk/complete/archive; wait for review/CID; one fixed comment; one test video danmaku; inspect remote result; then delete/retain manually according to operator choice. Stop immediately on challenge, `-352/-412/429`, token mismatch, or any unknown outcome. Do not automate CAPTCHA, proxy/identity changes, or repeated probing.

- [ ] **Step 5: Write the 3–5 room rollout and full-scale gate**

Enable 3–5 rooms for at least three continuous days and require each room to complete one live. Verify no lost/corrupt/missing part; final remux path; correct account/BVID/CID/timestamps; no avoidable duplicate write; safe network/container restart; no recorder resource starvation; expected pause on risk; and measured per-account import/confirmed/net backlog. Before all 58 rooms, require the companion batch-monitor rollout complete and no unsustainable growing backfill backlog. If backlog grows, keep rows and pacing, postpone full backfill, and do not raise speed/add accounts/drop data.

- [ ] **Step 6: Run full verification**

Run:

```bash
python -m pytest -v
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless
cd webapp && npm run build
docker build -t blrec:bili-upload-test .
bash tests/integration/test_upload_docker.sh blrec:bili-upload-test
```

Expected: every command exits 0; no test performs a real remote write; crash tests leave ambiguous danmaku manual and do not duplicate archive/comment writes.

- [ ] **Step 7: Commit the release gate**

```bash
git add Dockerfile .github/workflows/test.yml tests/integration/test_upload_recovery.py tests/integration/test_upload_docker.sh docs/operations/bilibili-upload-canary.md docs/operations/bilibili-upload-rollout.md
git commit -m "test: verify built-in bilibili upload rollout"
```

## Plan Self-Review

- Spec coverage: Tasks 1–5 cover fail-closed security, local SQLite, encrypted account bundles, fixed protocol matrix, QR/refresh, durable sessions, immutable part order, cancelled runs, and final postprocess artifacts. Tasks 6–10 cover recoverable UPOS, remote-write ambiguity, review/CID, independent comment/backfill branches, SC/guard-only comments, all eligible danmaku, fair ≥25-second pacing, error codes, and backlog. Tasks 11–14 cover lifecycle, switches, API/UI, Docker, canary, and 3–5 room/full rollout.
- Type consistency: `CredentialBundle`, `WriteState`, `JobState`, `FileIdentity`, `AccountWriteGate`, and `unknown_outcome` retain one name and meaning across protocol, worker, route, and UI tasks.
- Safety boundary: no step adds AI, a daily/per-part quantity cap, write fallback to the global Cookie, automatic CAPTCHA handling, proxy/account rotation, blind retry, or a second runtime/database.
