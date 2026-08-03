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
} from './public-dashboard.models';

@Component({
  selector: 'app-dashboard-mode-switcher',
  templateUrl: './dashboard-mode-switcher.component.html',
  styleUrls: ['./dashboard-mode-switcher.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DashboardModeSwitcherComponent {
  @Input() value: ModeFilter = '3v3';
  @Output() readonly valueChange = new EventEmitter<ModeFilter>();

  readonly options: readonly ModeOption[] = MODE_OPTIONS;

  selectMode(mode: ModeFilter): void {
    this.valueChange.emit(mode);
  }

  trackMode(_index: number, mode: ModeOption): ModeFilter {
    return mode.key;
  }
}
