import {
  HeroPerformance,
  HeroStanding,
  HeroUsage,
  MatchResult,
  ModeFilter,
  Performance,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';

interface PlayerSeed {
  readonly id: number;
  readonly name: string;
  readonly roomId: string;
  readonly aliases: readonly string[];
  readonly trend: number;
  readonly form: readonly MatchResult[];
  readonly modes: Readonly<Record<ModeFilter, Performance>>;
  readonly heroPool: readonly HeroUsage[];
}

function performance(
  matches: number,
  wins: number,
  topHero: string,
): Performance {
  return { matches, wins, topHero };
}

function heroPerformance(
  matches: number,
  wins: number,
  players: number,
): HeroPerformance {
  return { matches, wins, players };
}

function player(seed: PlayerSeed): PlayerStanding {
  return {
    id: seed.id,
    name: seed.name,
    initial: seed.name.slice(-1),
    roomLabel: '直播间 ' + seed.roomId,
    aliases: seed.aliases,
    trend: seed.trend,
    form: seed.form,
    modes: seed.modes,
    heroPool: seed.heroPool,
  };
}

const CURRENT_PLAYERS: readonly PlayerStanding[] = [
  player({
    id: 1,
    name: '星河',
    roomId: '22625025',
    aliases: ['XingHe', '河老板'],
    trend: 2,
    form: ['W', 'W', 'L', 'W', 'W'],
    modes: {
      all: performance(176, 113, 'Caine'),
      '3v3': performance(102, 70, 'Caine'),
      brawl: performance(46, 27, 'Vox'),
      '5v5': performance(28, 16, 'Celeste'),
    },
    heroPool: [
      { name: 'Caine', matches: 42, wins: 31 },
      { name: 'Vox', matches: 28, wins: 18 },
      { name: 'Celeste', matches: 21, wins: 13 },
    ],
  }),
  player({
    id: 2,
    name: '洛川',
    roomId: '31415926',
    aliases: ['LuoChuan', '洛水'],
    trend: 0,
    form: ['W', 'L', 'W', 'W', 'L'],
    modes: {
      all: performance(203, 126, 'Vox'),
      '3v3': performance(118, 71, 'Blackfeather'),
      brawl: performance(61, 42, 'Vox'),
      '5v5': performance(24, 13, 'Idris'),
    },
    heroPool: [
      { name: 'Vox', matches: 48, wins: 33 },
      { name: 'Blackfeather', matches: 37, wins: 22 },
      { name: 'Idris', matches: 24, wins: 14 },
    ],
  }),
  player({
    id: 3,
    name: '阿曜',
    roomId: '90817263',
    aliases: ['A-Yao', '曜'],
    trend: 1,
    form: ['L', 'W', 'W', 'L', 'W'],
    modes: {
      all: performance(154, 93, 'Idris'),
      '3v3': performance(79, 45, 'Taka'),
      brawl: performance(39, 23, 'Vox'),
      '5v5': performance(36, 25, 'Idris'),
    },
    heroPool: [
      { name: 'Idris', matches: 39, wins: 28 },
      { name: 'Taka', matches: 26, wins: 15 },
      { name: 'Kestrel', matches: 20, wins: 12 },
    ],
  }),
  player({
    id: 4,
    name: '木杉',
    roomId: '44556677',
    aliases: ['MuShan', '木木'],
    trend: -1,
    form: ['W', 'W', 'L', 'L', 'W'],
    modes: {
      all: performance(219, 128, 'Celeste'),
      '3v3': performance(141, 84, 'Celeste'),
      brawl: performance(52, 31, 'Lyra'),
      '5v5': performance(26, 13, 'Kestrel'),
    },
    heroPool: [
      { name: 'Celeste', matches: 54, wins: 34 },
      { name: 'Lyra', matches: 31, wins: 19 },
      { name: 'Kestrel', matches: 27, wins: 14 },
    ],
  }),
  player({
    id: 5,
    name: '南栀',
    roomId: '77889901',
    aliases: ['NanZhi', '栀子'],
    trend: 3,
    form: ['W', 'L', 'W', 'W', 'W'],
    modes: {
      all: performance(132, 80, 'Kestrel'),
      '3v3': performance(82, 48, 'Kestrel'),
      brawl: performance(31, 21, 'Celeste'),
      '5v5': performance(19, 11, 'Vox'),
    },
    heroPool: [
      { name: 'Kestrel', matches: 36, wins: 23 },
      { name: 'Celeste', matches: 25, wins: 17 },
      { name: 'Vox', matches: 18, wins: 10 },
    ],
  }),
  player({
    id: 6,
    name: '遥风',
    roomId: '13572468',
    aliases: ['YaoFeng', '风仔'],
    trend: -2,
    form: ['L', 'W', 'L', 'W', 'W'],
    modes: {
      all: performance(188, 108, 'Blackfeather'),
      '3v3': performance(109, 62, 'Blackfeather'),
      brawl: performance(54, 32, 'Reim'),
      '5v5': performance(25, 14, 'Idris'),
    },
    heroPool: [
      { name: 'Blackfeather', matches: 49, wins: 29 },
      { name: 'Reim', matches: 29, wins: 18 },
      { name: 'Idris', matches: 22, wins: 12 },
    ],
  }),
  player({
    id: 7,
    name: '栗子',
    roomId: '10293847',
    aliases: ['Chestnut', '小栗'],
    trend: 0,
    form: ['W', 'L', 'L', 'W', 'L'],
    modes: {
      all: performance(167, 92, 'Taka'),
      '3v3': performance(97, 55, 'Taka'),
      brawl: performance(43, 23, 'Vox'),
      '5v5': performance(27, 14, 'Kestrel'),
    },
    heroPool: [
      { name: 'Taka', matches: 44, wins: 26 },
      { name: 'Vox', matches: 30, wins: 16 },
      { name: 'Kestrel', matches: 19, wins: 9 },
    ],
  }),
  player({
    id: 8,
    name: '北屿',
    roomId: '66778899',
    aliases: ['BeiYu', '北北'],
    trend: 1,
    form: ['L', 'L', 'W', 'W', 'L'],
    modes: {
      all: performance(121, 64, 'Reim'),
      '3v3': performance(63, 33, 'Reim'),
      brawl: performance(34, 20, 'Lyra'),
      '5v5': performance(24, 11, 'Celeste'),
    },
    heroPool: [
      { name: 'Reim', matches: 33, wins: 19 },
      { name: 'Lyra', matches: 24, wins: 14 },
      { name: 'Celeste', matches: 17, wins: 8 },
    ],
  }),
  player({
    id: 9,
    name: '云渡',
    roomId: '38174625',
    aliases: ['YunDu', '渡口'],
    trend: 2,
    form: ['W', 'L', 'W', 'L', 'W'],
    modes: {
      all: performance(146, 82, 'Ringo'),
      '3v3': performance(78, 44, 'Ringo'),
      brawl: performance(44, 26, 'Koshka'),
      '5v5': performance(24, 12, 'Catherine'),
    },
    heroPool: [
      { name: 'Ringo', matches: 38, wins: 22 },
      { name: 'Koshka', matches: 27, wins: 17 },
      { name: 'Catherine', matches: 20, wins: 10 },
    ],
  }),
  player({
    id: 10,
    name: '清昼',
    roomId: '82736415',
    aliases: ['QingZhou', '白昼'],
    trend: -1,
    form: ['L', 'W', 'W', 'L', 'L'],
    modes: {
      all: performance(196, 107, 'Skye'),
      '3v3': performance(121, 68, 'Skye'),
      brawl: performance(49, 25, 'Adagio'),
      '5v5': performance(26, 14, 'Vox'),
    },
    heroPool: [
      { name: 'Skye', matches: 46, wins: 27 },
      { name: 'Adagio', matches: 34, wins: 17 },
      { name: 'Vox', matches: 22, wins: 12 },
    ],
  }),
  player({
    id: 11,
    name: '纸鸢',
    roomId: '91827364',
    aliases: ['ZhiYuan', '风筝'],
    trend: 1,
    form: ['W', 'L', 'L', 'W', 'W'],
    modes: {
      all: performance(112, 59, 'Catherine'),
      '3v3': performance(68, 37, 'Catherine'),
      brawl: performance(26, 14, 'Lyra'),
      '5v5': performance(18, 8, 'Grace'),
    },
    heroPool: [
      { name: 'Catherine', matches: 35, wins: 20 },
      { name: 'Lyra', matches: 24, wins: 14 },
      { name: 'Grace', matches: 18, wins: 8 },
    ],
  }),
  player({
    id: 12,
    name: '林深',
    roomId: '47281936',
    aliases: ['LinShen', '深林'],
    trend: -2,
    form: ['L', 'W', 'L', 'L', 'W'],
    modes: {
      all: performance(184, 95, 'Samuel'),
      '3v3': performance(104, 55, 'Samuel'),
      brawl: performance(50, 26, 'Reim'),
      '5v5': performance(30, 14, 'Idris'),
    },
    heroPool: [
      { name: 'Samuel', matches: 45, wins: 24 },
      { name: 'Reim', matches: 31, wins: 16 },
      { name: 'Idris', matches: 25, wins: 11 },
    ],
  }),
  player({
    id: 13,
    name: '长夜',
    roomId: '56372819',
    aliases: ['ChangYe', '夜'],
    trend: 1,
    form: ['W', 'L', 'W', 'L', 'L'],
    modes: {
      all: performance(129, 65, 'Krul'),
      '3v3': performance(80, 42, 'Krul'),
      brawl: performance(29, 14, 'Reim'),
      '5v5': performance(20, 9, 'Catherine'),
    },
    heroPool: [
      { name: 'Krul', matches: 41, wins: 22 },
      { name: 'Reim', matches: 24, wins: 11 },
      { name: 'Catherine', matches: 20, wins: 9 },
    ],
  }),
  player({
    id: 14,
    name: '青禾',
    roomId: '74629183',
    aliases: ['QingHe', '禾苗'],
    trend: 0,
    form: ['L', 'W', 'L', 'W', 'L'],
    modes: {
      all: performance(158, 78, 'Koshka'),
      '3v3': performance(91, 47, 'Koshka'),
      brawl: performance(42, 20, 'Taka'),
      '5v5': performance(25, 11, 'Vox'),
    },
    heroPool: [
      { name: 'Koshka', matches: 43, wins: 23 },
      { name: 'Taka', matches: 28, wins: 13 },
      { name: 'Vox', matches: 21, wins: 9 },
    ],
  }),
  player({
    id: 15,
    name: '扶光',
    roomId: '19283746',
    aliases: ['FuGuang', '微光'],
    trend: -1,
    form: ['L', 'L', 'W', 'L', 'W'],
    modes: {
      all: performance(96, 46, 'Gwen'),
      '3v3': performance(54, 27, 'Gwen'),
      brawl: performance(24, 11, 'Skye'),
      '5v5': performance(18, 8, 'Catherine'),
    },
    heroPool: [
      { name: 'Gwen', matches: 31, wins: 16 },
      { name: 'Skye', matches: 22, wins: 10 },
      { name: 'Catherine', matches: 17, wins: 7 },
    ],
  }),
  player({
    id: 16,
    name: '三七',
    roomId: '61928374',
    aliases: ['SanQi', '37'],
    trend: 1,
    form: ['W', 'L', 'L', 'L', 'W'],
    modes: {
      all: performance(138, 64, 'Alpha'),
      '3v3': performance(75, 36, 'Alpha'),
      brawl: performance(38, 18, 'Koshka'),
      '5v5': performance(25, 10, 'Grace'),
    },
    heroPool: [
      { name: 'Alpha', matches: 38, wins: 19 },
      { name: 'Koshka', matches: 27, wins: 12 },
      { name: 'Grace', matches: 22, wins: 9 },
    ],
  }),
];

const CURRENT_HEROES: readonly HeroStanding[] = [
  {
    id: 'caine',
    name: 'Caine',
    modes: {
      all: heroPerformance(148, 98, 6),
      '3v3': heroPerformance(90, 64, 5),
      brawl: heroPerformance(34, 20, 4),
      '5v5': heroPerformance(24, 14, 3),
    },
  },
  {
    id: 'vox',
    name: 'Vox',
    modes: {
      all: heroPerformance(171, 108, 8),
      '3v3': heroPerformance(87, 51, 7),
      brawl: heroPerformance(55, 39, 7),
      '5v5': heroPerformance(29, 18, 5),
    },
  },
  {
    id: 'idris',
    name: 'Idris',
    modes: {
      all: heroPerformance(119, 73, 6),
      '3v3': heroPerformance(58, 32, 5),
      brawl: heroPerformance(24, 13, 4),
      '5v5': heroPerformance(37, 28, 5),
    },
  },
  {
    id: 'celeste',
    name: 'Celeste',
    modes: {
      all: heroPerformance(156, 94, 7),
      '3v3': heroPerformance(86, 52, 6),
      brawl: heroPerformance(47, 31, 6),
      '5v5': heroPerformance(23, 11, 4),
    },
  },
  {
    id: 'blackfeather',
    name: 'Blackfeather',
    modes: {
      all: heroPerformance(132, 76, 6),
      '3v3': heroPerformance(81, 49, 6),
      brawl: heroPerformance(29, 16, 4),
      '5v5': heroPerformance(22, 11, 3),
    },
  },
  {
    id: 'kestrel',
    name: 'Kestrel',
    modes: {
      all: heroPerformance(127, 72, 7),
      '3v3': heroPerformance(69, 42, 6),
      brawl: heroPerformance(31, 17, 5),
      '5v5': heroPerformance(27, 13, 5),
    },
  },
  {
    id: 'taka',
    name: 'Taka',
    modes: {
      all: heroPerformance(150, 87, 7),
      '3v3': heroPerformance(99, 60, 7),
      brawl: heroPerformance(31, 17, 5),
      '5v5': heroPerformance(20, 10, 3),
    },
  },
  {
    id: 'reim',
    name: 'Reim',
    modes: {
      all: heroPerformance(118, 67, 6),
      '3v3': heroPerformance(66, 39, 5),
      brawl: heroPerformance(34, 18, 5),
      '5v5': heroPerformance(18, 10, 3),
    },
  },
  {
    id: 'lyra',
    name: 'Lyra',
    modes: {
      all: heroPerformance(105, 61, 6),
      '3v3': heroPerformance(54, 31, 5),
      brawl: heroPerformance(33, 21, 5),
      '5v5': heroPerformance(18, 9, 3),
    },
  },
  {
    id: 'ringo',
    name: 'Ringo',
    modes: {
      all: heroPerformance(144, 80, 8),
      '3v3': heroPerformance(85, 49, 7),
      brawl: heroPerformance(36, 19, 5),
      '5v5': heroPerformance(23, 12, 4),
    },
  },
  {
    id: 'koshka',
    name: 'Koshka',
    modes: {
      all: heroPerformance(92, 54, 6),
      '3v3': heroPerformance(55, 34, 5),
      brawl: heroPerformance(24, 13, 4),
      '5v5': heroPerformance(13, 7, 3),
    },
  },
  {
    id: 'catherine',
    name: 'Catherine',
    modes: {
      all: heroPerformance(137, 77, 8),
      '3v3': heroPerformance(72, 43, 7),
      brawl: heroPerformance(39, 20, 6),
      '5v5': heroPerformance(26, 14, 5),
    },
  },
  {
    id: 'skye',
    name: 'Skye',
    modes: {
      all: heroPerformance(88, 51, 5),
      '3v3': heroPerformance(50, 30, 5),
      brawl: heroPerformance(22, 12, 4),
      '5v5': heroPerformance(16, 9, 3),
    },
  },
  {
    id: 'samuel',
    name: 'Samuel',
    modes: {
      all: heroPerformance(103, 58, 6),
      '3v3': heroPerformance(58, 35, 5),
      brawl: heroPerformance(27, 14, 4),
      '5v5': heroPerformance(18, 9, 3),
    },
  },
];

function seasonScale(season: SeasonKey): number {
  switch (season) {
    case '2026-summer':
      return 1;
    case '2026-spring':
      return 0.78;
    case '2025-autumn':
      return 0.64;
    case 'all-time':
      return 2.45;
    default:
      return 1;
  }
}

function seasonVariance(seed: number, season: SeasonKey): number {
  if (season === '2026-summer') {
    return 0;
  }
  const direction = (seed % 5) - 2;
  const strength = season === '2026-spring' ? 0.007 : 0.011;
  return direction * strength;
}

function scaledPerformance(
  value: Performance,
  season: SeasonKey,
  seed: number,
): Performance {
  const matches = Math.max(1, Math.round(value.matches * seasonScale(season)));
  const rate = Math.min(
    0.78,
    Math.max(0.34, value.wins / value.matches + seasonVariance(seed, season)),
  );
  return {
    ...value,
    matches,
    wins: Math.round(matches * rate),
  };
}

function scaledPlayer(
  value: PlayerStanding,
  season: SeasonKey,
): PlayerStanding {
  if (season === '2026-summer') {
    return value;
  }
  const three = scaledPerformance(value.modes['3v3'], season, value.id + 1);
  const brawl = scaledPerformance(value.modes.brawl, season, value.id + 2);
  const five = scaledPerformance(value.modes['5v5'], season, value.id + 3);
  return {
    ...value,
    modes: {
      all: {
        matches: three.matches + brawl.matches + five.matches,
        wins: three.wins + brawl.wins + five.wins,
        topHero: value.modes.all.topHero,
      },
      '3v3': three,
      brawl,
      '5v5': five,
    },
    heroPool: value.heroPool.map((hero, index) => {
      const matches = Math.max(
        1,
        Math.round(hero.matches * seasonScale(season)),
      );
      const rate = Math.min(
        0.8,
        Math.max(
          0.3,
          hero.wins / hero.matches + seasonVariance(value.id + index, season),
        ),
      );
      return { ...hero, matches, wins: Math.round(matches * rate) };
    }),
  };
}

function scaledHero(
  value: HeroStanding,
  season: SeasonKey,
  index: number,
): HeroStanding {
  if (season === '2026-summer') {
    return value;
  }
  const scale = (performance: HeroPerformance, offset: number) => {
    const matches = Math.max(
      1,
      Math.round(performance.matches * seasonScale(season)),
    );
    const rate = Math.min(
      0.78,
      Math.max(
        0.3,
        performance.wins / performance.matches +
          seasonVariance(index + offset, season),
      ),
    );
    return {
      matches,
      wins: Math.round(matches * rate),
      players: Math.max(
        1,
        Math.round(performance.players * Math.min(seasonScale(season), 1.8)),
      ),
    };
  };
  const three = scale(value.modes['3v3'], 1);
  const brawl = scale(value.modes.brawl, 2);
  const five = scale(value.modes['5v5'], 3);
  return {
    ...value,
    modes: {
      all: {
        matches: three.matches + brawl.matches + five.matches,
        wins: three.wins + brawl.wins + five.wins,
        players: Math.max(three.players, brawl.players, five.players),
      },
      '3v3': three,
      brawl,
      '5v5': five,
    },
  };
}

export function playersForSeason(season: SeasonKey): readonly PlayerStanding[] {
  return CURRENT_PLAYERS.map((value) => scaledPlayer(value, season));
}

export function heroesForSeason(season: SeasonKey): readonly HeroStanding[] {
  return CURRENT_HEROES.map((value, index) => scaledHero(value, season, index));
}
