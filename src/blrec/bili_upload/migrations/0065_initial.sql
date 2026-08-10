DELETE FROM vainglory_players
WHERE id IN (
    SELECT player.id
    FROM vainglory_players player
    JOIN vainglory_player_sessions direct
        ON direct.player_id=player.id
    JOIN recording_sessions session
        ON session.id=direct.session_id
    WHERE player.origin='automatic'
      AND player.name='玩家 ' || session.id
      AND player.created_at=direct.created_at
      AND session.room_id<=0
      AND (session.anchor_uid IS NULL OR session.anchor_uid<=0)
      AND trim(session.anchor_name)=''
      AND NOT EXISTS(
          SELECT 1 FROM vainglory_player_rooms room
          WHERE room.player_id=player.id
      )
      AND (
          SELECT COUNT(*) FROM vainglory_player_sessions other
          WHERE other.player_id=player.id
      )=1
);
