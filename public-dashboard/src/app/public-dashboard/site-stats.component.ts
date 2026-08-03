import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

import { SiteStats, SiteStatsState } from './site-stats.models';

@Component({
  selector: 'app-site-stats',
  templateUrl: './site-stats.component.html',
  styleUrls: ['./site-stats.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SiteStatsComponent {
  @Input() state: SiteStatsState = { kind: 'loading' };

  get stats(): SiteStats | null {
    return this.state.kind === 'ready' ? this.state.stats : null;
  }

  get generatedAtLabel(): string {
    const stats = this.stats;
    if (stats === null) {
      return '';
    }
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: stats.timezone,
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(stats.generatedAt));
  }
}
