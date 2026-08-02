import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';

import {
  CURRENT_SEASON_KEY,
  ModeFilter,
  ModeOption,
  MODE_OPTIONS,
  SeasonKey,
  SeasonOption,
  SEASON_OPTIONS,
} from './public-dashboard.models';

@Component({
  selector: 'app-leaderboard-filters',
  templateUrl: './leaderboard-filters.component.html',
  styleUrls: ['./leaderboard-filters.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeaderboardFiltersComponent {
  @Input() activeSeason: SeasonKey = CURRENT_SEASON_KEY;
  @Input() activeMode: ModeFilter = 'all';

  @Output() readonly activeSeasonChange = new EventEmitter<SeasonKey>();
  @Output() readonly activeModeChange = new EventEmitter<ModeFilter>();

  readonly seasonOptions: readonly SeasonOption[] = SEASON_OPTIONS;
  readonly modeOptions: readonly ModeOption[] = MODE_OPTIONS;

  selectSeason(season: SeasonKey): void {
    this.activeSeasonChange.emit(season);
  }

  selectMode(mode: ModeFilter): void {
    this.activeModeChange.emit(mode);
  }

  trackMode(_index: number, mode: ModeOption): ModeFilter {
    return mode.key;
  }
}
