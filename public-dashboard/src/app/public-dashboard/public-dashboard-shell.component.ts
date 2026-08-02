import { ChangeDetectionStrategy, Component } from '@angular/core';

import { seasonOption } from './public-dashboard.data';
import { CURRENT_SEASON_KEY } from './public-dashboard.models';

@Component({
  selector: 'app-public-dashboard-shell',
  templateUrl: './public-dashboard-shell.component.html',
  styleUrls: ['./public-dashboard-shell.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardShellComponent {
  readonly currentSeason = seasonOption(CURRENT_SEASON_KEY);
}
