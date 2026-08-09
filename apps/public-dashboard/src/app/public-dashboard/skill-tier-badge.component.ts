import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

import { SkillTier, skillTierForRatingScore } from './skill-tier';

export type SkillTierBadgeVariant =
  | 'compact'
  | 'icon'
  | 'showcase';

const SCORE_FORMATTER = new Intl.NumberFormat('zh-CN');

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
      `${skillTier.tier} 段，${SCORE_FORMATTER.format(skillTier.displayScore)}`
    );
  }
}
