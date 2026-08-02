ALTER TABLE vainglory_matches
ADD COLUMN result_frame_path TEXT
CHECK (
    result_frame_path IS NULL OR (
        result_frame_path=trim(result_frame_path)
        AND length(result_frame_path) BETWEEN 1 AND 240
        AND result_frame_path NOT LIKE '/%'
        AND instr(result_frame_path,'..')=0
        AND result_frame_path GLOB '*.png'
    )
);
