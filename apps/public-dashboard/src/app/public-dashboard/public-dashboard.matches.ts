import {
  DashboardMatch,
  MatchResult,
  ModeFilter,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';
import { matchesSearchSegments } from './public-dashboard.search';

export interface DashboardMatchFilters {
  readonly seasonKey: SeasonKey;
  readonly mode: ModeFilter;
  readonly fixedPlayerId?: number;
  readonly playerQuery: string;
  readonly selectedHeroes: readonly string[];
}

export interface MatchStreak {
  readonly result: MatchResult;
  readonly matches: number;
}

export function filterDashboardMatches(
  matches: readonly DashboardMatch[],
  players: readonly PlayerStanding[],
  filters: DashboardMatchFilters,
): readonly DashboardMatch[] {
  const playersById = new Map(players.map((player) => [player.id, player]));
  const selectedHeroes = filters.selectedHeroes.map((name) =>
    name.toLocaleLowerCase(),
  );
  return matches.filter((match) => {
    if (
      (filters.seasonKey !== 'all-time' &&
        match.seasonKey !== filters.seasonKey) ||
      (filters.mode !== 'all' && match.mode !== filters.mode) ||
      (filters.fixedPlayerId !== undefined &&
        match.playerId !== filters.fixedPlayerId)
    ) {
      return false;
    }
    const participants = [...match.ally.players, ...match.enemy.players];
    if (filters.playerQuery.trim() !== '') {
      const participantNames = participants
        .filter(
          (participant) =>
            filters.fixedPlayerId === undefined ||
            !participant.isRecordedPlayer,
        )
        .map((participant) => participant.name);
      const stablePlayer = playersById.get(match.playerId);
      const stablePlayerSegments =
        filters.fixedPlayerId === undefined && stablePlayer !== undefined
          ? [
              stablePlayer.name,
              stablePlayer.roomLabel,
              ...stablePlayer.aliases,
            ]
          : [];
      if (
        !matchesSearchSegments(
          [
            ...(match.streamTitle === undefined ? [] : [match.streamTitle]),
            ...stablePlayerSegments,
            ...participantNames,
          ],
          filters.playerQuery,
        )
      ) {
        return false;
      }
    }
    const lineupHeroes = new Set(
      participants.map((participant) =>
        participant.heroName.toLocaleLowerCase(),
      ),
    );
    return selectedHeroes.every((heroName) => lineupHeroes.has(heroName));
  });
}

export function currentMatchStreak(
  matches: readonly DashboardMatch[],
): MatchStreak | null {
  const first = matches[0];
  if (first === undefined) {
    return null;
  }
  let count = 0;
  for (const match of matches) {
    if (match.result !== first.result) {
      break;
    }
    count += 1;
  }
  return { result: first.result, matches: count };
}
