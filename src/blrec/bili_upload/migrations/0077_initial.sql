ALTER TABLE vainglory_players
ADD COLUMN public_visible INTEGER NOT NULL DEFAULT 1
CHECK (public_visible IN (0,1));

ALTER TABLE vainglory_archive_syncs
ADD COLUMN daily_limit_override_v2 INTEGER
CHECK (daily_limit_override_v2 > 0);

ALTER TABLE archive_migration_jobs
ADD COLUMN daily_limit_override_v2 INTEGER
CHECK (daily_limit_override_v2 > 0);

UPDATE vainglory_publications
SET visibility_scope='owner',
    visibility_verified_at=COALESCE(visibility_verified_at,updated_at),
    public_visible_at=NULL
WHERE source_kind='upload'
  AND EXISTS(
      SELECT 1
      FROM upload_jobs source_job
      WHERE source_job.id=vainglory_publications.upload_job_id
        AND (
            instr(
                lower(replace(COALESCE(source_job.policy_snapshot_json,''),' ','')),
                '"is_only_self":true'
            )>0
            OR instr(
                lower(replace(COALESCE(source_job.policy_snapshot_json,''),' ','')),
                '"is_only_self":1'
            )>0
        )
  );

UPDATE vainglory_publications
SET visibility_scope='owner',
    visibility_verified_at=COALESCE(visibility_verified_at,updated_at),
    public_visible_at=NULL
WHERE source_kind='archive'
  AND EXISTS(
      SELECT 1
      FROM vainglory_archive_imports imported
      WHERE imported.account_id=vainglory_publications.account_id
        AND imported.bvid=vainglory_publications.bvid
        AND imported.is_only_self=1
  );
