import {
  AfterViewInit,
  ChangeDetectorRef,
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
  DashboardAdminApiService,
  DashboardAdminHero,
  DashboardAdminMatch,
  DashboardAdminMatchPlayer,
} from './dashboard-admin-api.service';
import {
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
  adminEditorOpen = false;
  adminLoading = false;
  adminSaving = false;
  adminError = '';
  adminSaved = false;
  adminMatch: DashboardAdminMatch | null = null;
  adminHeroes: readonly DashboardAdminHero[] = [];
  adminRecordedPlayer = '';

  private readonly restoreFocusTo = document.activeElement as HTMLElement | null;
  private readonly previousBodyOverflow = document.body.style.overflow;

  constructor(
    readonly adminApi: DashboardAdminApiService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

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

  async openAdminEditor(): Promise<void> {
    if (!this.adminApi.enabled || this.adminLoading) {
      return;
    }
    this.adminEditorOpen = true;
    this.adminLoading = true;
    this.adminError = '';
    this.adminSaved = false;
    this.changeDetector.markForCheck();
    try {
      const [match, heroes] = await Promise.all([
        this.adminApi.getMatch(this.match.id),
        this.adminApi.listHeroes(),
      ]);
      this.adminMatch = match;
      this.adminHeroes = heroes;
      const recorded = match.players.find((player) => player.isRecordedPlayer);
      this.adminRecordedPlayer =
        recorded === undefined ? '' : `${recorded.side}:${recorded.slot}`;
    } catch (error: unknown) {
      console.warn('Unable to load internal match editor', error);
      this.adminError = '识别原始数据加载失败，请稍后重试。';
    } finally {
      this.adminLoading = false;
      this.changeDetector.markForCheck();
    }
  }

  closeAdminEditor(): void {
    if (this.adminSaving) {
      return;
    }
    this.adminEditorOpen = false;
    this.adminError = '';
    this.changeDetector.markForCheck();
  }

  async saveAdminCorrection(): Promise<void> {
    const match = this.adminMatch;
    if (match === null || this.adminSaving) {
      return;
    }
    this.adminSaving = true;
    this.adminError = '';
    this.adminSaved = false;
    this.changeDetector.markForCheck();
    try {
      const separator = this.adminRecordedPlayer.indexOf(':');
      const recordedSide = this.adminRecordedPlayer.slice(0, separator);
      const recordedSlot = Number(this.adminRecordedPlayer.slice(separator + 1));
      let recordedPlayer:
        | Readonly<{ side: 'left' | 'right'; slot: number }>
        | undefined;
      if (
        separator > 0 &&
        (recordedSide === 'left' || recordedSide === 'right') &&
        Number.isInteger(recordedSlot)
      ) {
        recordedPlayer = { side: recordedSide, slot: recordedSlot };
      }
      const saved = await this.adminApi.updateMatch(match.id, {
        title: match.title,
        gameMode: match.gameMode,
        durationSeconds: match.durationSeconds,
        resultText: match.resultText,
        endReason: match.endReason,
        matchKind: match.matchKind,
        viewContext: match.viewContext,
        statsEligible: match.statsEligible,
        winnerColor: match.winnerColor,
        leftKills: match.leftKills,
        rightKills: match.rightKills,
        leftEconomy: match.leftEconomy,
        rightEconomy: match.rightEconomy,
        recordedPlayer,
        players: match.players.map((player) => ({
          side: player.side,
          slot: player.slot,
          name: player.name,
          heroId: player.heroId,
          kills: player.kills,
          deaths: player.deaths,
          assists: player.assists,
          economy: player.economy,
          lastHits: player.lastHits,
          afkManualOverride: player.afkManualOverride,
        })),
      });
      this.adminMatch = saved;
      this.adminSaved = true;
    } catch (error: unknown) {
      console.warn('Unable to save internal match correction', error);
      this.adminError = '保存失败，原数据没有被改动。';
    } finally {
      this.adminSaving = false;
      this.changeDetector.markForCheck();
    }
  }

  adminHeroName(heroId: number | null): string {
    if (heroId === null) {
      return '未识别';
    }
    return this.adminHeroes.find((hero) => hero.id === heroId)?.label ?? `#${heroId}`;
  }

  adminRecordedCandidates(): readonly DashboardAdminMatchPlayer[] {
    const match = this.adminMatch;
    if (match === null) {
      return [];
    }
    const tealSide = match.leftColor === 'teal' ? 'left' : 'right';
    return match.players.filter((player) => player.side === tealSide);
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
    switch (this.match.rating?.afkAdjustment) {
      case 'protected_loss':
        return '队友挂机，本局失败不扣分';
      case 'undermanned_win':
        return '少打多获胜，本局获得额外加分';
      case 'self_afk':
        return '本人挂机，本局按挂机惩罚计分';
      default:
        return '';
    }
  }

  trackPlayer(_index: number, player: DashboardMatchPlayer): string {
    return `${player.name}:${player.heroName}`;
  }
}
