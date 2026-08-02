UPDATE vainglory_publication_comments
SET
    attempt_count=0,
    next_attempt_at=0,
    error='等待删除缺图评论并重新上传结算图',
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='unknown_outcome'
AND rpid IS NOT NULL
AND publication_id IN (
    SELECT id
    FROM vainglory_publications
    WHERE state='failed'
    AND error='B 站连续未附加结算图，已停止重复发布'
);

UPDATE vainglory_publications
SET
    state='paused',
    attempt_count=0,
    next_attempt_at=0,
    error='正在安全修复缺少结算图的评论',
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='failed'
AND error='B 站连续未附加结算图，已停止重复发布'
AND EXISTS (
    SELECT 1
    FROM vainglory_publication_comments comment
    WHERE comment.publication_id=vainglory_publications.id
    AND comment.state='unknown_outcome'
    AND comment.rpid IS NOT NULL
);
