import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';

import { SeasonKey, SeasonOption } from './public-dashboard.models';

@Component({
  selector: 'app-leaderboard-filters',
  templateUrl: './leaderboard-filters.component.html',
  styleUrls: ['./leaderboard-filters.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeaderboardFiltersComponent {
  @Input() activeSeason: SeasonKey = 'all-time';
  @Input() seasonOptions: readonly SeasonOption[] = [];

  @Output() readonly activeSeasonChange = new EventEmitter<SeasonKey>();

  selectSeason(season: SeasonKey): void {
    this.activeSeasonChange.emit(season);
  }
}
