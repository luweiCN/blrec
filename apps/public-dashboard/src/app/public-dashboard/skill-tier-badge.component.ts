import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

import { SkillTier, skillTierForRatingScore } from './skill-tier';

export type SkillTierBadgeVariant =
  | 'compact'
  | 'featured'
  | 'icon'
  | 'podium';

@Component({
  selector: 'app-skill-tier-badge',
  templateUrl: './skill-tier-badge.component.html',
  styleUrls: ['./skill-tier-badge.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SkillTierBadgeComponent {
  @Input() ratingScore: number | null = null;
  @Input() provisional = false;
  @Input() variant: SkillTierBadgeVariant = 'compact';

  get skillTier(): SkillTier | null {
    return skillTierForRatingScore(this.ratingScore);
  }

  description(skillTier: SkillTier): string {
    return (
      `站内段位：${skillTier.name}${skillTier.divisionLabel}，` +
      `${skillTier.tier} 段；榜单分 ${skillTier.ratingScore}，` +
      `换算旧分 ${skillTier.legacyPoints}`
    );
  }
}
