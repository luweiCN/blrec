import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  HostListener,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { NzMessageService } from 'ng-zorro-antd/message';
import { from, of, Subject, timer } from 'rxjs';
import {
  catchError,
  concatMap,
  finalize,
  map,
  switchMap,
  takeUntil,
  takeWhile,
  toArray,
} from 'rxjs/operators';

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
  MatchEndReason,
  MatchKind,
  TeamColor,
  VaingloryAnalysisQueue,
  VaingloryAnalysisWorkerNodeStatus,
  VaingloryAnchorStats,
  VaingloryArchiveBackfillItem,
  VaingloryArchiveBackfillRealtimeSnapshot,
  VaingloryArchiveSync,
  VaingloryHero,
  VaingloryIndexRealtimeSnapshot,
  VaingloryIndexSummary,
  VaingloryMatch,
  VaingloryMatchFilters,
  VaingloryMatchPlayer,
  VaingloryMatchSession,
  VaingloryHeroStats,
  VaingloryMatchSessionSort,
  VaingloryPlayer,
  VaingloryPlayerStats,
  VaingloryPublicationRecommendedAction,
  VaingloryPublicationRetryStep,
  VaingloryPublicationStatus,
  VaingloryScanJob,
  VaingloryZeroMatchSession,
  ViewContext,
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

type ZeroMatchSessionsView =
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly items: readonly VaingloryZeroMatchSession[];
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

type PlayersView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryPlayer[] }
  | { readonly state: 'error'; readonly message: string };

type PlayerStatsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryPlayerStats[] }
  | { readonly state: 'error'; readonly message: string };

type HeroStatsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly items: readonly VaingloryHeroStats[] }
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

type ManagedRoomOption = ManagedAnchorOption;

type BulkUpdateAction = 'anchor' | 'include' | 'exclude' | 'rescan';

type BulkRescanResult =
  | {
      readonly state: 'queued';
      readonly sessionId: number;
      readonly job: VaingloryScanJob;
    }
  | {
      readonly state: 'failed';
      readonly sessionId: number;
      readonly error: unknown;
    };

interface MatchPlayerEditDraft {
  side: 'left' | 'right';
  slot: number;
  name: string;
  heroId: number | null;
  kills: number | null;
  deaths: number | null;
  assists: number | null;
  economy: number | null;
  lastHits: number | null;
}

interface MatchEditDraft {
  title: string;
  gameMode: GameMode;
  durationSeconds: number | null;
  resultText: string;
  endReason: MatchEndReason;
  winnerColor: TeamColor | 'unknown';
  matchKind: MatchKind;
  viewContext: ViewContext;
  statsEligible: boolean;
  leftKills: number | null;
  rightKills: number | null;
  leftEconomy: number | null;
  rightEconomy: number | null;
  players: MatchPlayerEditDraft[];
}

interface AnalysisImageRequest {
  readonly sessionId: number;
  readonly partId?: number;
  readonly title: string;
}

@Component({
  selector: 'app-vainglory',
  templateUrl: './vainglory.component.html',
  styleUrls: [
    './vainglory.component.scss',
    './vainglory-editor.component.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class VaingloryComponent implements OnInit, OnDestroy {
  sessionsView: SessionsView = { state: 'loading' };
  zeroMatchSessionsView: ZeroMatchSessionsView = { state: 'loading' };
  suppressedZeroMatchSessionsView: ZeroMatchSessionsView = {
    state: 'loading',
  };
  heroesView: HeroesView = { state: 'loading' };
  anchorStatsView: AnchorStatsView = { state: 'loading' };
  playersView: PlayersView = { state: 'loading' };
  playerStatsView: PlayerStatsView = { state: 'loading' };
  heroStatsView: HeroStatsView = { state: 'loading' };
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
  sessionSort: VaingloryMatchSessionSort = 'analyzed';
  sessionId: number | null = null;
  pageIndex = 1;
  readonly pageSize = 20;
  readonly selectedSessionIds = new Set<number>();
  readonly rescanningSessionIds = new Set<number>();
  readonly updatingScanSuppressionSessionIds = new Set<number>();
  readonly retryingPublicationSteps = new Set<string>();
  readonly savingMatchIds = new Set<number>();
  readonly rerunningMatchIds = new Set<number>();
  readonly deletingMatchIds = new Set<number>();
  readonly ignoringReviewKeys = new Set<string>();
  matchEditorVisible = false;
  editingMatch: VaingloryMatch | null = null;
  matchEditDraft: MatchEditDraft | null = null;
  savingMatchEdit = false;
  manualMarkerVisible = false;
  manualMarkerSession: VaingloryZeroMatchSession | null = null;
  manualMarkerPartIndex = 1;
  manualMarkerTime = '';
  savingManualMarker = false;
  bulkAnchorDraft: string | null = null;
  bulkAnchorModalVisible = false;
  bulkUpdatingAction: BulkUpdateAction | null = null;
  managedAnchors: readonly ManagedAnchorOption[] = [];
  managedAnchorsLoading = false;

  playerSearch = '';
  newPlayerName = '';
  creatingPlayer = false;
  syncingManagedRooms = false;
  readonly playerNameDrafts = new Map<number, string>();
  readonly playerRoomDrafts = new Map<number, string>();
  readonly savingPlayerIds = new Set<number>();
  managedRooms: readonly ManagedRoomOption[] = [];
  heroStatsGameMode: GameMode | '' = '3v3';

  heroReviewVisible = false;
  heroReviewView: HeroReviewView = { state: 'idle' };
  readonly heroReviewDrafts = new Map<string, number>();
  savingHeroReviewKey: string | null = null;
  recordedPlayerReviewVisible = false;
  recordedPlayerReviewView: RecordedPlayerReviewView = { state: 'idle' };
  savingRecordedPlayerMatchId: number | null = null;
  savingRecordedPlayerSlot: number | null = null;
  analysisTaskModalVisible = false;
  analysisTaskModalView: 'workers' | 'tasks' = 'tasks';
  analysisWorkerEditorVisible = false;
  analysisWorkerEditorMode: 'add' | 'edit' = 'add';
  analysisWorkerIdDraft = '';
  analysisWorkerNameDraft = '';
  savingAnalysisWorker = false;
  updatingAnalysisWorkerIds: ReadonlySet<string> = new Set<string>();
  analysisImageBrowserVisible = false;
  analysisImageBrowserTitle = '';
  analysisImageBrowserLoading = false;
  analysisImageBrowserError: string | null = null;
  analysisImageBrowserItems: readonly VaingloryMatch[] = [];
  analysisImageBrowserIndex = 0;
  zeroMatchReviewVisible = false;
  zeroMatchPageIndex = 1;
  readonly zeroMatchPageSize = 20;
  suppressedZeroMatchPageIndex = 1;
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
  private listRefreshSignature: string | null = null;
  private analysisImageRequestGeneration = 0;

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
    this.loadPlayerStats();
    this.loadHeroStats();
    this.loadManagedAnchors();
    this.loadHeroReviews(false);
    this.loadRecordedPlayerReviews(false);
    this.loadZeroMatchSessions();
    this.realtime.events$.pipe(takeUntil(this.destroy$)).subscribe((event) => {
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

  get zeroMatchSessions(): readonly VaingloryZeroMatchSession[] {
    return this.zeroMatchSessionsView.state === 'ready'
      ? this.zeroMatchSessionsView.items
      : [];
  }

  get zeroMatchSessionTotal(): number {
    return this.zeroMatchSessionsView.state === 'ready'
      ? this.zeroMatchSessionsView.total
      : 0;
  }

  get suppressedZeroMatchSessions(): readonly VaingloryZeroMatchSession[] {
    return this.suppressedZeroMatchSessionsView.state === 'ready'
      ? this.suppressedZeroMatchSessionsView.items
      : [];
  }

  get suppressedZeroMatchSessionTotal(): number {
    return this.suppressedZeroMatchSessionsView.state === 'ready'
      ? this.suppressedZeroMatchSessionsView.total
      : 0;
  }

  get heroes(): readonly VaingloryHero[] {
    return this.heroesView.state === 'ready' ? this.heroesView.items : [];
  }

  get anchorStats(): readonly VaingloryAnchorStats[] {
    return this.anchorStatsView.state === 'ready'
      ? this.anchorStatsView.items
      : [];
  }

  get playerLibrary(): readonly VaingloryPlayer[] {
    return this.playersView.state === 'ready' ? this.playersView.items : [];
  }

  get filteredPlayerLibrary(): readonly VaingloryPlayer[] {
    const query = this.playerSearch.trim().toLocaleLowerCase('zh-CN');
    if (!query) {
      return this.playerLibrary;
    }
    return this.playerLibrary.filter((player) =>
      [
        player.name,
        ...player.rooms.flatMap((room) => [
          String(room.roomId),
          room.anchorName,
        ]),
      ].some((value) => value.toLocaleLowerCase('zh-CN').includes(query)),
    );
  }

  get playerStats(): readonly VaingloryPlayerStats[] {
    return this.playerStatsView.state === 'ready'
      ? this.playerStatsView.items
      : [];
  }

  get heroStats(): readonly VaingloryHeroStats[] {
    return this.heroStatsView.state === 'ready' ? this.heroStatsView.items : [];
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
      this.indexSummary.playerSlotCount - this.indexSummary.recognizedHeroCount,
    );
  }

  get selectedDetails(): MatchDetailsView | null {
    const sessionId = this.selectedSession?.sessionId;
    return sessionId === undefined
      ? null
      : (this.matchDetails.get(sessionId) ?? null);
  }

  get currentAnalysisImage(): VaingloryMatch | null {
    return (
      this.analysisImageBrowserItems[this.analysisImageBrowserIndex] ?? null
    );
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

  toggleSessionSort(): void {
    this.sessionSort = this.sessionSort === 'analyzed' ? 'started' : 'analyzed';
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

  loadSessions(background = false): void {
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
    if (!background) {
      this.sessionsView = { state: 'loading' };
      this.detailsDrawerVisible = false;
      this.selectedSession = null;
      this.matchDetails.clear();
      this.recordingParts.clear();
      this.remoteMediaStatuses.clear();
    }
    this.vainglory
      .listMatchSessions(
        filters,
        this.pageSize,
        (this.pageIndex - 1) * this.pageSize,
        this.sessionSort,
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
            !background &&
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
          if (background && this.selectedSession !== null) {
            this.selectedSession =
              response.items.find(
                (item) => item.sessionId === this.selectedSession?.sessionId,
              ) ?? this.selectedSession;
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (background) {
            return;
          }
          this.sessionsView = {
            state: 'error',
            message: this.errorMessage(error, '直播场次加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  openZeroMatchReviews(): void {
    this.zeroMatchReviewVisible = true;
    this.loadZeroMatchSessions();
    this.loadSuppressedZeroMatchSessions();
  }

  zeroMatchPageChanged(pageIndex: number): void {
    if (pageIndex === this.zeroMatchPageIndex) {
      return;
    }
    this.zeroMatchPageIndex = pageIndex;
    this.loadZeroMatchSessions();
  }

  loadZeroMatchSessions(background = false): void {
    if (!background) {
      this.zeroMatchSessionsView = { state: 'loading' };
    }
    this.vainglory
      .listZeroMatchSessions(
        this.zeroMatchPageSize,
        (this.zeroMatchPageIndex - 1) * this.zeroMatchPageSize,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.zeroMatchSessionsView = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (background) {
            return;
          }
          this.zeroMatchSessionsView = {
            state: 'error',
            message: this.errorMessage(error, '0 局直播加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  suppressedZeroMatchPageChanged(pageIndex: number): void {
    if (pageIndex === this.suppressedZeroMatchPageIndex) {
      return;
    }
    this.suppressedZeroMatchPageIndex = pageIndex;
    this.loadSuppressedZeroMatchSessions();
  }

  loadSuppressedZeroMatchSessions(): void {
    this.suppressedZeroMatchSessionsView = { state: 'loading' };
    this.vainglory
      .listZeroMatchSessions(
        this.zeroMatchPageSize,
        (this.suppressedZeroMatchPageIndex - 1) * this.zeroMatchPageSize,
        true,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.suppressedZeroMatchSessionsView = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.suppressedZeroMatchSessionsView = {
            state: 'error',
            message: this.errorMessage(error, '已确认直播加载失败'),
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

  openAnalysisSessionDetails(sessionId: number): void {
    this.analysisTaskModalVisible = false;
    this.playerName = '';
    this.sourceTitle = '';
    this.anchorNameFilter = null;
    this.statsIncludedFilter = null;
    this.heroIds = [];
    this.winnerColor = null;
    this.gameMode = null;
    this.pageIndex = 1;
    this.selectedSessionIds.clear();
    if (this.sessionId === sessionId) {
      this.loadSessions();
      return;
    }
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { sessionId },
    });
  }

  openAnalysisTaskCenter(view: 'workers' | 'tasks'): void {
    this.analysisTaskModalView = view;
    this.analysisTaskModalVisible = true;
    this.changeDetector.markForCheck();
  }

  openAddAnalysisWorker(): void {
    this.analysisWorkerEditorMode = 'add';
    this.analysisWorkerIdDraft = '';
    this.analysisWorkerNameDraft = '';
    this.analysisWorkerEditorVisible = true;
    this.changeDetector.markForCheck();
  }

  openEditAnalysisWorker(worker: VaingloryAnalysisWorkerNodeStatus): void {
    this.analysisWorkerEditorMode = 'edit';
    this.analysisWorkerIdDraft = worker.workerId;
    this.analysisWorkerNameDraft = worker.displayName;
    this.analysisWorkerEditorVisible = true;
    this.changeDetector.markForCheck();
  }

  saveAnalysisWorker(): void {
    if (this.savingAnalysisWorker) {
      return;
    }
    const workerId = this.analysisWorkerIdDraft.trim();
    const displayName = this.analysisWorkerNameDraft.trim();
    if (!workerId) {
      this.messages.warning('请输入 Worker ID');
      return;
    }
    this.savingAnalysisWorker = true;
    const request =
      this.analysisWorkerEditorMode === 'add'
        ? this.vainglory.addAnalysisWorker(workerId, displayName)
        : this.vainglory.updateAnalysisWorker(workerId, { displayName });
    request.pipe(takeUntil(this.destroy$)).subscribe({
      next: (worker) => {
        this.savingAnalysisWorker = false;
        this.analysisWorkerEditorVisible = false;
        this.replaceAnalysisWorker(worker);
        this.messages.success(
          this.analysisWorkerEditorMode === 'add'
            ? 'Worker 已登记，等待节点使用同一 ID 连接'
            : 'Worker 名称已保存',
        );
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.savingAnalysisWorker = false;
        this.messages.error(this.errorMessage(error, 'Worker 保存失败'));
        this.changeDetector.markForCheck();
      },
    });
  }

  setAnalysisWorkerEnabled(change: {
    readonly workerId: string;
    readonly enabled: boolean;
  }): void {
    if (this.updatingAnalysisWorkerIds.has(change.workerId)) {
      return;
    }
    this.updatingAnalysisWorkerIds = new Set([
      ...this.updatingAnalysisWorkerIds,
      change.workerId,
    ]);
    this.vainglory
      .updateAnalysisWorker(change.workerId, { enabled: change.enabled })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (worker) => {
          this.updatingAnalysisWorkerIds = new Set(
            [...this.updatingAnalysisWorkerIds].filter(
              (workerId) => workerId !== change.workerId,
            ),
          );
          this.replaceAnalysisWorker(worker);
          this.messages.success(
            change.enabled
              ? 'Worker 已恢复领取新任务'
              : worker.activeTaskCount > 0
                ? 'Worker 已进入安全暂停，当前任务完成后不再领取'
                : 'Worker 已暂停领取新任务',
          );
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.updatingAnalysisWorkerIds = new Set(
            [...this.updatingAnalysisWorkerIds].filter(
              (workerId) => workerId !== change.workerId,
            ),
          );
          this.messages.error(this.errorMessage(error, 'Worker 状态更新失败'));
          this.changeDetector.markForCheck();
        },
      });
  }

  openAnalysisImageBrowser(request: AnalysisImageRequest): void {
    const generation = ++this.analysisImageRequestGeneration;
    this.analysisImageBrowserVisible = true;
    this.analysisImageBrowserTitle = request.title;
    this.analysisImageBrowserLoading = true;
    this.analysisImageBrowserError = null;
    this.analysisImageBrowserItems = [];
    this.analysisImageBrowserIndex = 0;
    this.loadAnalysisImagePage(request, 0, [], generation);
  }

  closeAnalysisImageBrowser(): void {
    this.analysisImageBrowserVisible = false;
    this.analysisImageRequestGeneration += 1;
  }

  showPreviousAnalysisImage(): void {
    if (this.analysisImageBrowserIndex > 0) {
      this.analysisImageBrowserIndex -= 1;
      this.changeDetector.markForCheck();
    }
  }

  showNextAnalysisImage(): void {
    if (
      this.analysisImageBrowserIndex + 1 <
      this.analysisImageBrowserItems.length
    ) {
      this.analysisImageBrowserIndex += 1;
      this.changeDetector.markForCheck();
    }
  }

  selectAnalysisImage(index: number): void {
    if (index >= 0 && index < this.analysisImageBrowserItems.length) {
      this.analysisImageBrowserIndex = index;
      this.changeDetector.markForCheck();
    }
  }

  analysisImageTitle(match: VaingloryMatch, index: number): string {
    return `${match.title || `第 ${index + 1} 局`} · P${match.partIndex} · ${this.formatDuration(match.resultAtMs / 1000)}`;
  }

  @HostListener('document:keydown', ['$event'])
  handleAnalysisImageKeydown(event: KeyboardEvent): void {
    if (
      !this.analysisImageBrowserVisible ||
      this.analysisImageBrowserLoading ||
      this.analysisImageBrowserItems.length < 2
    ) {
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      this.showPreviousAnalysisImage();
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      this.showNextAnalysisImage();
    }
  }

  private loadAnalysisImagePage(
    request: AnalysisImageRequest,
    offset: number,
    accumulated: readonly VaingloryMatch[],
    generation: number,
  ): void {
    const filters: VaingloryMatchFilters = {
      playerName: '',
      heroIds: [],
      winnerColor: null,
      gameMode: null,
      sessionId: request.sessionId,
    };
    this.vainglory
      .listMatches(filters, 100, offset)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          if (generation !== this.analysisImageRequestGeneration) {
            return;
          }
          const items = [...accumulated, ...response.items];
          if (response.items.length > 0 && items.length < response.total) {
            this.loadAnalysisImagePage(
              request,
              items.length,
              items,
              generation,
            );
            return;
          }
          this.analysisImageBrowserItems = items
            .filter(
              (match) =>
                match.resultFrameUrl !== null &&
                (request.partId === undefined ||
                  match.partId === request.partId),
            )
            .sort(
              (left, right) =>
                left.partIndex - right.partIndex ||
                left.resultAtMs - right.resultAtMs ||
                left.id - right.id,
            );
          this.analysisImageBrowserLoading = false;
          this.analysisImageBrowserError =
            this.analysisImageBrowserItems.length === 0
              ? '这项任务当前没有可浏览的对局截图'
              : null;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (generation !== this.analysisImageRequestGeneration) {
            return;
          }
          this.analysisImageBrowserLoading = false;
          this.analysisImageBrowserError = this.errorMessage(
            error,
            '对局截图加载失败',
          );
          this.changeDetector.markForCheck();
        },
      });
  }

  isSessionRescanning(sessionId: number): boolean {
    return this.rescanningSessionIds.has(sessionId);
  }

  requestSessionRescan(session: { readonly sessionId: number }): void {
    if (this.rescanningSessionIds.has(session.sessionId)) {
      return;
    }
    this.rescanningSessionIds.add(session.sessionId);
    this.changeDetector.markForCheck();
    this.vainglory
      .requestScan(session.sessionId)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.rescanningSessionIds.delete(session.sessionId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (job) => {
          this.removeSessionFromReviewQueues(session.sessionId);
          this.messages.success('已加入重新分析队列，并提升为手动优先');
          this.receiveScanJob(job);
          this.loadZeroMatchSessions();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '无法重新分析这场直播'));
        },
      });
  }

  isScanSuppressionUpdating(sessionId: number): boolean {
    return this.updatingScanSuppressionSessionIds.has(sessionId);
  }

  suppressZeroMatchSession(session: VaingloryZeroMatchSession): void {
    this.updateZeroMatchScanSuppression(session.sessionId, true);
  }

  restoreZeroMatchSession(session: VaingloryZeroMatchSession): void {
    this.updateZeroMatchScanSuppression(session.sessionId, false);
  }

  private updateZeroMatchScanSuppression(
    sessionId: number,
    suppressed: boolean,
  ): void {
    if (this.updatingScanSuppressionSessionIds.has(sessionId)) {
      return;
    }
    this.updatingScanSuppressionSessionIds.add(sessionId);
    const request = suppressed
      ? this.vainglory.suppressZeroMatchSession(sessionId)
      : this.vainglory.restoreZeroMatchSession(sessionId);
    request
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.updatingScanSuppressionSessionIds.delete(sessionId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.messages.success(
            suppressed
              ? '已确认无需扫描，今后的批量重扫也会跳过这场直播'
              : '已恢复扫描，可重新分析这场直播',
          );
          this.loadZeroMatchSessions();
          this.loadSuppressedZeroMatchSessions();
        },
        error: (error: unknown) => {
          this.messages.error(
            this.errorMessage(
              error,
              suppressed ? '无法确认无需扫描' : '无法恢复扫描',
            ),
          );
        },
      });
  }

  isPublicationStepFailed(
    session: VaingloryMatchSession,
    step: VaingloryPublicationRetryStep,
  ): boolean {
    if (session.publicationState !== 'failed') {
      return false;
    }
    if (session.chapterState !== 'confirmed') {
      return step === 'chapter';
    }
    if (session.descriptionState !== 'confirmed') {
      return step === 'description';
    }
    return (
      session.pinState !== 'confirmed' &&
      (step === 'pin' || step === 'comments')
    );
  }

  canRetryPublicationStep(
    session: VaingloryMatchSession,
    step: VaingloryPublicationRetryStep,
  ): boolean {
    if (
      session.publicationState === null ||
      session.publicationState === 'running'
    ) {
      return false;
    }
    if (session.publicationStatus === 'legacy_chapter_timing') {
      return step === 'chapter';
    }
    return ![
      'operator_paused',
      'analysis_failed',
      'waiting_analysis',
      'upload_missing',
      'review_rejected',
      'upload_paused',
      'waiting_review',
      'waiting_upload',
      'analysis_data_invalid',
    ].includes(session.publicationStatus ?? '');
  }

  publicationRetryLabel(
    session: VaingloryMatchSession,
    step: VaingloryPublicationRetryStep,
  ): string {
    const confirmed =
      step === 'pin' || step === 'comments'
        ? session.pinState === 'confirmed'
        : step === 'description'
          ? session.descriptionState === 'confirmed'
          : session.chapterState === 'confirmed';
    return confirmed ? '重发' : '重试';
  }

  isPublicationStepRetrying(
    sessionId: number,
    step: VaingloryPublicationRetryStep,
  ): boolean {
    return this.retryingPublicationSteps.has(
      this.publicationRetryKey(sessionId, step),
    );
  }

  retryPublicationStep(
    session: VaingloryMatchSession,
    step: VaingloryPublicationRetryStep,
  ): void {
    if (!this.canRetryPublicationStep(session, step)) {
      return;
    }
    const key = this.publicationRetryKey(session.sessionId, step);
    if (this.retryingPublicationSteps.has(key)) {
      return;
    }
    this.retryingPublicationSteps.add(key);
    this.changeDetector.markForCheck();
    this.vainglory
      .retryPublicationStep(session.sessionId, step)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.retryingPublicationSteps.delete(key);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.markPublicationStepQueued(session.sessionId, step);
          const labels: Readonly<
            Record<VaingloryPublicationRetryStep, string>
          > = {
            description: '简介',
            comments: '置顶评论',
            pin: '置顶评论',
            chapter: '视频分段',
          };
          this.messages.success(
            `已将${labels[step]}加入发布专用队列，并提升为最高优先级`,
          );
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '无法重试这个发布步骤'));
        },
      });
  }

  private publicationRetryKey(
    sessionId: number,
    step: VaingloryPublicationRetryStep,
  ): string {
    return `${sessionId}:${step}`;
  }

  private markPublicationStepQueued(
    sessionId: number,
    step: VaingloryPublicationRetryStep,
  ): void {
    if (this.sessionsView.state !== 'ready') {
      return;
    }
    this.sessionsView = {
      ...this.sessionsView,
      items: this.sessionsView.items.map((session) => {
        if (session.sessionId !== sessionId) {
          return session;
        }
        return {
          ...session,
          publicationState: 'prepared',
          publicationStatus: 'queued',
          publicationStatusLabel: '发布队列中',
          publicationStatusDetail: '已手动加入发布队列。',
          publicationRecommendedAction: 'wait',
          publicationNextAttemptAt: null,
          descriptionState:
            step === 'description' ? 'prepared' : session.descriptionState,
          pinState:
            step === 'pin' || step === 'comments'
              ? 'prepared'
              : session.pinState,
          chapterState: step === 'chapter' ? 'prepared' : session.chapterState,
        };
      }),
    };
  }

  publicationStepColor(state: string | null, failed: boolean): string {
    if (failed) {
      return 'red';
    }
    if (state === 'confirmed') {
      return 'green';
    }
    if (state === 'in_flight') {
      return 'blue';
    }
    if (state === 'prepared') {
      return 'gold';
    }
    return 'default';
  }

  descriptionStateLabel(session: VaingloryMatchSession): string {
    if (this.isPublicationStepFailed(session, 'description')) {
      return '失败';
    }
    switch (session.descriptionState) {
      case 'prepared':
        return '等待回填';
      case 'in_flight':
        return '回填中';
      case 'confirmed':
        return '已回填';
      case 'skipped_no_room':
        return '已跳过';
      case null:
        return '未开始';
    }
  }

  pinStateLabel(session: VaingloryMatchSession): string {
    if (this.isPublicationStepFailed(session, 'pin')) {
      return '失败';
    }
    switch (session.pinState) {
      case 'prepared':
        return '等待处理';
      case 'in_flight':
        return '处理中';
      case 'confirmed':
        return '已置顶';
      case null:
        return '未开始';
    }
  }

  chapterStateLabel(session: VaingloryMatchSession): string {
    if (this.isPublicationStepFailed(session, 'chapter')) {
      return '失败';
    }
    switch (session.chapterState) {
      case 'prepared':
        return '等待设置';
      case 'confirmed':
        return '已设置';
      case 'skipped':
        return '已跳过';
      case null:
        return '未开始';
    }
  }

  publicationStatusColor(
    status: VaingloryPublicationStatus | null | undefined,
  ): string {
    switch (status) {
      case 'confirmed':
        return 'green';
      case 'running':
        return 'blue';
      case 'waiting_analysis':
      case 'waiting_review':
      case 'waiting_upload':
        return 'cyan';
      case 'queued':
        return 'gold';
      case 'operator_paused':
      case 'upload_paused':
      case 'legacy_chapter_timing':
      case 'retry_scheduled':
        return 'orange';
      case 'analysis_failed':
      case 'analysis_data_invalid':
      case 'review_rejected':
      case 'upload_missing':
      case 'failed':
        return 'red';
      case null:
      case undefined:
        return 'default';
    }
  }

  publicationActionLabel(
    action: VaingloryPublicationRecommendedAction | null | undefined,
  ): string | null {
    switch (action) {
      case 'wait':
        return '无需操作，系统会自动继续';
      case 'reanalyze':
        return '使用新算法重新分析整场直播';
      case 'retry_chapter':
        return '重试视频分段，无需重新识别';
      case 'resume_migration':
        return '到历史稿件迁移任务中恢复运行';
      case 'check_upload':
        return '检查投稿任务或 B 站审核结果';
      case 'retry':
        return '根据下方失败步骤重试';
      case 'none':
      case null:
      case undefined:
        return null;
    }
  }

  detailsFor(sessionId: number): MatchDetailsView | null {
    return this.matchDetails.get(sessionId) ?? null;
  }

  openMatchEditor(match: VaingloryMatch): void {
    this.editingMatch = match;
    this.matchEditDraft = {
      title: match.title,
      gameMode: match.gameMode,
      durationSeconds: match.durationSeconds,
      resultText: match.resultText,
      endReason: match.endReason,
      winnerColor: match.winnerColor,
      matchKind: match.matchKind,
      viewContext: match.viewContext,
      statsEligible: match.statsEligible,
      leftKills: match.leftKills,
      rightKills: match.rightKills,
      leftEconomy: match.leftEconomy,
      rightEconomy: match.rightEconomy,
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
      })),
    };
    this.matchEditorVisible = true;
  }

  saveMatchEdit(): void {
    const match = this.editingMatch;
    const draft = this.matchEditDraft;
    if (match === null || draft === null || this.savingMatchEdit) {
      return;
    }
    this.savingMatchEdit = true;
    this.vainglory
      .updateMatch(match.id, draft)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingMatchEdit = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (saved) => {
          this.replaceMatchDetails(saved);
          this.matchEditorVisible = false;
          this.editingMatch = null;
          this.matchEditDraft = null;
          this.loadPlayerStats();
          this.loadHeroStats();
          this.messages.success('对局信息已人工修正，后续重扫不会覆盖');
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '对局信息保存失败'));
        },
      });
  }

  toggleMatchStatsEligibility(match: VaingloryMatch): void {
    if (this.savingMatchIds.has(match.id)) {
      return;
    }
    this.savingMatchIds.add(match.id);
    this.vainglory
      .updateMatch(match.id, { statsEligible: !match.statsEligible })
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingMatchIds.delete(match.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (saved) => {
          this.replaceMatchDetails(saved);
          this.loadPlayerStats();
          this.loadHeroStats();
          this.messages.success(
            saved.statsEligible ? '已恢复计入统计' : '已设为不计入统计',
          );
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '统计设置保存失败'));
        },
      });
  }

  reanalyzeMatch(match: VaingloryMatch): void {
    if (this.rerunningMatchIds.has(match.id)) {
      return;
    }
    this.rerunningMatchIds.add(match.id);
    this.vainglory
      .reanalyzeMatch(match.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.rerunningMatchIds.delete(match.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.replaceMatchDetails({
            ...match,
            rerunState: 'pending',
            rerunError: null,
          });
          this.removeMatchFromReviewQueues(match.id);
          this.messages.success('已加入单局重新识别队列，并优先处理');
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '单局重新识别提交失败'));
        },
      });
  }

  deleteDetectedMatch(match: VaingloryMatch): void {
    if (this.deletingMatchIds.has(match.id)) {
      return;
    }
    this.deletingMatchIds.add(match.id);
    this.vainglory
      .deleteMatch(match.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.deletingMatchIds.delete(match.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          const details = this.matchDetails.get(match.sessionId);
          if (details?.state === 'ready') {
            this.matchDetails.set(match.sessionId, {
              state: 'ready',
              items: details.items.filter((item) => item.id !== match.id),
            });
          }
          this.loadSessions(true);
          this.loadZeroMatchSessions();
          this.loadPlayerStats();
          this.loadHeroStats();
          this.messages.success('已删除误识别，并记住该时间点不再自动加入');
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '删除对局失败'));
        },
      });
  }

  openManualMarker(session: VaingloryZeroMatchSession): void {
    this.manualMarkerSession = session;
    this.manualMarkerPartIndex = 1;
    this.manualMarkerTime = '';
    this.manualMarkerVisible = true;
  }

  saveManualMarker(): void {
    const session = this.manualMarkerSession;
    const atMs = this.parseMarkerTime(this.manualMarkerTime);
    if (session === null || atMs === null || this.savingManualMarker) {
      if (atMs === null) {
        this.messages.error('请输入有效时间，例如 12:34 或 1:02:03');
      }
      return;
    }
    this.savingManualMarker = true;
    this.vainglory
      .markSessionMatch(session.sessionId, this.manualMarkerPartIndex, atMs)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingManualMarker = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.manualMarkerVisible = false;
          this.messages.success('已标记对局并加入人工优先识别队列');
          this.loadZeroMatchSessions();
          this.loadSuppressedZeroMatchSessions();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '对局时间点标记失败'));
        },
      });
  }

  private parseMarkerTime(value: string): number | null {
    const parts = value.trim().split(':');
    if (
      parts.length < 1 ||
      parts.length > 3 ||
      parts.some((part) => !/^\d+$/.test(part))
    ) {
      return null;
    }
    const numbers = parts.map((part) => Number.parseInt(part, 10));
    if (
      numbers.some((part) => !Number.isSafeInteger(part)) ||
      (parts.length > 1 && numbers.slice(1).some((part) => part >= 60))
    ) {
      return null;
    }
    const seconds = numbers.reduce((total, part) => total * 60 + part, 0);
    return seconds <= 604_800 ? seconds * 1_000 : null;
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

  loadPlayers(): void {
    this.playersView = { state: 'loading' };
    this.vainglory
      .listPlayers()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          this.playersView = { state: 'ready', items };
          for (const player of items) {
            this.playerNameDrafts.set(player.id, player.name);
            if (!this.playerRoomDrafts.has(player.id)) {
              this.playerRoomDrafts.set(player.id, '');
            }
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.playersView = {
            state: 'error',
            message: this.errorMessage(error, '玩家库加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  syncManagedRooms(showMessage = false): void {
    if (this.syncingManagedRooms) {
      return;
    }
    if (this.managedRooms.length === 0) {
      this.loadPlayers();
      return;
    }
    this.syncingManagedRooms = true;
    this.vainglory
      .syncPlayerRooms(
        this.managedRooms.map((room) => ({
          roomId: room.roomId,
          name: room.anchorName,
        })),
      )
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.syncingManagedRooms = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (items) => {
          this.playersView = { state: 'ready', items };
          for (const player of items) {
            this.playerNameDrafts.set(player.id, player.name);
          }
          if (showMessage) {
            this.messages.success('房间管理中的玩家已同步');
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '房间管理玩家同步失败'));
          this.loadPlayers();
        },
      });
  }

  createPlayer(): void {
    const name = this.newPlayerName.trim();
    if (!name || this.creatingPlayer) {
      return;
    }
    this.creatingPlayer = true;
    this.vainglory
      .createPlayer(name)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.creatingPlayer = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.newPlayerName = '';
          this.messages.success('玩家已创建');
          this.loadPlayers();
          this.loadPlayerStats();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '玩家创建失败'));
        },
      });
  }

  playerNameDraft(player: VaingloryPlayer): string {
    return this.playerNameDrafts.get(player.id) ?? player.name;
  }

  setPlayerNameDraft(playerId: number, value: string): void {
    this.playerNameDrafts.set(playerId, value);
  }

  savePlayerName(player: VaingloryPlayer): void {
    const name = this.playerNameDraft(player).trim();
    if (!name || name === player.name || this.playerSaving(player.id)) {
      return;
    }
    this.savingPlayerIds.add(player.id);
    this.vainglory
      .renamePlayer(player.id, name)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingPlayerIds.delete(player.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.messages.success('玩家名称已更新');
          this.loadPlayers();
          this.loadPlayerStats();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '玩家名称保存失败'));
        },
      });
  }

  deletePlayer(player: VaingloryPlayer): void {
    if (this.playerSaving(player.id)) {
      return;
    }
    this.savingPlayerIds.add(player.id);
    this.vainglory
      .deletePlayer(player.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingPlayerIds.delete(player.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.playerNameDrafts.delete(player.id);
          this.playerRoomDrafts.delete(player.id);
          this.messages.success('玩家已删除');
          this.loadPlayers();
          this.loadPlayerStats();
          this.loadHeroStats();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '玩家删除失败'));
        },
      });
  }

  playerRoomDraft(playerId: number): string {
    return this.playerRoomDrafts.get(playerId) ?? '';
  }

  setPlayerRoomDraft(playerId: number, value: string | number | null): void {
    this.playerRoomDrafts.set(playerId, value === null ? '' : String(value));
  }

  bindPlayerRoom(player: VaingloryPlayer): void {
    const roomId = Number(this.playerRoomDraft(player.id));
    if (
      !Number.isInteger(roomId) ||
      roomId <= 0 ||
      this.playerSaving(player.id)
    ) {
      return;
    }
    this.savingPlayerIds.add(player.id);
    this.vainglory
      .bindPlayerRoom(player.id, roomId)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingPlayerIds.delete(player.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.playerRoomDrafts.set(player.id, '');
          this.messages.success(`直播间 ${roomId} 已绑定`);
          this.loadPlayers();
          this.loadPlayerStats();
          this.loadHeroStats();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '直播间绑定失败'));
        },
      });
  }

  unbindPlayerRoom(player: VaingloryPlayer, roomId: number): void {
    if (this.playerSaving(player.id)) {
      return;
    }
    this.savingPlayerIds.add(player.id);
    this.vainglory
      .unbindPlayerRoom(player.id, roomId)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.savingPlayerIds.delete(player.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.messages.success(`直播间 ${roomId} 已解绑`);
          this.loadPlayers();
          this.loadPlayerStats();
          this.loadHeroStats();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '直播间解绑失败'));
        },
      });
  }

  playerSaving(playerId: number): boolean {
    return this.savingPlayerIds.has(playerId);
  }

  loadPlayerStats(): void {
    this.playerStatsView = { state: 'loading' };
    this.vainglory
      .listPlayerStats()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          this.playerStatsView = { state: 'ready', items };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.playerStatsView = {
            state: 'error',
            message: this.errorMessage(error, '玩家排行加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  loadHeroStats(): void {
    this.heroStatsView = { state: 'loading' };
    this.vainglory
      .listHeroStats(this.heroStatsGameMode)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          this.heroStatsView = { state: 'ready', items };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.heroStatsView = {
            state: 'error',
            message: this.errorMessage(error, '英雄排行加载失败'),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  heroStatsGameModeChanged(value: GameMode | ''): void {
    this.heroStatsGameMode = value;
    this.loadHeroStats();
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
          this.managedRooms = this.managedRoomOptions(items);
          this.syncManagedRooms();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.managedAnchorsLoading = false;
          this.messages.error(this.errorMessage(error, '房间管理主播加载失败'));
          this.loadPlayers();
          this.changeDetector.markForCheck();
        },
      });
  }

  refreshPage(): void {
    this.loadSessions();
    this.loadZeroMatchSessions();
    this.loadAnchorStats();
    this.loadPlayers();
    this.loadPlayerStats();
    this.loadHeroStats();
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

  unrecognizedPlayers(match: VaingloryMatch): readonly VaingloryMatchPlayer[] {
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
          this.loadPlayerStats();
          this.loadHeroStats();
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

  reviewIgnoreKey(
    matchId: number,
    reviewType: 'hero' | 'recorded_player',
  ): string {
    return `${reviewType}:${matchId}`;
  }

  ignoreMatchReview(
    match: VaingloryMatch,
    reviewType: 'hero' | 'recorded_player',
  ): void {
    const key = this.reviewIgnoreKey(match.id, reviewType);
    if (this.ignoringReviewKeys.has(key)) {
      return;
    }
    this.ignoringReviewKeys.add(key);
    this.vainglory
      .suppressMatchReview(match.id, reviewType)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.ignoringReviewKeys.delete(key);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.removeMatchFromReviewQueue(match.id, reviewType);
          this.messages.success(
            '已从当前待确认列表忽略，对局和统计数据保持不变',
          );
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '忽略待确认项失败'));
        },
      });
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
          this.messages.error(this.errorMessage(error, '主播英雄人工确认失败'));
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

  private removeMatchFromReviewQueue(
    matchId: number,
    reviewType: 'hero' | 'recorded_player',
  ): void {
    if (reviewType === 'hero' && this.heroReviewView.state === 'ready') {
      const items = this.heroReviewView.items.filter(
        (match) => match.id !== matchId,
      );
      this.heroReviewView = {
        state: 'ready',
        total: Math.max(
          0,
          this.heroReviewView.total -
            (items.length === this.heroReviewView.items.length ? 0 : 1),
        ),
        items,
      };
    }
    if (
      reviewType === 'recorded_player' &&
      this.recordedPlayerReviewView.state === 'ready'
    ) {
      const items = this.recordedPlayerReviewView.items.filter(
        (match) => match.id !== matchId,
      );
      this.recordedPlayerReviewView = {
        state: 'ready',
        total: Math.max(
          0,
          this.recordedPlayerReviewView.total -
            (items.length === this.recordedPlayerReviewView.items.length
              ? 0
              : 1),
        ),
        items,
      };
    }
  }

  private removeMatchFromReviewQueues(matchId: number): void {
    this.removeMatchFromReviewQueue(matchId, 'hero');
    this.removeMatchFromReviewQueue(matchId, 'recorded_player');
  }

  private removeSessionFromReviewQueues(sessionId: number): void {
    if (this.heroReviewView.state === 'ready') {
      const items = this.heroReviewView.items.filter(
        (match) => match.sessionId !== sessionId,
      );
      this.heroReviewView = {
        state: 'ready',
        total: Math.max(
          0,
          this.heroReviewView.total -
            (this.heroReviewView.items.length - items.length),
        ),
        items,
      };
    }
    if (this.recordedPlayerReviewView.state === 'ready') {
      const items = this.recordedPlayerReviewView.items.filter(
        (match) => match.sessionId !== sessionId,
      );
      this.recordedPlayerReviewView = {
        state: 'ready',
        total: Math.max(
          0,
          this.recordedPlayerReviewView.total -
            (this.recordedPlayerReviewView.items.length - items.length),
        ),
        items,
      };
    }
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
          this.loadPlayers();
          this.loadPlayerStats();
          this.loadHeroStats();
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

  bulkRescanSelected(): void {
    if (this.bulkUpdatingAction !== null) {
      return;
    }
    const sessionIds = [...this.selectedSessionIds];
    if (sessionIds.length === 0) {
      this.messages.warning('请先选择至少一场直播');
      return;
    }
    this.bulkUpdatingAction = 'rescan';
    this.changeDetector.markForCheck();
    from(sessionIds)
      .pipe(
        concatMap((sessionId) =>
          this.vainglory.requestScan(sessionId).pipe(
            map((job): BulkRescanResult => ({
              state: 'queued',
              sessionId,
              job,
            })),
            catchError((error: unknown) =>
              of<BulkRescanResult>({ state: 'failed', sessionId, error }),
            ),
          ),
        ),
        toArray(),
        takeUntil(this.destroy$),
        finalize(() => {
          this.bulkUpdatingAction = null;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe((results) => {
        let queuedCount = 0;
        let failedCount = 0;
        let firstError: unknown = null;
        for (const result of results) {
          if (result.state === 'queued') {
            queuedCount += 1;
            this.selectedSessionIds.delete(result.sessionId);
            this.receiveScanJob(result.job);
          } else {
            failedCount += 1;
            firstError ??= result.error;
          }
        }
        if (failedCount === 0) {
          this.messages.success(`已将 ${queuedCount} 场直播加入重新分析队列`);
        } else if (queuedCount > 0) {
          this.messages.warning(
            `已加入 ${queuedCount} 场，${failedCount} 场失败；失败项已保留选中`,
          );
        } else {
          this.messages.error(
            `批量重新分析失败：${this.errorMessage(firstError, '未知错误')}`,
          );
        }
        this.changeDetector.markForCheck();
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
      discovering: '正在查找历史稿件',
      running: `已处理 ${sync.completedCount} / 已发现 ${sync.discoveredCount}`,
      ready: `全部处理完成，共 ${sync.completedCount} 个稿件`,
      failed: '历史稿件接入失败',
    }[sync.state];
  }

  archiveDiscoveryLabel(sync: VaingloryArchiveSync): string {
    const scannedPages = Math.max(0, sync.nextPage - 1);
    return sync.discoveryComplete
      ? `稿件列表扫描完成：共 ${scannedPages} 页，已收录 ${sync.discoveredCount} 个稿件（按 BV 号去重）`
      : `已扫描 ${scannedPages} 页，总页数待确认；已收录 ${sync.discoveredCount} 个稿件（按 BV 号去重）`;
  }

  archiveItems(accountId: number): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItemsByAccountId.get(accountId) ?? [];
  }

  archiveDownloadingItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItems(accountId)
      .filter((item) => item.stage === 'downloading')
      .slice(0, 3);
  }

  archiveWaitingDownloadItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItems(accountId)
      .filter((item) =>
        ['queued', 'reading_metadata', 'download_pending'].includes(item.stage),
      )
      .slice(0, 3);
  }

  archiveDownloadQueueItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return [
      ...this.archiveDownloadingItems(accountId),
      ...this.archiveWaitingDownloadItems(accountId),
    ];
  }

  archiveIntakeLabel(item: VaingloryArchiveBackfillItem): string {
    return {
      queued: '等待读取稿件',
      reading_metadata: '正在读取稿件',
      download_pending: '等待下载',
      downloading: '正在下载',
      analysis_pending: '等待分析',
      scanning_video: '正在扫描视频',
      locating_results: '正在查找结算画面',
      ocr_recognition: '正在识别战绩',
      publication_pending: '分析完成',
      publishing_description: '正在更新稿件说明',
      publishing_comments: '正在更新评论',
      pinning_comment: '正在置顶评论',
      completed: '处理完成',
      managed_elsewhere: '无需重复处理',
      failed: '处理失败',
    }[item.stage];
  }

  archiveIntakePercent(item: VaingloryArchiveBackfillItem): number {
    if (item.downloadProgress >= 0.999 || this.archiveStageAfterDownload(item)) {
      return 100;
    }
    if (item.stage === 'downloading') {
      return Math.max(0, Math.min(100, Math.round(item.downloadProgress * 100)));
    }
    return 0;
  }

  heroRecognitionPercent(summary: VaingloryIndexSummary): number {
    if (summary.playerSlotCount === 0) {
      return 0;
    }
    return Math.round(
      (summary.recognizedHeroCount / summary.playerSlotCount) * 100,
    );
  }

  workerStatusTriggerLabel(queue: VaingloryAnalysisQueue): string {
    const workers = queue.workers ?? [];
    if (workers.length === 0) {
      if (queue.workerState === 'failed') {
        return 'Worker 异常';
      }
      return queue.workerState === 'stopped' ? 'Worker 离线' : 'Worker 空闲';
    }
    const runningWorkers = workers.filter(
      (worker) => worker.state === 'running' && worker.enabled,
    ).length;
    const failedWorkers = workers.filter(
      (worker) => worker.state === 'failed',
    ).length;
    const pausedWorkers = workers.filter((worker) => !worker.enabled).length;
    if (failedWorkers > 0) {
      return `${failedWorkers} 个 Worker 异常`;
    }
    if (runningWorkers === 0 && pausedWorkers > 0) {
      return `${pausedWorkers} 个 Worker 已暂停`;
    }
    if (runningWorkers === 0) {
      return `${workers.length} 个 Worker 离线`;
    }
    const count = runningWorkers > 0 ? `${runningWorkers} 个 Worker` : 'Worker';
    const activeTasks = workers.reduce(
      (total, worker) => total + worker.activeTaskCount,
      0,
    );
    return activeTasks > 0 || queue.active.length > 0
      ? `${count} 处理中`
      : `${count} 空闲`;
  }

  workerStatusPaused(queue: VaingloryAnalysisQueue | null): boolean {
    const workers = queue?.workers ?? [];
    return workers.length > 0 && workers.every((worker) => !worker.enabled);
  }

  archiveDownloadLabel(item: VaingloryArchiveBackfillItem): string {
    const size = this.archiveDownloadSize(item);
    if (
      item.downloadProgress >= 0.999 ||
      this.archiveStageAfterDownload(item)
    ) {
      return size ? `已完成 · ${size}` : '已完成';
    }
    if (item.stage === 'downloading') {
      return `${Math.round(item.downloadProgress * 100)}%${
        size ? ` · ${size}` : ''
      }`;
    }
    return '等待';
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
    if (!Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 1000) {
      this.messages.error('每日处理上限必须是 1 到 1000 的整数');
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
    const refreshSignature = this.vaingloryListRefreshSignature(snapshot);
    const shouldRefreshLists =
      this.listRefreshSignature !== null &&
      this.listRefreshSignature !== refreshSignature;
    this.listRefreshSignature = refreshSignature;
    this.analysisQueue = snapshot.analysisQueue;
    this.indexSummary = snapshot.indexSummary;
    this.indexSampledAt = snapshot.sampledAt;
    if (shouldRefreshLists) {
      this.loadSessions(true);
      this.loadZeroMatchSessions(true);
      if (this.detailsDrawerVisible && this.selectedSession !== null) {
        this.loadSessionDetails(this.selectedSession.sessionId);
      }
    }
    this.changeDetector.markForCheck();
  }

  private vaingloryListRefreshSignature(
    snapshot: VaingloryIndexRealtimeSnapshot,
  ): string {
    const summary = snapshot.indexSummary;
    const latestCompletion = snapshot.analysisQueue.recentCompletions[0];
    return [
      summary.matchCount,
      summary.sessionCount,
      summary.anchorCount,
      summary.unassignedSessionCount,
      summary.winCount,
      summary.lossCount,
      summary.unknownCount,
      summary.playerSlotCount,
      summary.recognizedHeroCount,
      snapshot.analysisQueue.recentCompletions.length,
      latestCompletion?.partId ?? 0,
      latestCompletion?.completedAt ?? 0,
      latestCompletion?.matchCount ?? 0,
    ].join(':');
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

  private archiveStageAfterDownload(
    item: VaingloryArchiveBackfillItem,
  ): boolean {
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

  openMatch(match: VaingloryMatch, atResult = false): void {
    this.openMatchMedia(match, atResult ? 'result' : 'play');
  }

  openAnalysisPart(target: {
    readonly sessionId: number;
    readonly partId: number;
  }): void {
    this.recordingSessions
      .getSession(target.sessionId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (session) => {
          const part = session.parts.find((item) => item.id === target.partId);
          if (part === undefined || (!part.sourceExists && !part.finalExists)) {
            this.messages.info('该分 P 的本地视频已经不可用');
            return;
          }
          this.analysisTaskModalVisible = false;
          this.previewSession = session;
          this.previewPart = part;
          this.previewSeekSeconds = null;
          this.previewVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.messages.error(this.errorMessage(error, '录像打开失败'));
        },
      });
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

  zeroMatchSessionBiliUrl(session: VaingloryZeroMatchSession): string | null {
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

  statsExclusionLabel(match: VaingloryMatch): string {
    switch (match.statsExclusionReason) {
      case 'too_short_3v3':
        return '3V3 时长过短';
      case 'bot':
        return '人机对战';
      case 'practice':
        return '练习模式';
      case 'observed':
        return '观战或回放';
      case 'duplicate':
        return '重复对局';
      default:
        return '不计入排行榜';
    }
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
      ready: `已识别 ${job.matchCount} 局 · ${job.partCount} 个有效分 P`,
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

  trackZeroMatchSession(
    _index: number,
    session: VaingloryZeroMatchSession,
  ): number {
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

  trackStoredPlayer(_index: number, player: VaingloryPlayer): number {
    return player.id;
  }

  trackPlayerStats(_index: number, stats: VaingloryPlayerStats): number {
    return stats.playerId;
  }

  trackHeroStats(_index: number, stats: VaingloryHeroStats): number {
    return stats.heroId;
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

  private replaceAnalysisWorker(
    saved: VaingloryAnalysisWorkerNodeStatus,
  ): void {
    if (this.analysisQueue === null) {
      return;
    }
    const existing = this.analysisQueue.workers ?? [];
    const found = existing.some((worker) => worker.workerId === saved.workerId);
    this.analysisQueue = {
      ...this.analysisQueue,
      workers: found
        ? existing.map((worker) =>
            worker.workerId === saved.workerId ? saved : worker,
          )
        : [...existing, saved],
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
          this.loadPlayerStats();
          this.loadHeroStats();
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

  private managedRoomOptions(
    items: readonly TaskData[],
  ): readonly ManagedRoomOption[] {
    const byRoom = new Map<number, ManagedRoomOption>();
    for (const item of items) {
      const anchorName = item.user_info.name.trim();
      const roomId = item.room_info.room_id;
      if (!anchorName || roomId <= 0) {
        continue;
      }
      byRoom.set(roomId, {
        anchorName,
        roomId,
        anchorUid: item.user_info.uid,
        label: `${anchorName}（房间 ${roomId}）`,
      });
    }
    return [...byRoom.values()].sort((left, right) =>
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
