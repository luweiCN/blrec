UPDATE vainglory_publications
SET state='prepared',
    remote_verified_at=NULL,
    attempt_count=0,
    next_attempt_at=0,
    error='等待远端重新复核简介、视频分段、评论和置顶位置',
    priority=0
WHERE state='confirmed';
