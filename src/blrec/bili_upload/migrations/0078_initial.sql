CREATE TABLE vainglory_remote_media_controls (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
    downloads_per_interface INTEGER NOT NULL DEFAULT 3
        CHECK (downloads_per_interface BETWEEN 1 AND 8),
    updated_at INTEGER NOT NULL CHECK (updated_at > 0)
);

INSERT INTO vainglory_remote_media_controls(
    singleton_id,downloads_per_interface,updated_at
) VALUES(1,3,1);
