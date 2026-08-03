import { ChangeDetectionStrategy, Component } from '@angular/core';

import { seasonOption } from './public-dashboard.data';
import { DashboardDataService } from './public-dashboard-data.service';

@Component({
  selector: 'app-public-dashboard-shell',
  templateUrl: './public-dashboard-shell.component.html',
  styleUrls: ['./public-dashboard-shell.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardShellComponent {
  constructor(readonly data: DashboardDataService) {}

  get currentSeasonLabel(): string {
    const snapshot = this.data.snapshotOrNull;
    return snapshot === null
      ? '数据载入中'
      : seasonOption(snapshot, snapshot.currentSeasonKey).label;
  }

  get lastUpdatedLabel(): string {
    const snapshot = this.data.snapshotOrNull;
    if (snapshot === null) {
      return '';
    }
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(snapshot.generatedAt));
  }
}
