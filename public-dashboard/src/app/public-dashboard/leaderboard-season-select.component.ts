import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  Output,
  QueryList,
  ViewChild,
  ViewChildren,
} from '@angular/core';

import {
  CURRENT_SEASON_KEY,
  SeasonKey,
  SeasonOption,
  SEASON_OPTIONS,
} from './public-dashboard.models';

@Component({
  selector: 'app-leaderboard-season-select',
  templateUrl: './leaderboard-season-select.component.html',
  styleUrls: ['./leaderboard-season-select.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeaderboardSeasonSelectComponent {
  @Input() value: SeasonKey = CURRENT_SEASON_KEY;
  @Input() options: readonly SeasonOption[] = SEASON_OPTIONS;

  @Output() readonly valueChange = new EventEmitter<SeasonKey>();

  @ViewChild('trigger', { static: true })
  private readonly trigger?: ElementRef<HTMLButtonElement>;

  @ViewChildren('optionButton')
  private readonly optionButtons?: QueryList<ElementRef<HTMLButtonElement>>;

  isOpen = false;
  activeIndex = 0;

  constructor(private readonly host: ElementRef<HTMLElement>) {}

  get selectedOption(): SeasonOption {
    return (
      this.options.find((option) => option.key === this.value) ??
      this.options[0]
    );
  }

  toggle(): void {
    if (this.isOpen) {
      this.close(false);
      return;
    }
    this.openAt(this.selectedIndex());
  }

  onTriggerKeydown(event: KeyboardEvent): void {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        this.openAt(this.selectedIndex());
        break;
      case 'ArrowUp':
        event.preventDefault();
        this.openAt(this.selectedIndex());
        break;
      case 'Home':
        event.preventDefault();
        this.openAt(0);
        break;
      case 'End':
        event.preventDefault();
        this.openAt(this.options.length - 1);
        break;
      case 'Escape':
        this.close(false);
        break;
    }
  }

  onOptionKeydown(event: KeyboardEvent, index: number): void {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        this.focusOption((index + 1) % this.options.length);
        break;
      case 'ArrowUp':
        event.preventDefault();
        this.focusOption(
          (index - 1 + this.options.length) % this.options.length,
        );
        break;
      case 'Home':
        event.preventDefault();
        this.focusOption(0);
        break;
      case 'End':
        event.preventDefault();
        this.focusOption(this.options.length - 1);
        break;
      case 'Escape':
        event.preventDefault();
        this.close(true);
        break;
      case 'Tab':
        this.isOpen = false;
        break;
    }
  }

  select(option: SeasonOption): void {
    if (option.key !== this.value) {
      this.valueChange.emit(option.key);
    }
    this.close(true);
  }

  trackOption(_index: number, option: SeasonOption): SeasonKey {
    return option.key;
  }

  @HostListener('document:click', ['$event'])
  closeFromOutside(event: MouseEvent): void {
    if (
      this.isOpen &&
      event.target instanceof Node &&
      !this.host.nativeElement.contains(event.target)
    ) {
      this.close(false);
    }
  }

  private selectedIndex(): number {
    const index = this.options.findIndex((option) => option.key === this.value);
    return index < 0 ? 0 : index;
  }

  private openAt(index: number): void {
    this.isOpen = true;
    this.activeIndex = Math.min(Math.max(index, 0), this.options.length - 1);
    requestAnimationFrame(() => this.focusOption(this.activeIndex));
  }

  private focusOption(index: number): void {
    this.activeIndex = index;
    this.optionButtons?.get(index)?.nativeElement.focus();
  }

  private close(restoreFocus: boolean): void {
    this.isOpen = false;
    if (restoreFocus) {
      this.trigger?.nativeElement.focus();
    }
  }
}
