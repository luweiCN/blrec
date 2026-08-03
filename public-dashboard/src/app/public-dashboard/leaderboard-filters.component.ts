import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';

import {
  ModeFilter,
  ModeOption,
  MODE_OPTIONS,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

@Component({
  selector: 'app-leaderboard-filters',
  templateUrl: './leaderboard-filters.component.html',
  styleUrls: ['./leaderboard-filters.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeaderboardFiltersComponent {
  @Input() activeSeason: SeasonKey = 'all-time';
  @Input() activeMode: ModeFilter = 'all';
  @Input() seasonOptions: readonly SeasonOption[] = [];

  @Output() readonly activeSeasonChange = new EventEmitter<SeasonKey>();
  @Output() readonly activeModeChange = new EventEmitter<ModeFilter>();

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
