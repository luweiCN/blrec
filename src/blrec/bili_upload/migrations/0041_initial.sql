ALTER TABLE vainglory_archive_imports
ADD COLUMN content_classification TEXT NOT NULL DEFAULT 'unknown'
CHECK (
    content_classification IN (
        'unknown','vainglory','suspected_non_vainglory'
    )
);

ALTER TABLE vainglory_archive_imports
ADD COLUMN classification_reason TEXT
CHECK (
    classification_reason IS NULL
    OR length(classification_reason) BETWEEN 1 AND 500
);

UPDATE vainglory_archive_imports
SET content_classification=CASE
        WHEN EXISTS(
            SELECT 1
            FROM vainglory_archive_parts part
            JOIN vainglory_matches match
              ON match.result_part_id=part.recording_part_id
            WHERE part.import_id=vainglory_archive_imports.id
        ) THEN 'vainglory'
        ELSE 'suspected_non_vainglory'
    END,
    classification_reason=CASE
        WHEN EXISTS(
            SELECT 1
            FROM vainglory_archive_parts part
            JOIN vainglory_matches match
              ON match.result_part_id=part.recording_part_id
            WHERE part.import_id=vainglory_archive_imports.id
        ) THEN '已识别到虚荣对局结算'
        ELSE '所有分P分析完成，但未发现虚荣对局结算'
    END
WHERE state='ready';

CREATE INDEX vainglory_archive_imports_classification_idx
ON vainglory_archive_imports(
    content_classification,published_at DESC,id DESC
);
