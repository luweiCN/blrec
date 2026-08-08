import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  Output,
  ViewChild,
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
  @ViewChild('trigger') private trigger?: ElementRef<HTMLButtonElement>;
  @Input() value: ModeFilter = '3v3';
  @Output() readonly valueChange = new EventEmitter<ModeFilter>();

  readonly options: readonly ModeOption[] = MODE_OPTIONS;
  isOpen = false;

  get selectedOption(): ModeOption {
    return (
      this.options.find((option) => option.key === this.value) ?? this.options[0]
    );
  }

  toggleMenu(): void {
    this.isOpen = !this.isOpen;
  }

  closeMenu(restoreFocus = false): void {
    if (!this.isOpen) {
      return;
    }
    this.isOpen = false;
    if (restoreFocus) {
      this.trigger?.nativeElement.focus();
    }
  }

  selectMode(mode: ModeFilter): void {
    if (mode !== this.value) {
      this.valueChange.emit(mode);
    }
    this.closeMenu(true);
  }

  @HostListener('document:keydown.escape')
  handleEscape(): void {
    this.closeMenu(true);
  }

  trackMode(_index: number, mode: ModeOption): ModeFilter {
    return mode.key;
  }
}
