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

export interface SkillTierProgress {
  readonly bronzeLabelPosition: number;
  readonly goldBoundaryPosition: number;
  readonly goldLabelPosition: number;
  readonly progressPosition: number;
  readonly silverBoundaryPosition: number;
  readonly silverLabelPosition: number;
  readonly skillTier: SkillTier;
  readonly tierMaximumScore: number;
  readonly tierMinimumScore: number;
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
const DISPLAY_SCORE_MULTIPLIER = 3;
const DISPLAY_SCORE_MAXIMUM = 3_000;

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
    if (displayScore < SKILL_TIER_START_POINTS[index]) {
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
      `${division.key}-hd.webp`,
    maximumScore:
      nextStart === undefined
        ? DISPLAY_SCORE_MAXIMUM
        : nextStart - 1,
    minimumScore: SKILL_TIER_START_POINTS[skillTierIndex],
    name: tierName.localized,
    ratingScore: normalizedScore,
    tier,
  };
}

export function skillTierProgressForRatingScore(
  ratingScore: number | null,
): SkillTierProgress | null {
  const skillTier = skillTierForRatingScore(ratingScore);
  if (skillTier === null) {
    return null;
  }

  const tierStartIndex = (skillTier.tier - 1) * DIVISIONS.length;
  const tierMinimumScore = SKILL_TIER_START_POINTS[tierStartIndex];
  const silverMinimumScore = SKILL_TIER_START_POINTS[tierStartIndex + 1];
  const goldMinimumScore = SKILL_TIER_START_POINTS[tierStartIndex + 2];
  const tierMaximumScore =
    SKILL_TIER_START_POINTS[tierStartIndex + DIVISIONS.length] ??
    DISPLAY_SCORE_MAXIMUM;

  return {
    bronzeLabelPosition: scorePosition(
      (tierMinimumScore + silverMinimumScore) / 2,
      tierMinimumScore,
      tierMaximumScore,
    ),
    goldBoundaryPosition: scorePosition(
      goldMinimumScore,
      tierMinimumScore,
      tierMaximumScore,
    ),
    goldLabelPosition: scorePosition(
      (goldMinimumScore + tierMaximumScore) / 2,
      tierMinimumScore,
      tierMaximumScore,
    ),
    progressPosition: scorePosition(
      skillTier.displayScore,
      tierMinimumScore,
      tierMaximumScore,
    ),
    silverBoundaryPosition: scorePosition(
      silverMinimumScore,
      tierMinimumScore,
      tierMaximumScore,
    ),
    silverLabelPosition: scorePosition(
      (silverMinimumScore + goldMinimumScore) / 2,
      tierMinimumScore,
      tierMaximumScore,
    ),
    skillTier,
    tierMaximumScore,
    tierMinimumScore,
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

function scorePosition(score: number, minimum: number, maximum: number): number {
  return Math.min(
    100,
    Math.max(0, ((score - minimum) / (maximum - minimum)) * 100),
  );
}
