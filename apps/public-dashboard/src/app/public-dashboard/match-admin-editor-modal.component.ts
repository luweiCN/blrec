import {
  AfterViewInit,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  OnDestroy,
  OnInit,
  Output,
  ViewChild,
} from '@angular/core';

import {
  DashboardAdminApiService,
  DashboardAdminHero,
  DashboardAdminMatch,
  DashboardAdminMatchPlayer,
} from './dashboard-admin-api.service';
import { DashboardMatch } from './public-dashboard.models';

@Component({
  selector: 'app-match-admin-editor-modal',
  templateUrl: './match-admin-editor-modal.component.html',
  styleUrls: ['./match-admin-editor-modal.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MatchAdminEditorModalComponent
  implements OnInit, AfterViewInit, OnDestroy
{
  @Input() match!: DashboardMatch;
  @Input() streamerName = '主播';
  @Output() readonly closeEditor = new EventEmitter<void>();
  @ViewChild('dialog') private dialog?: ElementRef<HTMLElement>;
  @ViewChild('closeButton')
  private closeButton?: ElementRef<HTMLButtonElement>;
  @ViewChild('heroSearch') private heroSearch?: ElementRef<HTMLInputElement>;

  loading = true;
  saving = false;
  error = '';
  saved = false;
  editableMatch: DashboardAdminMatch | null = null;
  heroes: readonly DashboardAdminHero[] = [];
  recordedPlayer = '';
  heroPickerPlayer: DashboardAdminMatchPlayer | null = null;
  heroQuery = '';
  readonly sides = ['left', 'right'] as const;

  private readonly restoreFocusTo = document.activeElement as HTMLElement | null;

  constructor(
    readonly adminApi: DashboardAdminApiService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.load();
  }

  ngAfterViewInit(): void {
    this.closeButton?.nativeElement.focus({ preventScroll: true });
  }

  ngOnDestroy(): void {
    this.restoreFocusTo?.focus({ preventScroll: true });
  }

  @HostListener('document:keydown', ['$event'])
  onDocumentKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (this.heroPickerPlayer !== null) {
        this.closeHeroPicker();
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
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
    if (!this.saving) {
      this.closeEditor.emit();
    }
  }

  onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.close();
    }
  }

  async save(): Promise<void> {
    const match = this.editableMatch;
    if (match === null || this.saving) {
      return;
    }
    this.saving = true;
    this.error = '';
    this.saved = false;
    this.changeDetector.markForCheck();
    try {
      const separator = this.recordedPlayer.indexOf(':');
      const recordedSide = this.recordedPlayer.slice(0, separator);
      const recordedSlot = Number(this.recordedPlayer.slice(separator + 1));
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
      this.editableMatch = saved;
      this.saved = true;
    } catch (error: unknown) {
      console.warn('Unable to save internal match correction', error);
      this.error = '保存失败，原数据没有被改动。';
    } finally {
      this.saving = false;
      this.changeDetector.markForCheck();
    }
  }

  heroName(heroId: number | null): string {
    if (heroId === null) {
      return '未识别';
    }
    return this.heroes.find((hero) => hero.id === heroId)?.label ?? `#${heroId}`;
  }

  heroThumbnail(heroId: number | null): string {
    if (heroId === null) {
      return '';
    }
    const hero = this.heroes.find((candidate) => candidate.id === heroId);
    return hero === undefined ? '' : this.adminApi.heroThumbnail(hero);
  }

  heroOptionThumbnail(hero: DashboardAdminHero): string {
    return this.adminApi.heroThumbnail(hero);
  }

  get filteredHeroes(): readonly DashboardAdminHero[] {
    const query = this.heroQuery.trim().toLocaleLowerCase('zh-CN');
    return query === ''
      ? this.heroes
      : this.heroes.filter((hero) =>
          hero.label.toLocaleLowerCase('zh-CN').includes(query),
        );
  }

  openHeroPicker(player: DashboardAdminMatchPlayer): void {
    this.heroPickerPlayer = player;
    this.heroQuery = '';
    this.changeDetector.markForCheck();
    setTimeout(() => {
      this.heroSearch?.nativeElement.focus({ preventScroll: true });
    });
  }

  closeHeroPicker(): void {
    this.heroPickerPlayer = null;
    this.heroQuery = '';
    this.changeDetector.markForCheck();
  }

  onHeroPickerBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.closeHeroPicker();
    }
  }

  selectHero(heroId: number | null): void {
    if (this.heroPickerPlayer !== null) {
      this.heroPickerPlayer.heroId = heroId;
    }
    this.closeHeroPicker();
  }

  isRecordedPlayer(player: DashboardAdminMatchPlayer): boolean {
    return this.recordedPlayer === `${player.side}:${player.slot}`;
  }

  setRecordedPlayer(player: DashboardAdminMatchPlayer): void {
    const candidate = `${player.side}:${player.slot}`;
    this.recordedPlayer = this.recordedPlayer === candidate ? '' : candidate;
  }

  recordedPlayerLabel(): string {
    const match = this.editableMatch;
    if (match === null || this.recordedPlayer === '') {
      return '未确认';
    }
    const player = match.players.find(
      (candidate) =>
        `${candidate.side}:${candidate.slot}` === this.recordedPlayer,
    );
    return player === undefined
      ? '未确认'
      : `${player.side === 'left' ? '左' : '右'}${player.slot} · ${this.heroName(player.heroId)} · ${player.name || '未知玩家'}`;
  }

  players(side: 'left' | 'right'): readonly DashboardAdminMatchPlayer[] {
    return (
      this.editableMatch?.players.filter((player) => player.side === side) ?? []
    );
  }

  sideLabel(side: 'left' | 'right'): string {
    const match = this.editableMatch;
    const color =
      match === null
        ? 'unknown'
        : side === 'left'
          ? match.leftColor
          : match.rightColor;
    const colorLabel =
      color === 'teal' ? '蓝方' : color === 'orange' ? '红方' : '颜色未知';
    return `${side === 'left' ? '左队' : '右队'} · ${colorLabel}`;
  }

  private async load(): Promise<void> {
    try {
      const [match, heroes] = await Promise.all([
        this.adminApi.getMatch(this.match.id),
        this.adminApi.listHeroes(),
      ]);
      this.editableMatch = match;
      this.heroes = heroes;
      const recorded = match.players.find((player) => player.isRecordedPlayer);
      this.recordedPlayer =
        recorded === undefined ? '' : `${recorded.side}:${recorded.slot}`;
    } catch (error: unknown) {
      console.warn('Unable to load internal match editor', error);
      this.error = '识别原始数据加载失败，请稍后重试。';
    } finally {
      this.loading = false;
      this.changeDetector.markForCheck();
    }
  }
}
