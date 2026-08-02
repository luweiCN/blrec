import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { NzMessageService } from 'ng-zorro-antd/message';
import { Subject, timer } from 'rxjs';
import { finalize, switchMap, takeUntil, takeWhile } from 'rxjs/operators';

import { RealtimeService } from '../core/services/realtime.service';
import {
  RecordingPart,
  RecordingSessionDetail,
  RemoteMediaStatus,
} from '../upload-tasks/shared/recording-session.model';
import { RecordingSessionService } from '../upload-tasks/shared/recording-session.service';
import { BiliAccount } from '../uploads/shared/bili-account.model';
import { BiliAccountService } from '../uploads/shared/bili-account.service';
import { TaskData } from '../tasks/shared/task.model';
import { TaskService } from '../tasks/shared/services/task.service';
import {
  GameMode,
  TeamColor,
  VaingloryAnalysisQueue,
  VaingloryAnalysisQueueCategory,
  VaingloryAnalysisQueueItem,
  VaingloryAnchorStats,
  VaingloryArchiveBackfillItem,
  VaingloryArchiveBackfillRealtimeSnapshot,
  VaingloryArchiveContentReview,
  VaingloryArchiveSync,
  VaingloryHero,
  VaingloryIndexRealtimeSnapshot,
  VaingloryIndexSummary,
  VaingloryMatch,
  VaingloryMatchFilters,
  VaingloryMatchPlayer,
  VaingloryMatchSession,
  VaingloryScanJob,
} from './vainglory.model';
import { VaingloryService } from './vainglory.service';

type SessionsView =
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly items: readonly VaingloryMatchSession[];
    }
  | { readonly state: 'error'; readonly message: string };

type MatchDetailsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryMatch[] }
  | { readonly state: 'error'; readonly message: string };

type HeroesView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryHero[] }
  | { readonly state: 'error'; readonly message: string };

type AnchorStatsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryAnchorStats[] }
  | { readonly state: 'error'; readonly message: string };

type RecordedPlayerReviewView =
  | { readonly state: 'idle' }
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly items: readonly VaingloryMatch[];
    }
  | { readonly state: 'error'; readonly message: string };

type HeroReviewView =
  | { readonly state: 'idle' }
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly items: readonly VaingloryMatch[];
    }
  | { readonly state: 'error'; readonly message: string };

type ScanView =
  | { readonly state: 'not_selected' }
  | { readonly state: 'loading' }
  | { readonly state: 'missing' }
  | { readonly state: 'job'; readonly job: VaingloryScanJob }
  | { readonly state: 'error'; readonly message: string };

interface ManagedAnchorOption {
  readonly anchorName: string;
  readonly roomId: number;
  readonly anchorUid: number;
  readonly label: string;
}

type BulkUpdateAction = 'anchor' | 'include' | 'exclude';

@Component({
  selector: 'app-vainglory',
  templateUrl: './vainglory.component.html',
  styleUrls: ['./vainglory.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class VaingloryComponent implements OnInit, OnDestroy {
  sessionsView: SessionsView = { state: 'loading' };
  heroesView: HeroesView = { state: 'loading' };
  anchorStatsView: AnchorStatsView = { state: 'loading' };
  scanView: ScanView = { state: 'not_selected' };
  readonly matchDetails = new Map<number, MatchDetailsView>();
  selectedSession: VaingloryMatchSession | null = null;
  detailsDrawerVisible = false;
  sessionTitleDraft = '';
  savingSessionTitle = false;
  sessionAnchorDraft = '';
  savingSessionAnchor = false;

  playerName = '';
  sourceTitle = '';
  anchorNameFilter: string | null = null;
  statsIncludedFilter: boolean | null = null;
  heroIds: number[] = [];
  winnerColor: TeamColor | null = null;
  gameMode: GameMode | null = null;
  sessionId: number | null = null;
  pageIndex = 1;
  readonly pageSize = 20;
  readonly selectedSessionIds = new Set<number>();
  bulkAnchorDraft: string | null = null;
  bulkAnchorModalVisible = false;
  bulkUpdatingAction: BulkUpdateAction | null = null;
  managedAnchors: readonly ManagedAnchorOption[] = [];
  managedAnchorsLoading = false;

  heroManagerVisible = false;
  heroReviewVisible = false;
  heroReviewView: HeroReviewView = { state: 'idle' };
  readonly heroReviewDrafts = new Map<string, number>();
  savingHeroReviewKey: string | null = null;
  recordedPlayerReviewVisible = false;
  recordedPlayerReviewView: RecordedPlayerReviewView = { state: 'idle' };
  savingRecordedPlayerMatchId: number | null = null;
  savingRecordedPlayerSlot: number | null = null;
  analysisTaskModalVisible = false;
  archiveManagerVisible = false;
  archiveAccounts: readonly BiliAccount[] = [];
  archiveAccountsLoading = false;
  archiveAccountsError: string | null = null;
  readonly archiveSyncs = new Map<number, VaingloryArchiveSync>();
  archiveItemsByAccountId: ReadonlyMap<
    number,
    readonly VaingloryArchiveBackfillItem[]
  > = new Map();
  analysisQueue: VaingloryAnalysisQueue | null = null;
  indexSummary: VaingloryIndexSummary | null = null;
  indexSampledAt: number | null = null;
  readonly requestingArchiveAccountIds = new Set<number>();
  readonly archiveDailyLimitDrafts = new Map<number, number>();
  archiveContentReviews: readonly VaingloryArchiveContentReview[] = [];
  archiveContentReviewTotal = 0;
  archiveContentReviewPageIndex = 1;
  readonly archiveContentReviewPageSize = 20;
  archiveContentReviewsLoading = false;
  archiveContentReviewsError: string | null = null;
  scanRequesting = false;

  previewSession: RecordingSessionDetail | null = null;
  previewPart: RecordingPart | null = null;
  previewVisible = false;
  previewSeekSeconds: number | null = null;
  previewOpeningKey: string | null = null;
  readonly remoteMediaStatuses = new Map<number, RemoteMediaStatus>();
  readonly recordingParts = new Map<number, RecordingPart>();

  private readonly destroy$ = new Subject<void>();
  private scanPollTimer: number | null = null;
  private readonly remoteMediaPollingPartIds = new Set<number>();

  constructor(
    private vainglory: VaingloryService,
    private recordingSessions: RecordingSessionService,
    private route: ActivatedRoute,
    private messages: NzMessageService,
    private changeDetector: ChangeDetectorRef,
    private router: Router,
    private accounts: BiliAccountService,
    private tasks: TaskService,
    private realtime: RealtimeService,
  ) {}

  ngOnInit(): void {
    this.loadHeroes();
    this.loadAnchorStats();
    this.loadManagedAnchors();
    this.loadHeroReviews(false);
    this.loadRecordedPlayerReviews(false);
    this.realtime.events$
      .pipe(takeUntil(this.destroy$))
      .subscribe((event) => {
        if (event.type === 'resync') {
          if (this.archiveManagerVisible) {
            this.refreshArchiveManager();
          }
          return;
        }
        if (event.type === 'archive_backfill') {
          this.applyArchiveBackfillSnapshot(event.data);
        }
        if (event.type === 'vainglory_index') {
          this.applyVaingloryIndexSnapshot(event.data);
        }
      });
    this.route.queryParamMap
      .pipe(takeUntil(this.destroy$))
      .subscribe((params) => {
        const rawSessionId = Number(params.get('sessionId'));
        this.sessionId =
          Number.isInteger(rawSessionId) && rawSessionId > 0
            ? rawSessionId
            : null;
        this.pageIndex = 1;
        this.stopScanPolling();
        this.loadSessions();
        this.loadScan();
      });
  }

  ngOnDestroy(): void {
    this.stopScanPolling();
    this.destroy$.next();
    this.destroy$.complete();
  }

  get sessions(): readonly VaingloryMatchSession[] {
    return this.sessionsView.state === 'ready' ? this.sessionsView.items : [];
  }

  get total(): number {
    return this.sessionsView.state === 'ready' ? this.sessionsView.total : 0;
  }

  get heroes(): readonly VaingloryHero[] {
    return this.heroesView.state === 'ready' ? this.heroesView.items : [];
  }

  get anchorStats(): readonly VaingloryAnchorStats[] {
    return this.anchorStatsView.state === 'ready'
      ? this.anchorStatsView.items
      : [];
  }

  get scanJob(): VaingloryScanJob | null {
    return this.scanView.state === 'job' ? this.scanView.job : null;
  }

  get recordedPlayerReviews(): readonly VaingloryMatch[] {
    return this.recordedPlayerReviewView.state === 'ready'
      ? this.recordedPlayerReviewView.items
      : [];
  }

  get recordedPlayerReviewTotal(): number {
    return this.recordedPlayerReviewView.state === 'ready'
      ? this.recordedPlayerReviewView.total
      : 0;
  }

  get heroReviews(): readonly VaingloryMatch[] {
    return this.heroReviewView.state === 'ready'
      ? this.heroReviewView.items
      : [];
  }

  get heroReviewTotal(): number {
    return this.heroReviewView.state === 'ready'
      ? this.heroReviewView.total
      : 0;
  }

  get unrecognizedHeroPositionCount(): number {
    if (this.indexSummary === null) {
      return 0;
    }
    return Math.max(
      0,
      this.indexSummary.playerSlotCount -
        this.indexSummary.recognizedHeroCount,
    );
  }

  get selectedDetails(): MatchDetailsView | null {
    const sessionId = this.selectedSession?.sessionId;
    return sessionId === undefined
      ? null
      : (this.matchDetails.get(sessionId) ?? null);
  }

  applySearch(): void {
    this.pageIndex = 1;
    this.selectedSessionIds.clear();
    this.loadSessions();
  }

  clearSearch(): void {
    this.playerName = '';
    this.sourceTitle = '';
    this.anchorNameFilter = null;
    this.statsIncludedFilter = null;
    this.heroIds = [];
    this.winnerColor = null;
    this.gameMode = null;
    this.applySearch();
  }

  pageChanged(pageIndex: number): void {
    if (pageIndex === this.pageIndex) {
      return;
    }
    this.pageIndex = pageIndex;
    this.selectedSessionIds.clear();
    this.loadSessions();
  }

  loadSessions(): void {
    const filters: VaingloryMatchFilters = {
      playerName: this.playerName,
      heroIds: this.heroIds,
      winnerColor: this.winnerColor,
      gameMode: this.gameMode,
      sessionId: this.sessionId,
      sourceTitle: this.sourceTitle,
      anchorName: this.anchorNameFilter,
      statsIncluded: this.statsIncludedFilter,
    };
    this.sessionsView = { state: 'loading' };
    this.detailsDrawerVisible = false;
    this.selectedSession = null;
    this.matchDetails.clear();
    this.recordingParts.clear();
    this.remoteMediaStatuses.clear();
    this.vainglory
      .listMatchSessions(
        filters,
        this.pageSize,
        (this.pageIndex - 1) * this.pageSize,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.sessionsView = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          if (
            this.sessionId !== null &&
            response.items.some((item) => item.sessionId === this.sessionId)
          ) {
            const selected = response.items.find(
              (item) => item.sessionId === this.sessionId,
            );
            if (selected !== undefined) {
              this.openSessionDetails(selected);
            }
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.sessionsView = {
            state: 'error',
            message: this.errorMessage(error, '直播场次加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  openSessionDetails(session: VaingloryMatchSession): void {
    this.selectedSession = session;
    this.sessionTitleDraft = session.title;
    this.sessionAnchorDraft = session.anchorName;
    this.detailsDrawerVisible = true;
    if (!this.matchDetails.has(session.sessionId)) {
      this.loadSessionDetails(session.sessionId);
    }
    this.changeDetector.markForCheck();
  }

  closeSessionDetails(): void {
    this.detailsDrawerVisible = false;
    this.changeDetector.markForCheck();
  }

  detailsFor(sessionId: number): MatchDetailsView | null {
    return this.matchDetails.get(sessionId) ?? null;
  }

  private loadSessionDetails(sessionId: number): void {
    const filters: VaingloryMatchFilters = {
      playerName: '',
      heroIds: [],
      winnerColor: null,
      gameMode: null,
      sessionId,
    };
    this.matchDetails.set(sessionId, { state: 'loading' });
    this.vainglory
      .listMatches(filters, 100, 0)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          const ordered = [...response.items].sort(
            (left, right) =>
              left.partIndex - right.partIndex ||
              left.resultAtMs - right.resultAtMs ||
              left.id - right.id,
          );
          this.matchDetails.set(sessionId, {
            state: 'ready',
            items: ordered,
          });
          this.loadMatchMediaStates(sessionId, ordered);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.matchDetails.set(sessionId, {
            state: 'error',
            message: this.errorMessage(error, '对局详情加载失败'),
          });
          this.changeDetector.markForCheck();
        },
      });
  }

  loadAnchorStats(): void {
    this.anchorStatsView = { state: 'loading' };
    this.vainglory
      .listAnchorStats()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          this.anchorStatsView = { state: 'ready', items };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.anchorStatsView = {
            state: 'error',
            message: this.errorMessage(error, '主播数据加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  loadManagedAnchors(): void {
    this.managedAnchorsLoading = true;
    this.tasks
      .getAllTaskData()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          this.managedAnchorsLoading = false;
          this.managedAnchors = this.managedAnchorOptions(items);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.managedAnchorsLoading = false;
          this.messages.error(
            this.errorMessage(error, '房间管理主播加载失败'),
          );
          this.changeDetector.markForCheck();
        },
      });
  }

  refreshPage(): void {
    this.loadSessions();
    this.loadAnchorStats();
    this.loadHeroReviews(false);
    this.loadRecordedPlayerReviews(false);
  }

  openHeroReviews(): void {
    this.heroReviewVisible = true;
    this.loadHeroReviews();
  }

  loadHeroReviews(showLoading = true): void {
    if (showLoading) {
      this.heroReviewView = { state: 'loading' };
    }
    this.vainglory
      .listHeroReviews()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.heroReviewView = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.heroReviewView = {
            state: 'error',
            message: this.errorMessage(error, '未识别英雄列表加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  unrecognizedPlayers(
    match: VaingloryMatch,
  ): readonly VaingloryMatchPlayer[] {
    return match.players.filter((player) => player.heroId === null);
  }

  heroReviewKey(match: VaingloryMatch, player: VaingloryMatchPlayer): string {
    return `${match.id}:${player.side}:${player.slot}`;
  }

  heroReviewDraft(
    match: VaingloryMatch,
    player: VaingloryMatchPlayer,
  ): number | null {
    return this.heroReviewDrafts.get(this.heroReviewKey(match, player)) ?? null;
  }

  setHeroReviewDraft(
    match: VaingloryMatch,
    player: VaingloryMatchPlayer,
    heroId: number | null,
  ): void {
    const key = this.heroReviewKey(match, player);
    if (heroId === null) {
      this.heroReviewDrafts.delete(key);
    } else {
      this.heroReviewDrafts.set(key, heroId);
    }
  }

  saveHeroReview(match: VaingloryMatch, player: VaingloryMatchPlayer): void {
    const key = this.heroReviewKey(match, player);
    const heroId = this.heroReviewDrafts.get(key);
    if (heroId === undefined || this.savingHeroReviewKey !== null) {
      return;
    }
    this.savingHeroReviewKey = key;
    this.vainglory
      .setPlayerHero(match.id, player.side, player.slot, heroId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (saved) => {
          this.savingHeroReviewKey = null;
          this.heroReviewDrafts.delete(key);
          if (this.heroReviewView.state === 'ready') {
            const resolved = this.unrecognizedPlayers(saved).length === 0;
            this.heroReviewView = {
              state: 'ready',
              total: resolved
                ? Math.max(0, this.heroReviewView.total - 1)
                : this.heroReviewView.total,
              items: resolved
                ? this.heroReviewView.items.filter(
                    (item) => item.id !== saved.id,
                  )
                : this.heroReviewView.items.map((item) =>
                    item.id === saved.id ? saved : item,
                  ),
            };
          }
          if (this.indexSummary !== null) {
            this.indexSummary = {
              ...this.indexSummary,
              recognizedHeroCount: Math.min(
                this.indexSummary.playerSlotCount,
                this.indexSummary.recognizedHeroCount + 1,
              ),
            };
          }
          this.replaceMatchDetails(saved);
          this.messages.success('英雄已人工指定，稿件内容已加入刷新队列');
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.savingHeroReviewKey = null;
          this.messages.error(this.errorMessage(error, '英雄人工指定失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  heroReviewSaving(
    match: VaingloryMatch,
    player: VaingloryMatchPlayer,
  ): boolean {
    return this.savingHeroReviewKey === this.heroReviewKey(match, player);
  }

  heroReviewPositionLabel(player: VaingloryMatchPlayer): string {
    return `${player.side === 'left' ? '左侧' : '右侧'}第 ${player.slot} 行`;
  }

  openRecordedPlayerReviews(): void {
    this.recordedPlayerReviewVisible = true;
    this.loadRecordedPlayerReviews();
  }

  loadRecordedPlayerReviews(showLoading = true): void {
    if (showLoading) {
      this.recordedPlayerReviewView = { state: 'loading' };
    }
    this.vainglory
      .listRecordedPlayerReviews()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.recordedPlayerReviewView = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.recordedPlayerReviewView = {
            state: 'error',
            message: this.errorMessage(error, '主播英雄待确认列表加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  confirmRecordedPlayer(
    match: VaingloryMatch,
    player: VaingloryMatchPlayer,
  ): void {
    if (this.savingRecordedPlayerMatchId !== null) {
      return;
    }
    this.savingRecordedPlayerMatchId = match.id;
    this.savingRecordedPlayerSlot = player.slot;
    this.vainglory
      .setRecordedPlayer(match.id, player.side, player.slot)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (saved) => {
          this.savingRecordedPlayerMatchId = null;
          this.savingRecordedPlayerSlot = null;
          if (this.recordedPlayerReviewView.state === 'ready') {
            this.recordedPlayerReviewView = {
              state: 'ready',
              total: Math.max(0, this.recordedPlayerReviewView.total - 1),
              items: this.recordedPlayerReviewView.items.filter(
                (item) => item.id !== saved.id,
              ),
            };
          }
          this.replaceMatchDetails(saved);
          this.messages.success('主播英雄已人工确认');
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.savingRecordedPlayerMatchId = null;
          this.savingRecordedPlayerSlot = null;
          this.messages.error(
            this.errorMessage(error, '主播英雄人工确认失败'),
          );
          this.changeDetector.markForCheck();
        },
      });
  }

  recordedPlayerCandidates(
    match: VaingloryMatch,
  ): readonly VaingloryMatchPlayer[] {
    const tealSide = match.leftColor === 'teal' ? 'left' : 'right';
    return this.players(match, tealSide);
  }

  recordedPlayer(match: VaingloryMatch): VaingloryMatchPlayer | null {
    return match.players.find((player) => player.isRecordedPlayer) ?? null;
  }

  recordedPlayerSelection(match: VaingloryMatch): string | null {
    const player = this.recordedPlayer(match);
    return player === null ? null : this.recordedPlayerOptionValue(player);
  }

  recordedPlayerOptionValue(player: VaingloryMatchPlayer): string {
    return `${player.side}:${player.slot}`;
  }

  recordedPlayerOptionLabel(player: VaingloryMatchPlayer): string {
    const hero = this.plainHeroName(player);
    const playerName = player.name.trim() || `第 ${player.slot} 行`;
    return `${hero} · ${playerName}`;
  }

  selectRecordedPlayer(match: VaingloryMatch, value: string | null): void {
    if (value === null || value === this.recordedPlayerSelection(match)) {
      return;
    }
    const player = this.recordedPlayerCandidates(match).find(
      (candidate) => this.recordedPlayerOptionValue(candidate) === value,
    );
    if (player !== undefined) {
      this.confirmRecordedPlayer(match, player);
    }
  }

  recordedPlayerStateLabel(match: VaingloryMatch): string {
    const player = this.recordedPlayer(match);
    if (
      (match.recordedPlayerState === 'automatic' ||
        match.recordedPlayerState === 'manual') &&
      player?.heroId === null
    ) {
      return '位置已识别，英雄未识别';
    }
    switch (match.recordedPlayerState) {
      case 'pending':
        return '等待旧数据回填';
      case 'uncertain':
        return '自动判断不明确';
      case 'automatic':
        return match.recordedPlayerConfidence === null
          ? '自动识别'
          : `自动识别 ${Math.round(match.recordedPlayerConfidence * 100)}%`;
      case 'manual':
        return '人工指定';
      case 'unsupported':
        return '当前模式暂未支持';
    }
  }

  recordedPlayerStateColor(match: VaingloryMatch): string {
    switch (match.recordedPlayerState) {
      case 'pending':
        return 'blue';
      case 'uncertain':
        return 'orange';
      case 'automatic':
        return 'cyan';
      case 'manual':
        return 'green';
      case 'unsupported':
        return 'default';
    }
  }

  recordedPlayerSaving(match: VaingloryMatch): boolean {
    return this.savingRecordedPlayerMatchId === match.id;
  }

  private replaceMatchDetails(saved: VaingloryMatch): void {
    const details = this.matchDetails.get(saved.sessionId);
    if (details?.state !== 'ready') {
      return;
    }
    this.matchDetails.set(saved.sessionId, {
      state: 'ready',
      items: details.items.map((match) =>
        match.id === saved.id ? saved : match,
      ),
    });
  }

  loadHeroes(): void {
    this.heroesView = { state: 'loading' };
    this.vainglory
      .listHeroes()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (heroes) => {
          this.heroesView = { state: 'ready', items: heroes };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.heroesView = {
            state: 'error',
            message: this.errorMessage(error, '英雄列表加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  loadScan(showLoading = true): void {
    const sessionId = this.sessionId;
    if (sessionId === null) {
      this.scanView = { state: 'not_selected' };
      return;
    }
    if (showLoading) {
      this.scanView = { state: 'loading' };
    }
    this.vainglory
      .getScan(sessionId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (job) => this.receiveScanJob(job),
        error: (error: unknown) => {
          if (error instanceof HttpErrorResponse && error.status === 404) {
            this.scanView = { state: 'missing' };
          } else {
            this.scanView = {
              state: 'error',
              message: this.errorMessage(error, '分析状态加载失败'),
            };
          }
          this.changeDetector.markForCheck();
        },
      });
  }

  requestScan(): void {
    const sessionId = this.sessionId;
    if (sessionId === null || this.scanRequesting) {
      return;
    }
    this.scanRequesting = true;
    this.vainglory
      .requestScan(sessionId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (job) => {
          this.scanRequesting = false;
          this.receiveScanJob(job);
        },
        error: (error: unknown) => {
          this.scanRequesting = false;
          this.scanView = {
            state: 'error',
            message: this.errorMessage(error, '无法开始分析'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  saveSessionTitle(): void {
    const session = this.selectedSession;
    if (session === null || this.savingSessionTitle) {
      return;
    }
    this.savingSessionTitle = true;
    this.vainglory
      .updateSessionTitle(session.sessionId, this.sessionTitleDraft)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (saved) => {
          this.savingSessionTitle = false;
          this.sessionTitleDraft = saved.title;
          this.replaceSessionSummary(saved);
          this.messages.success('直播标题已保存');
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.savingSessionTitle = false;
          this.messages.error(this.errorMessage(error, '直播标题保存失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  saveSessionAnchor(): void {
    const session = this.selectedSession;
    if (session === null || this.savingSessionAnchor) {
      return;
    }
    const anchorName = this.sessionAnchorDraft.trim();
    if (!anchorName) {
      this.messages.warning('请选择一位主播');
      return;
    }
    this.savingSessionAnchor = true;
    this.vainglory
      .updateSessionAnchor(session.sessionId, anchorName)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (saved) => {
          this.savingSessionAnchor = false;
          this.sessionAnchorDraft = saved.anchorName;
          this.replaceSessionSummary(saved);
          this.loadAnchorStats();
          this.messages.success('主播归属已保存');
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.savingSessionAnchor = false;
          this.messages.error(this.errorMessage(error, '主播归属保存失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  isSessionSelected(sessionId: number): boolean {
    return this.selectedSessionIds.has(sessionId);
  }

  setSessionSelected(sessionId: number, selected: boolean): void {
    if (selected) {
      this.selectedSessionIds.add(sessionId);
    } else {
      this.selectedSessionIds.delete(sessionId);
    }
  }

  currentPageSelected(): boolean {
    return (
      this.sessions.length > 0 &&
      this.sessions.every((session) =>
        this.selectedSessionIds.has(session.sessionId),
      )
    );
  }

  selectCurrentPage(selected: boolean): void {
    for (const session of this.sessions) {
      if (selected) {
        this.selectedSessionIds.add(session.sessionId);
      } else {
        this.selectedSessionIds.delete(session.sessionId);
      }
    }
  }

  bulkSetSessionAnchor(): void {
    if (this.selectedSessionIds.size === 0) {
      this.messages.warning('请先选择至少一场直播');
      return;
    }
    this.bulkAnchorDraft = null;
    this.bulkAnchorModalVisible = true;
  }

  bulkSetStatsIncluded(statsIncluded: boolean): void {
    this.bulkUpdateSelected(statsIncluded ? 'include' : 'exclude', {
      statsIncluded,
    });
  }

  confirmBulkSetSessionAnchor(): void {
    const anchorName = this.bulkAnchorDraft?.trim() ?? '';
    if (!anchorName) {
      return;
    }
    this.bulkUpdateSelected('anchor', { anchorName });
  }

  closeBulkAnchorModal(): void {
    if (this.bulkUpdatingAction === 'anchor') {
      return;
    }
    this.bulkAnchorModalVisible = false;
    this.bulkAnchorDraft = null;
  }

  bulkActionLoading(action: BulkUpdateAction): boolean {
    return this.bulkUpdatingAction === action;
  }

  openArchiveManager(): void {
    this.archiveManagerVisible = true;
    this.archiveAccountsLoading = true;
    this.archiveAccountsError = null;
    this.archiveContentReviewPageIndex = 1;
    this.loadArchiveContentReviews();
    this.accounts
      .listAccounts()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (accounts) => {
          this.archiveAccountsLoading = false;
          this.archiveAccounts = accounts.filter(
            (account) => account.state === 'active',
          );
          for (const account of this.archiveAccounts) {
            this.refreshArchiveSync(account.id);
            this.refreshArchiveItems(account.id);
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveAccountsLoading = false;
          this.archiveAccountsError = this.errorMessage(
            error,
            'B 站账号加载失败',
          );
          this.changeDetector.markForCheck();
        },
      });
  }

  closeArchiveManager(): void {
    this.archiveManagerVisible = false;
    this.changeDetector.markForCheck();
  }

  archiveContentReviewPageChanged(pageIndex: number): void {
    if (pageIndex === this.archiveContentReviewPageIndex) {
      return;
    }
    this.archiveContentReviewPageIndex = pageIndex;
    this.loadArchiveContentReviews();
  }

  requestArchiveSync(account: BiliAccount): void {
    if (this.requestingArchiveAccountIds.has(account.id)) {
      return;
    }
    this.requestingArchiveAccountIds.add(account.id);
    this.vainglory
      .requestArchiveSync(account.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.requestingArchiveAccountIds.delete(account.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (sync) => {
          this.archiveSyncs.set(account.id, sync);
          this.messages.info(`已开始回填 ${account.displayName} 的历史稿件`);
          this.refreshArchiveItems(account.id);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '历史稿件回填启动失败'));
        },
      });
  }

  archiveSyncLabel(sync: VaingloryArchiveSync): string {
    return {
      idle: '尚未开始',
      discovering: '正在发现稿件',
      running: `稿件分析 ${sync.completedCount}/${sync.discoveredCount}`,
      ready: `已分析 ${sync.completedCount} 个稿件`,
      failed: '回填失败',
    }[sync.state];
  }

  archiveItems(accountId: number): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItemsByAccountId.get(accountId) ?? [];
  }

  archiveCurrentItem(
    accountId: number,
  ): VaingloryArchiveBackfillItem | null {
    return this.archiveActiveItems(accountId)[0] ?? null;
  }

  archiveActiveItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItems(accountId)
      .filter((item) => this.archiveItemActive(item))
      .slice(0, 3);
  }

  archiveRecentItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    const activeIds = new Set(
      this.archiveActiveItems(accountId).map((item) => item.id),
    );
    return this.archiveItems(accountId)
      .filter((item) => !activeIds.has(item.id))
      .slice(0, 5);
  }

  archiveStageLabel(item: VaingloryArchiveBackfillItem): string {
    return {
      queued: '等待领取',
      reading_metadata: '读取稿件信息',
      download_pending: '等待下载',
      downloading: '正在下载',
      analysis_pending: '等待分析',
      scanning_video: '扫描视频',
      locating_results: '定位结算画面',
      ocr_recognition: 'OCR 与英雄识别',
      publication_pending: '等待回填新稿件',
      publishing_description: '回填新稿件简介',
      publishing_comments: '发布新稿件战绩评论',
      pinning_comment: '置顶新稿件评论',
      completed: '全部完成',
      managed_elsewhere: '已由录制或迁移流程接管',
      failed: '处理失败',
    }[item.stage];
  }

  analysisQueueWorkerLabel(queue: VaingloryAnalysisQueue): string {
    if (queue.workerState === 'failed') {
      return '分析服务异常';
    }
    if (queue.workerState === 'stopped') {
      return '分析服务未运行';
    }
    if (queue.active.length > 0) {
      return '正在运行';
    }
    return queue.pendingCount > 0 ? '准备领取任务' : '空闲';
  }

  analysisQueueCategoryLabel(
    category: VaingloryAnalysisQueueCategory,
  ): string {
    return {
      manual: '手动任务',
      realtime: '当天直播',
      archive: '历史稿件接入',
      migration: '稿件迁移',
      backlog: '旧数据重扫',
    }[category];
  }

  analysisQueueStageLabel(item: VaingloryAnalysisQueueItem): string {
    if (item.state === 'pending') {
      return '等待扫描视频';
    }
    if (item.stage === 'ocr_waiting') {
      return '已定位结算画面，等待 OCR';
    }
    if (item.stage === 'ocr_recognition') {
      return 'OCR 与英雄识别';
    }
    if (item.progress < 0.42) {
      return '粗扫视频';
    }
    return '定位结算画面';
  }

  analysisQueuePercent(item: VaingloryAnalysisQueueItem): number {
    return Math.max(0, Math.min(100, Math.round(item.progress * 100)));
  }

  heroRecognitionPercent(summary: VaingloryIndexSummary): number {
    if (summary.playerSlotCount === 0) {
      return 0;
    }
    return Math.round(
      (summary.recognizedHeroCount / summary.playerSlotCount) * 100,
    );
  }

  archiveItemPercent(item: VaingloryArchiveBackfillItem): number {
    switch (item.stage) {
      case 'queued':
      case 'reading_metadata':
        return 0;
      case 'download_pending':
        return 5;
      case 'downloading':
        return Math.round(5 + item.downloadProgress * 25);
      case 'analysis_pending':
        return 30;
      case 'scanning_video':
      case 'locating_results':
      case 'ocr_recognition':
        return Math.round(30 + item.analysisProgress * 45);
      case 'publication_pending':
        return 75;
      case 'publishing_description':
      case 'publishing_comments':
      case 'pinning_comment':
        return Math.round(75 + item.publicationProgress * 25);
      case 'completed':
      case 'managed_elsewhere':
        return 100;
      case 'failed':
        return Math.round(item.progress * 100);
    }
  }

  archiveDownloadLabel(item: VaingloryArchiveBackfillItem): string {
    const size = this.archiveDownloadSize(item);
    if (item.downloadProgress >= 0.999 || this.archiveStageAfterDownload(item)) {
      return size ? `已完成 · ${size}` : '已完成';
    }
    if (item.stage === 'downloading') {
      return `${Math.round(item.downloadProgress * 100)}%${
        size ? ` · ${size}` : ''
      }`;
    }
    return '等待';
  }

  archiveAnalysisLabel(item: VaingloryArchiveBackfillItem): string {
    if (item.analysisState === 'ready' || this.archiveStageAfterAnalysis(item)) {
      return '已完成';
    }
    if (item.stage === 'scanning_video') {
      return `粗扫 ${Math.round((item.analysisProgress / 0.45) * 100)}%`;
    }
    if (item.stage === 'locating_results') {
      return `定位 ${Math.round(
        ((item.analysisProgress - 0.45) / 0.25) * 100,
      )}%`;
    }
    if (item.stage === 'ocr_recognition') {
      return '结算画面已定位';
    }
    return item.stage === 'analysis_pending' ? '等待执行' : '等待';
  }

  archiveOcrLabel(item: VaingloryArchiveBackfillItem): string {
    if (item.analysisState === 'ready' || this.archiveStageAfterAnalysis(item)) {
      return '已完成';
    }
    if (item.stage === 'ocr_recognition') {
      return `${Math.max(
        0,
        Math.min(
          100,
          Math.round(((item.analysisProgress - 0.7) / 0.3) * 100),
        ),
      )}%`;
    }
    return '等待';
  }

  archivePublicationLabel(item: VaingloryArchiveBackfillItem): string {
    if (item.stage === 'managed_elsewhere') {
      return '由原录制/迁移任务处理';
    }
    if (item.stage === 'completed') {
      return item.matchCount > 0 ? '简介、评论和置顶已完成' : '未发现对局，无需回填';
    }
    if (item.stage === 'publishing_description') {
      return '正在更新简介';
    }
    if (item.stage === 'publishing_comments') {
      return `评论 ${item.confirmedCommentCount}/${item.commentCount}`;
    }
    if (item.stage === 'pinning_comment') {
      return '正在置顶评论';
    }
    return item.stage === 'publication_pending' ? '等待发布' : '等待';
  }

  archiveDailyLimit(accountId: number, sync: VaingloryArchiveSync): number {
    return this.archiveDailyLimitDrafts.get(accountId) ?? sync.dailyLimit;
  }

  setArchiveDailyLimit(accountId: number, value: number): void {
    this.archiveDailyLimitDrafts.set(accountId, Number(value));
  }

  updateArchiveSyncControl(
    account: BiliAccount,
    sync: VaingloryArchiveSync,
    paused?: boolean,
  ): void {
    if (this.requestingArchiveAccountIds.has(account.id)) {
      return;
    }
    const dailyLimit = this.archiveDailyLimit(account.id, sync);
    if (!Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 500) {
      this.messages.error('每日处理上限必须是 1 到 500 的整数');
      return;
    }
    this.requestingArchiveAccountIds.add(account.id);
    this.vainglory
      .updateArchiveSync(account.id, { paused, dailyLimit })
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.requestingArchiveAccountIds.delete(account.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (updated) => {
          this.archiveDailyLimitDrafts.delete(account.id);
          this.archiveSyncs.set(account.id, updated);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '历史回填控制失败'));
        },
      });
  }

  archiveSyncPercent(sync: VaingloryArchiveSync): number {
    return Math.max(0, Math.min(100, Math.round(sync.progress * 100)));
  }

  archiveSyncActive(sync: VaingloryArchiveSync): boolean {
    return sync.state === 'discovering' || sync.state === 'running';
  }

  private refreshArchiveSync(accountId: number): void {
    this.vainglory
      .getArchiveSync(accountId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (sync) => {
          this.archiveSyncs.set(accountId, sync);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (!(error instanceof HttpErrorResponse && error.status === 404)) {
            this.messages.warning(
              this.errorMessage(error, '历史回填状态读取失败'),
            );
          }
        },
      });
  }

  private refreshArchiveItems(accountId: number): void {
    this.vainglory
      .listArchiveSyncItems(accountId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          const updated = new Map(this.archiveItemsByAccountId);
          updated.set(accountId, items);
          this.archiveItemsByAccountId = updated;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (!(error instanceof HttpErrorResponse && error.status === 404)) {
            this.messages.warning(
              this.errorMessage(error, '历史稿件处理进度读取失败'),
            );
          }
        },
      });
  }

  private refreshArchiveManager(): void {
    for (const account of this.archiveAccounts) {
      this.refreshArchiveSync(account.id);
      this.refreshArchiveItems(account.id);
    }
    this.loadArchiveContentReviews();
  }

  private applyArchiveBackfillSnapshot(data: unknown): void {
    const snapshot = this.archiveBackfillSnapshot(data);
    if (snapshot === null) {
      if (this.archiveManagerVisible) {
        this.refreshArchiveManager();
      }
      return;
    }
    this.archiveSyncs.clear();
    for (const sync of snapshot.syncs) {
      this.archiveSyncs.set(sync.accountId, sync);
    }
    const items = new Map<number, readonly VaingloryArchiveBackfillItem[]>();
    for (const sync of snapshot.syncs) {
      items.set(sync.accountId, snapshot.items[String(sync.accountId)] ?? []);
    }
    this.archiveItemsByAccountId = items;
    this.changeDetector.markForCheck();
  }

  private applyVaingloryIndexSnapshot(data: unknown): void {
    const snapshot = this.vaingloryIndexSnapshot(data);
    if (snapshot === null) {
      return;
    }
    this.analysisQueue = snapshot.analysisQueue;
    this.indexSummary = snapshot.indexSummary;
    this.indexSampledAt = snapshot.sampledAt;
    this.changeDetector.markForCheck();
  }

  private archiveBackfillSnapshot(
    data: unknown,
  ): VaingloryArchiveBackfillRealtimeSnapshot | null {
    if (typeof data !== 'object' || data === null) {
      return null;
    }
    const syncs = Reflect.get(data, 'syncs');
    const items = Reflect.get(data, 'items');
    if (
      !Array.isArray(syncs) ||
      typeof items !== 'object' ||
      items === null ||
      Array.isArray(items)
    ) {
      return null;
    }
    return {
      syncs: syncs as VaingloryArchiveSync[],
      items: items as Record<string, VaingloryArchiveBackfillItem[]>,
    };
  }

  private vaingloryIndexSnapshot(
    data: unknown,
  ): VaingloryIndexRealtimeSnapshot | null {
    if (typeof data !== 'object' || data === null) {
      return null;
    }
    const sampledAt = Reflect.get(data, 'sampledAt');
    const analysisQueue = Reflect.get(data, 'analysisQueue');
    const indexSummary = Reflect.get(data, 'indexSummary');
    if (
      typeof sampledAt !== 'number' ||
      typeof analysisQueue !== 'object' ||
      analysisQueue === null ||
      Array.isArray(analysisQueue) ||
      typeof indexSummary !== 'object' ||
      indexSummary === null ||
      Array.isArray(indexSummary)
    ) {
      return null;
    }
    return {
      sampledAt,
      analysisQueue: analysisQueue as VaingloryAnalysisQueue,
      indexSummary: indexSummary as VaingloryIndexSummary,
    };
  }

  private archiveItemActive(item: VaingloryArchiveBackfillItem): boolean {
    return !['completed', 'managed_elsewhere', 'failed'].includes(item.stage);
  }

  private archiveStageAfterDownload(item: VaingloryArchiveBackfillItem): boolean {
    return [
      'analysis_pending',
      'scanning_video',
      'locating_results',
      'ocr_recognition',
      'publication_pending',
      'publishing_description',
      'publishing_comments',
      'pinning_comment',
      'completed',
    ].includes(item.stage);
  }

  private archiveStageAfterAnalysis(item: VaingloryArchiveBackfillItem): boolean {
    return [
      'publication_pending',
      'publishing_description',
      'publishing_comments',
      'pinning_comment',
      'completed',
    ].includes(item.stage);
  }

  private archiveDownloadSize(item: VaingloryArchiveBackfillItem): string {
    if (item.downloadedBytes <= 0 && item.totalBytes === null) {
      return '';
    }
    const downloaded = this.formatBytes(item.downloadedBytes);
    return item.totalBytes === null
      ? downloaded
      : `${downloaded}/${this.formatBytes(item.totalBytes)}`;
  }

  private formatBytes(value: number): string {
    const units = ['B', 'KB', 'MB', 'GB'];
    let normalized = Math.max(0, value);
    let unit = 0;
    while (normalized >= 1024 && unit < units.length - 1) {
      normalized /= 1024;
      unit += 1;
    }
    return `${normalized.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  private loadArchiveContentReviews(): void {
    this.archiveContentReviewsLoading = true;
    this.archiveContentReviewsError = null;
    this.vainglory
      .listArchiveContentReviews(
        this.archiveContentReviewPageSize,
        (this.archiveContentReviewPageIndex - 1) *
          this.archiveContentReviewPageSize,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (page) => {
          this.archiveContentReviewsLoading = false;
          this.archiveContentReviewTotal = page.total;
          this.archiveContentReviews = page.items;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveContentReviewsLoading = false;
          this.archiveContentReviewsError = this.errorMessage(
            error,
            '疑似非虚荣稿件列表加载失败',
          );
          this.changeDetector.markForCheck();
        },
      });
  }

  openMatch(match: VaingloryMatch, atResult = false): void {
    this.openMatchMedia(match, atResult ? 'result' : 'play');
  }

  openMatchClip(match: VaingloryMatch): void {
    this.openMatchMedia(match, 'clip');
  }

  remoteMediaStatus(match: VaingloryMatch): RemoteMediaStatus | null {
    return this.remoteMediaStatuses.get(match.partId) ?? null;
  }

  remoteMediaActive(status: RemoteMediaStatus): boolean {
    return status.state === 'pending' || status.state === 'downloading';
  }

  remoteMediaPercent(partId: number): number {
    const progress = this.remoteMediaStatuses.get(partId)?.progress ?? 0;
    return Math.max(0, Math.min(100, Math.round(progress * 100)));
  }

  matchMediaReady(match: VaingloryMatch): boolean {
    const part = this.recordingParts.get(match.partId);
    if (part?.sourceExists || part?.finalExists) {
      return true;
    }
    const state = this.remoteMediaStatuses.get(match.partId)?.state;
    return state === 'local' || state === 'ready';
  }

  matchMediaOpening(
    match: VaingloryMatch,
    action: 'play' | 'clip' | 'download',
  ): boolean {
    return this.previewOpeningKey === `${action}:${match.id}`;
  }

  downloadMatchMedia(match: VaingloryMatch): void {
    const status = this.remoteMediaStatuses.get(match.partId);
    if (
      match.partId <= 0 ||
      this.previewOpeningKey !== null ||
      status === undefined ||
      !status.remoteAvailable ||
      this.remoteMediaActive(status) ||
      this.matchMediaReady(match)
    ) {
      return;
    }
    this.previewOpeningKey = `download:${match.id}`;
    this.recordingSessions
      .requestRemoteMedia(match.partId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (updated) => {
          this.previewOpeningKey = null;
          this.remoteMediaStatuses.set(match.partId, updated);
          if (this.remoteMediaActive(updated)) {
            this.messages.info('视频已开始后台下载');
            this.startRemoteMediaPolling(match.partId);
          } else if (updated.state === 'ready' || updated.state === 'local') {
            this.messages.success('视频已经可以播放和剪辑');
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.previewOpeningKey = null;
          this.messages.error(this.errorMessage(error, '录像下载失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  private openMatchMedia(
    match: VaingloryMatch,
    action: 'play' | 'result' | 'clip',
  ): void {
    if (match.partId <= 0 || this.previewOpeningKey !== null) {
      return;
    }
    if (!this.matchMediaReady(match)) {
      this.messages.info('本地视频不可用，请先点击“下载视频”');
      return;
    }
    this.previewOpeningKey = `${action === 'clip' ? 'clip' : 'play'}:${match.id}`;
    this.recordingSessions
      .getSession(match.sessionId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (session) => {
          const part = session.parts.find((item) => item.id === match.partId);
          if (part === undefined) {
            this.previewOpeningKey = null;
            this.messages.error('结算画面所在的分 P 已不可用');
            this.changeDetector.markForCheck();
            return;
          }
          if (!this.matchMediaAvailable(part)) {
            this.previewOpeningKey = null;
            this.messages.info('本地视频不可用，请先点击“下载视频”');
            this.changeDetector.markForCheck();
            return;
          }
          this.previewOpeningKey = null;
          if (action === 'clip') {
            void this.router.navigate(
              ['/recordings/highlights', String(match.sessionId)],
              {
                queryParams: {
                  partId: match.partId,
                  seekMs: match.startedAtMs,
                },
              },
            );
            this.changeDetector.markForCheck();
            return;
          }
          this.previewSession = session;
          this.previewPart = part;
          this.previewSeekSeconds =
            (action === 'result' ? match.resultAtMs : match.startedAtMs) /
            1_000;
          this.previewVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.previewOpeningKey = null;
          this.messages.error(this.errorMessage(error, '录像打开失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  private matchMediaAvailable(part: RecordingPart): boolean {
    if (part.sourceExists || part.finalExists) {
      return true;
    }
    const state = this.remoteMediaStatuses.get(part.id)?.state;
    return state === 'local' || state === 'ready';
  }

  private loadMatchMediaStates(
    sessionId: number,
    matches: readonly VaingloryMatch[],
  ): void {
    this.recordingSessions
      .getSession(sessionId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (session) => {
          const partIds = new Set(matches.map((match) => match.partId));
          for (const part of session.parts) {
            if (!partIds.has(part.id)) {
              continue;
            }
            this.recordingParts.set(part.id, part);
            if (part.sourceExists || part.finalExists) {
              continue;
            }
            this.recordingSessions
              .getRemoteMediaStatus(part.id)
              .pipe(takeUntil(this.destroy$))
              .subscribe({
                next: (status) => {
                  this.remoteMediaStatuses.set(part.id, status);
                  if (this.remoteMediaActive(status)) {
                    this.startRemoteMediaPolling(part.id);
                  }
                  this.changeDetector.markForCheck();
                },
                error: () => this.changeDetector.markForCheck(),
              });
          }
          this.changeDetector.markForCheck();
        },
        error: () => this.changeDetector.markForCheck(),
      });
  }

  private startRemoteMediaPolling(partId: number): void {
    if (this.remoteMediaPollingPartIds.has(partId)) {
      return;
    }
    this.remoteMediaPollingPartIds.add(partId);
    timer(1_000, 1_000)
      .pipe(
        switchMap(() => this.recordingSessions.getRemoteMediaStatus(partId)),
        takeWhile((status) => this.remoteMediaActive(status), true),
        takeUntil(this.destroy$),
        finalize(() => this.remoteMediaPollingPartIds.delete(partId)),
      )
      .subscribe({
        next: (status) => {
          this.remoteMediaStatuses.set(partId, status);
          if (status.state === 'ready' || status.state === 'local') {
            this.messages.success('视频下载完成，可以播放和剪辑了');
          } else if (status.state === 'failed') {
            this.messages.error(status.error || '录像下载失败');
          }
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.messages.warning('暂时无法读取下载进度，后台下载仍会继续');
          this.changeDetector.markForCheck();
        },
      });
  }

  biliPlaybackUrl(match: VaingloryMatch): string | null {
    if (!match.bvid || match.archivePage === null) {
      return null;
    }
    const seconds = Math.max(0, Math.floor(match.startedAtMs / 1_000));
    return `https://www.bilibili.com/video/${match.bvid}?p=${match.archivePage}&t=${seconds}`;
  }

  sessionBiliUrl(session: VaingloryMatchSession): string | null {
    return session.bvid
      ? `https://www.bilibili.com/video/${session.bvid}`
      : null;
  }

  gameModeLabel(mode: GameMode): string {
    return {
      '3v3': '3V3',
      '5v5': '5V5',
      aram: '大乱斗 / 天赋模式',
      other: '其他',
      unknown: '模式待识别',
    }[mode];
  }

  sessionModes(session: VaingloryMatchSession): string {
    return session.gameModes
      .map((mode) => this.gameModeLabel(mode))
      .join(' / ');
  }

  closePreview(): void {
    this.previewVisible = false;
    this.previewSeekSeconds = null;
    this.changeDetector.markForCheck();
  }

  players(
    match: VaingloryMatch,
    side: 'left' | 'right',
  ): readonly VaingloryMatchPlayer[] {
    return match.players.filter((player) => player.side === side);
  }

  heroThumbnail(player: VaingloryMatchPlayer): string | null {
    if (player.heroId === null) {
      return null;
    }
    return (
      this.heroes.find((hero) => hero.id === player.heroId)?.thumbnailUrl ??
      null
    );
  }

  heroName(player: VaingloryMatchPlayer): string {
    const name = this.plainHeroName(player);
    return player.isRecordedPlayer ? `[${name}]` : name;
  }

  plainHeroName(player: VaingloryMatchPlayer): string {
    return player.heroLabel
      ? player.heroLabel
      : player.heroId === null
        ? '未识别英雄'
        : `英雄 #${player.heroId}`;
  }

  teamColor(match: VaingloryMatch, side: 'left' | 'right'): TeamColor {
    return side === 'left' ? match.leftColor : match.rightColor;
  }

  teamKills(match: VaingloryMatch, side: 'left' | 'right'): number | null {
    return side === 'left' ? match.leftKills : match.rightKills;
  }

  teamEconomy(match: VaingloryMatch, side: 'left' | 'right'): number | null {
    return side === 'left' ? match.leftEconomy : match.rightEconomy;
  }

  teamLabel(color: TeamColor): string {
    return color === 'teal' ? '主播队' : '对手队';
  }

  winnerLabel(match: VaingloryMatch): string {
    return match.winnerColor === 'unknown'
      ? '胜负待核对'
      : match.winnerColor === 'teal'
        ? '主播获胜'
        : '主播失败';
  }

  scanStateLabel(job: VaingloryScanJob): string {
    return {
      pending: '等待分析',
      analyzing: '正在分析',
      ready: `已识别 ${job.matchCount} 局`,
      failed: '分析失败',
    }[job.state];
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) {
      return '—';
    }
    const wholeSeconds = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(wholeSeconds / 60);
    return `${minutes}:${(wholeSeconds % 60).toString().padStart(2, '0')}`;
  }

  formatEconomy(value: number | null): string {
    if (value === null) {
      return '—';
    }
    return value >= 1_000
      ? `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}k`
      : value.toString();
  }

  formatKda(player: VaingloryMatchPlayer): string {
    return [player.kills, player.deaths, player.assists]
      .map((value) => value ?? '—')
      .join('/');
  }

  trackMatch(_index: number, match: VaingloryMatch): number {
    return match.id;
  }

  trackSession(_index: number, session: VaingloryMatchSession): number {
    return session.sessionId;
  }

  trackHero(_index: number, hero: VaingloryHero): number {
    return hero.id;
  }

  trackAnchorStats(_index: number, stats: VaingloryAnchorStats): string {
    return stats.anchorUid === null
      ? `${stats.anchorName}:${stats.roomId}`
      : `uid:${stats.anchorUid}`;
  }

  trackPlayer(_index: number, player: VaingloryMatchPlayer): string {
    return `${player.side}:${player.slot}`;
  }

  private replaceSessionSummary(saved: VaingloryMatchSession): void {
    this.selectedSession = saved;
    if (this.sessionsView.state !== 'ready') {
      return;
    }
    this.sessionsView = {
      state: 'ready',
      total: this.sessionsView.total,
      items: this.sessionsView.items.map((item) =>
        item.sessionId === saved.sessionId ? saved : item,
      ),
    };
  }

  private bulkUpdateSelected(
    action: BulkUpdateAction,
    update: {
      readonly anchorName?: string;
      readonly statsIncluded?: boolean;
    },
  ): void {
    if (this.bulkUpdatingAction !== null) {
      return;
    }
    const sessionIds = [...this.selectedSessionIds];
    if (sessionIds.length === 0) {
      this.messages.warning('请先选择至少一场直播');
      return;
    }
    this.bulkUpdatingAction = action;
    this.vainglory
      .bulkUpdateSessions(sessionIds, update)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (result) => {
          this.bulkUpdatingAction = null;
          if (action === 'anchor') {
            this.bulkAnchorModalVisible = false;
            this.bulkAnchorDraft = null;
          }
          this.selectedSessionIds.clear();
          this.loadSessions();
          this.loadAnchorStats();
          this.messages.success(`已更新 ${result.updatedCount} 场直播`);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.bulkUpdatingAction = null;
          this.messages.error(this.errorMessage(error, '批量修改失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  private managedAnchorOptions(
    items: readonly TaskData[],
  ): readonly ManagedAnchorOption[] {
    const byIdentity = new Map<string, ManagedAnchorOption>();
    for (const item of items) {
      const anchorName = item.user_info.name.trim();
      const roomId = item.room_info.room_id;
      const anchorUid = item.user_info.uid;
      if (!anchorName || roomId <= 0) {
        continue;
      }
      const key = anchorUid > 0 ? `uid:${anchorUid}` : `room:${roomId}`;
      byIdentity.set(key, {
        anchorName,
        roomId,
        anchorUid,
        label: `${anchorName}（房间 ${roomId}）`,
      });
    }
    return [...byIdentity.values()].sort((left, right) =>
      left.anchorName.localeCompare(right.anchorName, 'zh-CN'),
    );
  }

  private receiveScanJob(job: VaingloryScanJob): void {
    if (job.sessionId !== this.sessionId) {
      return;
    }
    this.scanView = { state: 'job', job };
    if (job.state === 'pending' || job.state === 'analyzing') {
      this.scheduleScanPoll();
    } else {
      this.stopScanPolling();
      if (job.state === 'ready') {
        this.loadHeroes();
        this.loadSessions();
      }
    }
    this.changeDetector.markForCheck();
  }

  private scheduleScanPoll(): void {
    this.stopScanPolling();
    this.scanPollTimer = window.setTimeout(() => {
      this.scanPollTimer = null;
      this.loadScan(false);
    }, 3_000);
  }

  private stopScanPolling(): void {
    if (this.scanPollTimer !== null) {
      window.clearTimeout(this.scanPollTimer);
      this.scanPollTimer = null;
    }
  }

  private errorMessage(error: unknown, fallback: string): string {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail;
      if (typeof detail === 'string' && detail.trim()) {
        return detail;
      }
    }
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
