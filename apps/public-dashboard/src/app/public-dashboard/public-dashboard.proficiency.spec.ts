import {
  getHeroProficiencies,
  getHeroProficiencyLeader,
  getPlayerHeroProficiencies,
  heroProficiencyLevel,
} from './public-dashboard.proficiency';
import {
  DashboardSnapshot,
  HeroUsage,
  PlayerStanding,
} from './public-dashboard.models';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('hero proficiency', () => {
  it('scores and sorts every hero played by the selected player', () => {
    const proficiencies = getPlayerHeroProficiencies(
      TEST_DASHBOARD_SNAPSHOT,
      '2026-summer',
      '3v3',
      1,
    );

    expect(proficiencies.length).toBe(7);
    expect(proficiencies.map((record) => record.score)).toEqual(
      [...proficiencies]
        .map((record) => record.score)
        .sort((left, right) => right - left),
    );
    expect(
      proficiencies.every(
        (record) =>
          Number.isInteger(record.score) &&
          record.score >= 0 &&
          record.score <= 100 &&
          record.level === heroProficiencyLevel(record.score),
      ),
    ).toBeTrue();
  });

  it('uses the same highest proficiency player on the hero and player views', () => {
    const proficiencies = getHeroProficiencies(
      TEST_DASHBOARD_SNAPSHOT,
      '2026-summer',
      '3v3',
      'Vox',
    );
    const leader = getHeroProficiencyLeader(
      TEST_DASHBOARD_SNAPSHOT,
      '2026-summer',
      '3v3',
      'Vox',
    );

    expect(proficiencies.length).toBeGreaterThan(1);
    expect(leader).toEqual(proficiencies[0]);
  });

  it('does not let one perfect game outrank stable long-term usage', () => {
    const players =
      TEST_DASHBOARD_SNAPSHOT.standings['2026-summer'].players;
    const oneGame = withHeroUsage(players[0], {
      name: 'Vox',
      matches: 1,
      wins: 1,
      stats: {
        kdaMatches: 1,
        kills: 10,
        deaths: 0,
        assists: 20,
        economyMatches: 1,
        economy: 30_000,
      },
    });
    const established = withHeroUsage(players[1], {
      name: 'Vox',
      matches: 20,
      wins: 12,
      stats: {
        kdaMatches: 20,
        kills: 80,
        deaths: 40,
        assists: 120,
        economyMatches: 20,
        economy: 250_000,
      },
    });
    const snapshot: DashboardSnapshot = {
      ...TEST_DASHBOARD_SNAPSHOT,
      standings: {
        ...TEST_DASHBOARD_SNAPSHOT.standings,
        '2026-summer': {
          ...TEST_DASHBOARD_SNAPSHOT.standings['2026-summer'],
          players: [oneGame, established],
        },
      },
    };

    const proficiencies = getHeroProficiencies(
      snapshot,
      '2026-summer',
      '3v3',
      'Vox',
    );

    expect(proficiencies.map((record) => record.player.id)).toEqual([
      established.id,
      oneGame.id,
    ]);
    expect(proficiencies[0].score).toBeGreaterThan(proficiencies[1].score);
    expect(proficiencies[1].score).toBeLessThan(45);
  });
});

function withHeroUsage(
  player: PlayerStanding,
  usage: HeroUsage,
): PlayerStanding {
  return {
    ...player,
    heroPool: [usage],
    heroPools: {
      all: [usage],
      '3v3': [usage],
      brawl: [],
      '5v5': [],
    },
  };
}
