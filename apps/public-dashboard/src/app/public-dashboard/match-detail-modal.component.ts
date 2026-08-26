import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  OnInit,
  OnDestroy,
  Output,
  ViewChild,
} from '@angular/core';

import {
  afkRatingAdjustmentLabel,
  formatEconomy,
  heroImage,
  modeLabel,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  DashboardMatch,
  DashboardMatchPlayer,
  DashboardMatchTeam,
} from './public-dashboard.models';

@Component({
  selector: 'app-match-detail-modal',
  templateUrl: './match-detail-modal.component.html',
  styleUrls: ['./match-detail-modal.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MatchDetailModalComponent
  implements OnInit, AfterViewInit, OnDestroy
{
  @Input() match!: DashboardMatch;
  @Input() streamerName = '主播';
  @Input() initialImageExpanded = false;
  @Output() readonly closeModal = new EventEmitter<void>();
  @ViewChild('dialog') private dialog?: ElementRef<HTMLElement>;
  @ViewChild('closeButton') private closeButton?: ElementRef<HTMLButtonElement>;
  imageExpanded = false;

  private readonly restoreFocusTo = document.activeElement as HTMLElement | null;
  private readonly previousBodyOverflow = document.body.style.overflow;

  ngOnInit(): void {
    this.imageExpanded = this.initialImageExpanded;
  }

  ngAfterViewInit(): void {
    document.body.style.overflow = 'hidden';
    this.closeButton?.nativeElement.focus({ preventScroll: true });
  }

  ngOnDestroy(): void {
    document.body.style.overflow = this.previousBodyOverflow;
    this.restoreFocusTo?.focus({ preventScroll: true });
  }

  @HostListener('document:keydown', ['$event'])
  onDocumentKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (this.imageExpanded) {
        this.closeImage();
        return;
      }
      this.close();
      return;
    }
    if (event.key !== 'Tab') {
      return;
    }
    const focusable = Array.from(
      this.dialog?.nativeElement.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  close(): void {
    this.closeModal.emit();
  }

  onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.close();
    }
  }

  openImage(): void {
    this.imageExpanded = true;
  }

  closeImage(): void {
    if (this.initialImageExpanded) {
      this.close();
      return;
    }
    this.imageExpanded = false;
  }

  onImageBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.closeImage();
    }
  }

  heroImage(heroName: string): string {
    return heroImage(heroName);
  }

  heroName(heroName: string): string {
    return heroName === '' ? '未识别' : heroDisplayName(heroName);
  }

  modeName(): string {
    return modeLabel(this.match.mode);
  }

  formatEconomy(value: number | null): string {
    return formatEconomy(value);
  }

  teamResult(team: DashboardMatchTeam): string {
    return team.side === this.match.ally.side
      ? this.match.result === 'W'
        ? '胜利'
        : '失败'
      : this.match.result === 'W'
        ? '失败'
        : '胜利';
  }

  teamLabel(team: DashboardMatchTeam): string {
    return team.color === 'teal' ? '蓝方' : '红方';
  }

  formatDate(value: string): string {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(value));
  }

  formatDuration(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const remaining = seconds % 60;
    return `${minutes}:${String(remaining).padStart(2, '0')}`;
  }

  afkAdjustmentLabel(): string {
    return afkRatingAdjustmentLabel(this.match.rating?.afkAdjustment);
  }

  trackPlayer(_index: number, player: DashboardMatchPlayer): string {
    return `${player.name}:${player.heroName}`;
  }
}
