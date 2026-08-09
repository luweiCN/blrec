ALTER TABLE vainglory_publications
ADD COLUMN public_visible_at INTEGER
CHECK (public_visible_at IS NULL OR public_visible_at > 0);

UPDATE vainglory_publications
SET public_visible_at=updated_at
WHERE state='confirmed';
