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
  @Input() metricLabel = '站内段位与排位分';

  get skillTier(): SkillTier | null {
    return skillTierForRatingScore(this.ratingScore);
  }

  description(skillTier: SkillTier): string {
    return (
      `${this.metricLabel}：${skillTier.name}${skillTier.divisionLabel}，` +
      `${skillTier.tier} 段，${SCORE_FORMATTER.format(skillTier.displayScore)} 排位分`
    );
  }
}
