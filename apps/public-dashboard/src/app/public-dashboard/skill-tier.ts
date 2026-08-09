export type SkillTierDivision = 'bronze' | 'silver' | 'gold';

export interface SkillTier {
  readonly division: SkillTierDivision;
  readonly divisionLabel: string;
  readonly displayScore: number;
  readonly englishName: string;
  readonly imageUrl: string;
  readonly maximumScore: number;
  readonly minimumScore: number;
  readonly name: string;
  readonly ratingScore: number;
  readonly tier: number;
}

interface SkillTierName {
  readonly english: string;
  readonly localized: string;
}

interface DivisionDefinition {
  readonly key: SkillTierDivision;
  readonly label: string;
}

const RATING_SCORE_MAXIMUM = 1000;
const DISPLAY_SCORE_MULTIPLIER = 30;
const HISTORICAL_POINT_DISPLAY_SCALE = 10;
const DISPLAY_SCORE_MAXIMUM = 30_000;

const SKILL_TIER_START_POINTS = [
  0, 109, 218, 327, 436, 545, 654, 763, 872, 981, 1090, 1200,
  1250, 1300, 1350, 1400, 1467, 1533, 1600, 1667, 1733, 1800,
  1867, 1933, 2000, 2134, 2267, 2400, 2600, 2800,
] as const;

const SKILL_TIER_NAMES: readonly SkillTierName[] = [
  { localized: '初出茅庐', english: 'Just Beginning' },
  { localized: '逐步成长', english: 'Getting There' },
  { localized: '铜头铁臂', english: 'Rock Solid' },
  { localized: '值得一战', english: 'Worthy Foe' },
  { localized: '深藏不露', english: 'Got Swagger' },
  { localized: '名不虚传', english: 'Credible Threat' },
  { localized: '炉火纯青', english: 'The Hotness' },
  { localized: '神乎其技', english: 'Simply Amazing' },
  { localized: '登峰造极', english: 'Pinnacle of Awesome' },
  { localized: '至尊荣耀', english: 'Vainglorious' },
] as const;

const DIVISIONS: readonly DivisionDefinition[] = [
  { key: 'bronze', label: '铜' },
  { key: 'silver', label: '银' },
  { key: 'gold', label: '金' },
] as const;

export function skillTierForRatingScore(
  ratingScore: number | null,
): SkillTier | null {
  const normalizedScore = normalizeRatingScore(ratingScore);
  if (normalizedScore === null) {
    return null;
  }

  const displayScore = normalizedScore * DISPLAY_SCORE_MULTIPLIER;
  let skillTierIndex = 0;

  for (let index = 1; index < SKILL_TIER_START_POINTS.length; index += 1) {
    if (
      displayScore <
      SKILL_TIER_START_POINTS[index] * HISTORICAL_POINT_DISPLAY_SCALE
    ) {
      break;
    }
    skillTierIndex = index;
  }

  const tier = Math.floor(skillTierIndex / DIVISIONS.length) + 1;
  const division = DIVISIONS[skillTierIndex % DIVISIONS.length];
  const tierName = SKILL_TIER_NAMES[tier - 1];
  const nextStart = SKILL_TIER_START_POINTS[skillTierIndex + 1];

  return {
    division: division.key,
    divisionLabel: division.label,
    displayScore,
    englishName: tierName.english,
    imageUrl:
      `assets/skill-tiers/tier-${tier.toString().padStart(2, '0')}-` +
      `${division.key}.webp`,
    maximumScore:
      nextStart === undefined
        ? DISPLAY_SCORE_MAXIMUM
        : nextStart * HISTORICAL_POINT_DISPLAY_SCALE - 1,
    minimumScore:
      SKILL_TIER_START_POINTS[skillTierIndex] *
      HISTORICAL_POINT_DISPLAY_SCALE,
    name: tierName.localized,
    ratingScore: normalizedScore,
    tier,
  };
}

export function displayScoreForRatingScore(
  ratingScore: number | null,
): number | null {
  const normalizedScore = normalizeRatingScore(ratingScore);
  return normalizedScore === null
    ? null
    : normalizedScore * DISPLAY_SCORE_MULTIPLIER;
}

export function displayScoreForRatingDelta(ratingDelta: number): number {
  return Math.round(ratingDelta * DISPLAY_SCORE_MULTIPLIER);
}

function normalizeRatingScore(ratingScore: number | null): number | null {
  if (ratingScore === null || !Number.isFinite(ratingScore)) {
    return null;
  }
  return Math.round(
    Math.min(RATING_SCORE_MAXIMUM, Math.max(0, ratingScore)),
  );
}
