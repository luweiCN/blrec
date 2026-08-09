ALTER TABLE vainglory_publications
ADD COLUMN plan_state TEXT NOT NULL DEFAULT 'ready'
CHECK (plan_state IN ('waiting_analysis','ready'));

ALTER TABLE vainglory_publications
ADD COLUMN match_count INTEGER NOT NULL DEFAULT 0
CHECK (match_count >= 0);

ALTER TABLE vainglory_publications
ADD COLUMN force_republish INTEGER NOT NULL DEFAULT 0
CHECK (force_republish IN (0,1));

ALTER TABLE vainglory_publications
ADD COLUMN active_revision_id INTEGER;

ALTER TABLE vainglory_publications
ADD COLUMN published_revision_id INTEGER;

CREATE TABLE vainglory_analysis_revisions (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    part_id INTEGER
        REFERENCES recording_parts(id) ON DELETE SET NULL,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    request_kind TEXT NOT NULL CHECK (length(request_kind) > 0),
    algorithm_version INTEGER NOT NULL CHECK (algorithm_version >= 0),
    match_count INTEGER NOT NULL CHECK (match_count >= 0),
    snapshot_hash TEXT NOT NULL CHECK (
        length(snapshot_hash)=64
        AND snapshot_hash NOT GLOB '*[^0-9a-f]*'
    ),
    snapshot_json TEXT NOT NULL CHECK (length(snapshot_json) > 1),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    UNIQUE(part_id,revision_no)
);

CREATE INDEX vainglory_analysis_revisions_session_idx
ON vainglory_analysis_revisions(session_id,created_at,id);

CREATE TABLE vainglory_publication_revisions (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL
        REFERENCES vainglory_publications(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    previous_payload_hash TEXT CHECK (
        previous_payload_hash IS NULL OR (
            length(previous_payload_hash)=64
            AND previous_payload_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash)=64
        AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    match_count INTEGER NOT NULL CHECK (match_count >= 0),
    analysis_revision_ids_json TEXT NOT NULL,
    analysis_snapshot_json TEXT NOT NULL CHECK (length(analysis_snapshot_json) > 1),
    description_block TEXT NOT NULL,
    comments_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (
        reason IN ('legacy','initial','changed','forced','unchanged')
    ),
    state TEXT NOT NULL CHECK (
        state IN ('prepared','confirmed','unchanged')
    ),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    confirmed_at INTEGER CHECK (
        confirmed_at IS NULL OR confirmed_at >= created_at
    ),
    UNIQUE(publication_id,revision_no)
);

CREATE INDEX vainglory_publication_revisions_publication_idx
ON vainglory_publication_revisions(publication_id,revision_no DESC);

UPDATE vainglory_publications
SET match_count=(
    SELECT COUNT(*)
    FROM vainglory_matches match
    WHERE match.session_id=vainglory_publications.session_id
);

INSERT INTO vainglory_publication_revisions(
    publication_id,revision_no,previous_payload_hash,payload_hash,match_count,
    analysis_revision_ids_json,analysis_snapshot_json,description_block,
    comments_json,reason,state,created_at,confirmed_at
)
SELECT id,1,NULL,payload_hash,match_count,'[]','{"legacy":true}',
       description_block,'[]','legacy',
       CASE WHEN state='confirmed' THEN 'confirmed' ELSE 'prepared' END,
       created_at,
       CASE WHEN state='confirmed' THEN updated_at ELSE NULL END
FROM vainglory_publications;

UPDATE vainglory_publications
SET active_revision_id=(
        SELECT revision.id
        FROM vainglory_publication_revisions revision
        WHERE revision.publication_id=vainglory_publications.id
        ORDER BY revision.revision_no DESC
        LIMIT 1
    ),
    published_revision_id=CASE
        WHEN state='confirmed' THEN (
            SELECT revision.id
            FROM vainglory_publication_revisions revision
            WHERE revision.publication_id=vainglory_publications.id
            ORDER BY revision.revision_no DESC
            LIMIT 1
        )
        ELSE NULL
    END,
    state='prepared',
    chapter_state='prepared',
    description_state='prepared',
    comment_cleanup_state='prepared',
    pin_state='prepared',
    root_rpid=NULL,
    needs_refresh=1,
    force_republish=1,
    next_attempt_at=0,
    error='等待全量核对并重新发布',
    updated_at=CAST(strftime('%s','now') AS INTEGER);

CREATE INDEX vainglory_publications_plan_idx
ON vainglory_publications(plan_state,needs_refresh,force_republish,priority,id);
