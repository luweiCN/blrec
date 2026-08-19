import { ChangeDetectorRef } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { NEVER, of, Subject, throwError } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  RealtimeEvent,
  RealtimeService,
} from '../core/services/realtime.service';
import { TaskService } from '../tasks/shared/services/task.service';
import {
  RecordingSessionDetail,
  RemoteMediaStatus,
} from '../upload-tasks/shared/recording-session.model';
import { RecordingSessionService } from '../upload-tasks/shared/recording-session.service';
import { BiliAccount } from '../uploads/shared/bili-account.model';
import { BiliAccountService } from '../uploads/shared/bili-account.service';
import { VaingloryComponent } from './vainglory.component';
import {
  ArchiveBackfillStage,
  VaingloryArchiveBackfillItem,
  VaingloryArchiveSync,
  VaingloryIndexRealtimeSnapshot,
  VaingloryMatch,
  VaingloryMatchSession,
  VaingloryPlayer,
  VaingloryScanJob,
} from './vainglory.model';
import { VaingloryService } from './vainglory.service';

function match(): VaingloryMatch {
  return {
    id: 3,
    sessionId: 9,
    sessionTitle: '直播标题',
    sessionStartedAt: 1_000,
    partId: 7,
    partIndex: 1,
    title: '第一局',
    sourceTitle: '直播标题',
    uploadTitle: '投稿标题',
    gameMode: '3v3',
    teamSize: 3,
    matchKind: 'pvp',
    viewContext: 'played',
    statsEligible: true,
    statsExclusionReason: null,
    duplicateOfMatchId: null,
    duplicateResultFrameUrl: null,
    duplicateReviewState: 'none',
    startedAtMs: 15_000,
    resultAtMs: 600_000,
    durationSeconds: 585,
    resultText: '获胜',
    endReason: 'normal',
    leftColor: 'teal',
    rightColor: 'orange',
    winnerSide: 'left',
    winnerColor: 'teal',
    leftKills: 20,
    rightKills: 10,
    leftEconomy: 40_000,
    rightEconomy: 30_000,
    confidence: 0.9,
    accountId: 2,
    bvid: 'BV1abcdefgh',
    archivePage: 1,
    resultFrameUrl: '/api/v1/vainglory/matches/3/result-frame',
    recordedPlayerConfidence: null,
    recordedPlayerSource: 'automatic',
    recordedPlayerState: 'pending',
    rerunState: null,
    rerunError: null,
    players: [],
  };
}

function scanJob(sessionId: number): VaingloryScanJob {
  return {
    sessionId,
    state: 'pending',
    progress: 0,
    algorithmVersion: 17,
    matchCount: 0,
    error: null,
    requestedAt: 1_000,
    startedAt: null,
    completedAt: null,
    updatedAt: 1_000,
    partCount: 1,
    originalPartCount: 1,
    ignoredPartCount: 0,
    ignoredPartReasons: [],
  };
}

function indexSnapshot(matchCount: number): VaingloryIndexRealtimeSnapshot {
  return {
    sampledAt: matchCount,
    analysisQueue: {
      workerState: 'running',
      worker: {
        state: 'running',
        remoteEnabled: true,
        workerId: 'mac-studio',
        modelPackageId: 'vg-vision-v1',
        pipelineVersion: 'timeline-v2',
        lastSeenAt: matchCount,
      },
      workers: [],
      active: [],
      queued: [],
      recentCompletions: [],
      pendingCount: 0,
      manualPending: 0,
      realtimePending: 0,
      archivePending: 0,
      migrationPending: 0,
      backlogPending: 0,
      liveStreamCount: 0,
      liveRunningCount: 0,
      livePendingWindowCount: 0,
      liveSampleCount: 0,
      liveProvisionalMatchCount: 0,
      liveLastObservedAt: null,
      liveItems: [],
    },
    indexSummary: {
      matchCount,
      sessionCount: 1,
      anchorCount: 1,
      unassignedSessionCount: 0,
      winCount: matchCount,
      lossCount: 0,
      unknownCount: 0,
      playerSlotCount: matchCount * 6,
      recognizedHeroCount: matchCount * 6,
    },
  };
}

function archiveItem(
  id: number,
  stage: ArchiveBackfillStage,
): VaingloryArchiveBackfillItem {
  return {
    id,
    accountId: 7,
    aid: 100 + id,
    bvid: `BV${id}`,
    title: `历史稿件 ${id}`,
    publishedAt: 1_000,
    state: 'running',
    stage,
    progress: 0.5,
    pageCount: 1,
    completedPageCount: 0,
    currentPage: 1,
    currentPartTitle: 'P1',
    downloadProgress: stage === 'downloading' ? 0.5 : 0,
    downloadedBytes: 100,
    totalBytes: 200,
    analysisState: null,
    analysisProgress: 0,
    matchCount: 0,
    publicationState: null,
    descriptionState: null,
    commentCount: 0,
    confirmedCommentCount: 0,
    pinState: null,
    publicationProgress: 0,
    error: null,
    updatedAt: 1_000,
  };
}

function archiveSync(
  overrides: Partial<VaingloryArchiveSync> = {},
): VaingloryArchiveSync {
  return {
    accountId: 7,
    state: 'running',
    progress: 0.85,
    discoveredCount: 2_148,
    completedCount: 1_832,
    error: null,
    requestedAt: 1_000,
    startedAt: 1_001,
    completedAt: null,
    updatedAt: 1_002,
    operatorPaused: false,
    dailyLimit: 500,
    dailyUsed: 405,
    quotaDay: '2026-08-15',
    nextPage: 202,
    discoveryComplete: false,
    seasonStartedAt: null,
    seasonEndedAt: null,
    todayAnalyzedCount: 0,
    ...overrides,
  };
}

function session(local: boolean): RecordingSessionDetail {
  return {
    id: 9,
    roomId: 100,
    liveStartTime: 1_000,
    state: 'closed',
    startedAt: 1_000,
    endedAt: 1_600,
    title: '直播标题',
    coverUrl: '',
    anchorUid: 1,
    anchorName: '主播',
    areaId: 2,
    areaName: '手游',
    parentAreaId: 3,
    parentAreaName: '游戏',
    liveEndTime: 1_600,
    partCount: 1,
    danmakuCount: 0,
    totalFileSizeBytes: 0,
    recordDurationSeconds: 600,
    uploadIntent: 'upload',
    uploadDecision: 'upload',
    submissionInherited: false,
    uploadResolutionState: 'job_created',
    uploadResolutionError: null,
    uploadSuppressed: false,
    deletionState: 'none',
    deletionError: null,
    sourceKind: 'live',
    highlightClipId: null,
    matchIndexState: null,
    matchCount: 0,
    matchPublicationState: null,
    matchChapterState: null,
    matchDescriptionState: null,
    matchCommentState: null,
    matchCommentCount: 0,
    matchConfirmedCommentCount: 0,
    matchPublicationError: null,
    displayState: 'completed',
    availableActions: [],
    uploadJob: null,
    broadcastSessionKey: '100:1000',
    coverPath: null,
    parts: [
      {
        id: 7,
        runId: 'run-1',
        partIndex: 1,
        sourcePath: '/rec/p1.mp4',
        finalPath: null,
        xmlPath: null,
        recordStartTime: 1_000,
        recordEndTime: 1_600,
        recordDurationSeconds: 600,
        fileSizeBytes: null,
        danmakuCount: 0,
        artifactState: local ? 'ready' : 'missing',
        xmlCompleted: true,
        sourceExists: local,
        finalExists: false,
        errorMessage: null,
      },
    ],
  };
}

describe('VaingloryComponent remote media', () => {
  let component: VaingloryComponent;
  let recordings: jasmine.SpyObj<RecordingSessionService>;
  let messages: jasmine.SpyObj<NzMessageService>;
  let router: jasmine.SpyObj<Router>;
  let vainglory: jasmine.SpyObj<VaingloryService>;
  let accounts: jasmine.SpyObj<BiliAccountService>;
  let tasks: jasmine.SpyObj<TaskService>;
  let realtimeEvents: Subject<RealtimeEvent>;

  beforeEach(() => {
    vainglory = jasmine.createSpyObj<VaingloryService>('VaingloryService', [
      'listMatchSessions',
      'listZeroMatchSessions',
      'suppressZeroMatchSession',
      'restoreZeroMatchSession',
      'listMatches',
      'listDuplicateReviews',
      'listHeroes',
      'listAnchorStats',
      'listPlayers',
      'createPlayer',
      'renamePlayer',
      'setPlayerPublicVisibility',
      'bindPlayerRoom',
      'unbindPlayerRoom',
      'listPlayerStats',
      'listHeroStats',
      'updateSessionTitle',
      'updateSessionAnchor',
      'bulkUpdateSessions',
      'requestScan',
      'retryPublicationStep',
      'requestArchiveSync',
      'getArchiveSync',
      'listArchiveSyncItems',
      'updateArchiveSync',
      'listArchiveContentReviews',
      'listHeroReviews',
      'listRecordedPlayerReviews',
      'setRecordedPlayer',
      'setPlayerHero',
      'reviewMatchDuplicate',
      'reanalyzeMatch',
      'suppressMatchReview',
      'addAnalysisWorker',
      'updateAnalysisWorker',
    ]);
    recordings = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['getSession', 'requestRemoteMedia', 'getRemoteMediaStatus'],
    );
    vainglory.listAnchorStats.and.returnValue(of([]));
    vainglory.listHeroes.and.returnValue(of([]));
    vainglory.listZeroMatchSessions.and.returnValue(
      of({ total: 0, items: [] }),
    );
    vainglory.listPlayers.and.returnValue(of([]));
    vainglory.listPlayerStats.and.returnValue(of([]));
    vainglory.listHeroStats.and.returnValue(of([]));
    vainglory.listArchiveContentReviews.and.returnValue(
      of({ total: 0, items: [] }),
    );
    vainglory.listRecordedPlayerReviews.and.returnValue(
      of({ total: 0, items: [] }),
    );
    vainglory.listDuplicateReviews.and.returnValue(
      of({ total: 0, items: [] }),
    );
    vainglory.listHeroReviews.and.returnValue(of({ total: 0, items: [] }));
    vainglory.listArchiveSyncItems.and.returnValue(of([]));
    recordings.getSession.and.returnValue(of(session(true)));
    messages = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'info',
      'success',
      'error',
      'warning',
    ]);
    router = jasmine.createSpyObj<Router>('Router', ['navigate']);
    router.navigate.and.resolveTo(true);
    accounts = jasmine.createSpyObj<BiliAccountService>('BiliAccountService', [
      'listAccounts',
    ]);
    accounts.listAccounts.and.returnValue(of([]));
    tasks = jasmine.createSpyObj<TaskService>('TaskService', [
      'getAllTaskData',
    ]);
    tasks.getAllTaskData.and.returnValue(of([]));
    realtimeEvents = new Subject<RealtimeEvent>();
    const route = {
      queryParamMap: NEVER,
    } as Pick<ActivatedRoute, 'queryParamMap'>;
    const changeDetector = jasmine.createSpyObj<ChangeDetectorRef>(
      'ChangeDetectorRef',
      ['markForCheck'],
    );
    component = new VaingloryComponent(
      vainglory,
      recordings,
      route as ActivatedRoute,
      messages,
      changeDetector,
      router,
      accounts,
      tasks,
      {
        events$: realtimeEvents.asObservable(),
      } as Pick<RealtimeService, 'events$'> as RealtimeService,
    );
  });

  it('shows the audit reason for a match excluded from rankings', () => {
    expect(
      component.statsExclusionLabel({
        ...match(),
        statsEligible: false,
        statsExclusionReason: 'observed',
      }),
    ).toBe('观战或回放');
  });

  it('only downloads missing match video after an explicit request', () => {
    const missing: RemoteMediaStatus = {
      partId: 7,
      state: 'missing',
      progress: 0,
      remoteAvailable: true,
      accountId: 2,
      bvid: 'BV1abcdefgh',
      cid: 77,
      page: 1,
      downloadedBytes: 0,
      totalBytes: 100,
      cachedAt: null,
      expiresAt: null,
      error: null,
    };
    const downloading: RemoteMediaStatus = {
      partId: 7,
      state: 'downloading',
      progress: 0.25,
      remoteAvailable: true,
      accountId: 2,
      bvid: 'BV1abcdefgh',
      cid: 77,
      page: 1,
      downloadedBytes: 25,
      totalBytes: 100,
      cachedAt: null,
      expiresAt: null,
      error: null,
    };
    recordings.getSession.and.returnValue(of(session(false)));
    recordings.requestRemoteMedia.and.returnValue(of(downloading));
    component.recordingParts.set(7, session(false).parts[0]);
    component.remoteMediaStatuses.set(7, missing);

    component.openMatch(match());

    expect(recordings.requestRemoteMedia).not.toHaveBeenCalled();
    expect(component.previewVisible).toBeFalse();
    expect(messages.info).toHaveBeenCalled();

    component.downloadMatchMedia(match());

    expect(recordings.requestRemoteMedia).toHaveBeenCalledOnceWith(7);
    expect(component.previewVisible).toBeFalse();
    expect(component.remoteMediaPercent(7)).toBe(25);
  });

  it('opens local media and routes local clips to the match start', () => {
    const localSession = session(true);
    recordings.getSession.and.returnValue(of(localSession));
    component.recordingParts.set(7, localSession.parts[0]);

    component.openMatch(match());
    component.closePreview();
    component.openMatchClip(match());

    expect(component.previewVisible).toBeFalse();
    expect(router.navigate).toHaveBeenCalledOnceWith(
      ['/recordings/highlights', '9'],
      { queryParams: { partId: 7, seekMs: 15_000 } },
    );
    expect(recordings.requestRemoteMedia).not.toHaveBeenCalled();
  });

  it('opens the analysis image browser at the first image and uses arrow keys', () => {
    const earlier = match();
    const later = { ...match(), id: 4, resultAtMs: 700_000 };
    vainglory.listMatches.and.returnValue(
      of({ total: 2, items: [later, earlier] }),
    );

    component.openAnalysisImageBrowser({
      sessionId: 9,
      title: '直播标题 · 已识别对局',
    });

    expect(component.analysisImageBrowserVisible).toBeTrue();
    expect(component.analysisImageBrowserIndex).toBe(0);
    expect(component.currentAnalysisImage?.id).toBe(3);

    component.handleAnalysisImageKeydown(
      new KeyboardEvent('keydown', { key: 'ArrowRight' }),
    );

    expect(component.analysisImageBrowserIndex).toBe(1);
    expect(component.currentAnalysisImage?.id).toBe(4);
  });

  it('ignores one review without deleting its match', () => {
    component.heroReviewView = {
      state: 'ready',
      total: 1,
      items: [match()],
    };
    vainglory.suppressMatchReview.and.returnValue(of(void 0));

    component.ignoreMatchReview(match(), 'hero');

    expect(vainglory.suppressMatchReview).toHaveBeenCalledOnceWith(3, 'hero');
    expect(component.heroReviewTotal).toBe(0);
    expect(component.heroReviews).toEqual([]);
    expect(messages.success).toHaveBeenCalledWith(
      '已从当前待确认列表忽略，对局和统计数据保持不变',
    );
  });

  it('lets the user start historical backfill for a chosen account', () => {
    const account: BiliAccount = {
      id: 7,
      uid: 42,
      displayName: '旧账号',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 2_000,
      createdAt: 1_000,
      state: 'active',
      isPrimary: false,
    };
    accounts.listAccounts.and.returnValue(of([account]));
    vainglory.getArchiveSync.and.returnValue(
      of({
        accountId: 7,
        state: 'ready',
        progress: 1,
        discoveredCount: 10,
        completedCount: 10,
        error: null,
        requestedAt: 1_000,
        startedAt: 1_001,
        completedAt: 1_002,
        updatedAt: 1_002,
        operatorPaused: false,
        dailyLimit: 20,
        dailyUsed: 10,
        quotaDay: '1970-01-01',
        nextPage: 2,
        discoveryComplete: true,
        seasonStartedAt: null,
        seasonEndedAt: null,
        todayAnalyzedCount: 0,
      }),
    );
    vainglory.requestArchiveSync.and.returnValue(
      of({
        accountId: 7,
        state: 'discovering',
        progress: 0,
        discoveredCount: 0,
        completedCount: 0,
        error: null,
        requestedAt: 2_000,
        startedAt: null,
        completedAt: null,
        updatedAt: 2_000,
        operatorPaused: false,
        dailyLimit: 20,
        dailyUsed: 0,
        quotaDay: null,
        nextPage: 1,
        discoveryComplete: false,
        seasonStartedAt: null,
        seasonEndedAt: null,
        todayAnalyzedCount: 0,
      }),
    );

    component.openArchiveManager();
    component.requestArchiveSync(account);

    expect(component.archiveManagerVisible).toBeTrue();
    expect(component.archiveAccounts).toEqual([account]);
    expect(vainglory.requestArchiveSync).toHaveBeenCalledOnceWith(7, false);
    expect(component.archiveSyncs.get(7)?.state).toBe('discovering');
  });

  it('separates current downloads from the next waiting archives', () => {
    component.archiveItemsByAccountId = new Map([
      [
        7,
        [
          archiveItem(1, 'analysis_pending'),
          archiveItem(2, 'download_pending'),
          archiveItem(3, 'downloading'),
          archiveItem(4, 'queued'),
          archiveItem(5, 'completed'),
        ],
      ],
    ]);

    expect(component.archiveDownloadingItems(7).map((item) => item.id)).toEqual([
      3,
    ]);
    expect(
      component.archiveWaitingDownloadItems(7).map((item) => item.id),
    ).toEqual([2, 4]);
  });

  it('describes archive discovery and quota in user-facing terms', () => {
    const running = archiveSync();
    const completed = archiveSync({
      state: 'ready',
      discoveryComplete: true,
    });

    expect(component.archiveSyncLabel(running)).toBe(
      '已处理 1832 / 已发现 2148',
    );
    expect(component.archiveDiscoveryLabel(running)).toBe(
      '已扫描 201 页，总页数待确认；已收录 2148 个稿件（按 BV 号去重）',
    );
    expect(component.archiveDiscoveryLabel(completed)).toBe(
      '稿件列表扫描完成：共 201 页，已收录 2148 个稿件（按 BV 号去重）',
    );
  });

  it('loads matches only after a recording session drawer is opened', () => {
    const summary: VaingloryMatchSession = {
      sessionId: 9,
      title: '直播标题',
      sourceTitle: '原始直播标题',
      anchorName: '主播',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: null,
      descriptionState: null,
      pinState: null,
      chapterState: null,
      publicationPriority: false,
      publicationUpdatedAt: null,
    };
    vainglory.listMatches.and.returnValue(of({ total: 1, items: [match()] }));

    component.openSessionDetails(summary);

    expect(component.detailsDrawerVisible).toBeTrue();
    expect(component.selectedSession).toBe(summary);
    expect(vainglory.listMatches).toHaveBeenCalledOnceWith(
      {
        playerName: '',
        heroIds: [],
        winnerColor: null,
        gameMode: null,
        sessionId: 9,
      },
      100,
      0,
    );
    expect(component.detailsFor(9)).toEqual({
      state: 'ready',
      items: [match()],
    });
  });

  it('keeps an open session detail stable when the realtime index changes', () => {
    const summary: VaingloryMatchSession = {
      sessionId: 9,
      title: '直播标题',
      sourceTitle: '原始直播标题',
      anchorName: '主播',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: null,
      descriptionState: null,
      pinState: null,
      chapterState: null,
      publicationPriority: false,
      publicationUpdatedAt: null,
    };
    const loadedMatch = match();
    vainglory.listMatches.and.returnValues(
      of({ total: 1, items: [loadedMatch] }),
      NEVER,
    );
    vainglory.listMatchSessions.and.returnValue(
      of({ total: 1, items: [summary] }),
    );
    component.ngOnInit();
    component.openSessionDetails(summary);

    realtimeEvents.next({ type: 'vainglory_index', data: indexSnapshot(1) });
    realtimeEvents.next({ type: 'vainglory_index', data: indexSnapshot(2) });

    expect(vainglory.listMatchSessions).toHaveBeenCalledTimes(1);
    expect(vainglory.listMatches).toHaveBeenCalledTimes(1);
    expect(component.detailsFor(9)).toEqual({
      state: 'ready',
      items: [loadedMatch],
    });
    component.ngOnDestroy();
  });

  it('saves one title for the whole recording session', () => {
    const summary: VaingloryMatchSession = {
      sessionId: 9,
      title: '旧标题',
      sourceTitle: '原始直播标题',
      anchorName: '主播',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: null,
      descriptionState: null,
      pinState: null,
      chapterState: null,
      publicationPriority: false,
      publicationUpdatedAt: null,
    };
    const saved = { ...summary, title: '整场新标题' };
    vainglory.listMatches.and.returnValue(of({ total: 0, items: [] }));
    vainglory.updateSessionTitle.and.returnValue(of(saved));
    component.openSessionDetails(summary);
    component.sessionTitleDraft = '整场新标题';

    component.saveSessionTitle();

    expect(vainglory.updateSessionTitle).toHaveBeenCalledOnceWith(
      9,
      '整场新标题',
    );
    expect(component.selectedSession).toEqual(saved);
  });

  it('saves a manually corrected anchor for the whole session', () => {
    const summary: VaingloryMatchSession = {
      sessionId: 9,
      title: '直播标题',
      sourceTitle: '原始直播标题',
      anchorName: '',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: null,
      descriptionState: null,
      pinState: null,
      chapterState: null,
      publicationPriority: false,
      publicationUpdatedAt: null,
    };
    const saved = { ...summary, anchorName: '玩不明白' };
    vainglory.listMatches.and.returnValue(of({ total: 0, items: [] }));
    vainglory.updateSessionAnchor.and.returnValue(of(saved));
    component.openSessionDetails(summary);
    component.sessionAnchorDraft = '玩不明白';

    component.saveSessionAnchor();

    expect(vainglory.updateSessionAnchor).toHaveBeenCalledOnceWith(
      9,
      '玩不明白',
    );
    expect(component.selectedSession).toEqual(saved);
  });

  it('bulk excludes selected sessions from anchor statistics', () => {
    vainglory.bulkUpdateSessions.and.returnValue(of({ updatedCount: 2 }));
    vainglory.listMatchSessions.and.returnValue(of({ total: 0, items: [] }));
    component.selectedSessionIds.add(9);
    component.selectedSessionIds.add(10);

    component.bulkSetStatsIncluded(false);

    expect(vainglory.bulkUpdateSessions).toHaveBeenCalledOnceWith([9, 10], {
      statsIncluded: false,
    });
    expect(component.selectedSessionIds.size).toBe(0);
    expect(messages.success).toHaveBeenCalledWith('已更新 2 场直播');
  });

  it('keeps failed sessions selected when batch reanalysis partly fails', () => {
    vainglory.requestScan.and.callFake((sessionId) =>
      sessionId === 9
        ? of(scanJob(sessionId))
        : throwError(() => new Error('队列暂不可用')),
    );
    component.selectedSessionIds.add(9);
    component.selectedSessionIds.add(10);

    component.bulkRescanSelected();

    expect(vainglory.requestScan.calls.allArgs()).toEqual([[9], [10]]);
    expect(component.selectedSessionIds.has(9)).toBeFalse();
    expect(component.selectedSessionIds.has(10)).toBeTrue();
    expect(messages.warning).toHaveBeenCalledWith(
      '已加入 1 场，1 场失败；失败项已保留选中',
    );
  });

  it('loads zero-match sessions and refreshes them after requeueing one', () => {
    vainglory.listZeroMatchSessions.and.returnValue(
      of({
        total: 1,
        items: [
          {
            sessionId: 12,
            title: '未识别直播',
            sourceTitle: '原始标题',
            anchorName: '主播',
            startedAt: 1_000,
            completedAt: 2_000,
            recordingDurationSeconds: 7_200,
            partCount: 3,
            bvid: 'BV1zero12345',
          },
        ],
      }),
    );
    vainglory.requestScan.and.returnValue(of(scanJob(12)));

    component.loadZeroMatchSessions();
    component.requestSessionRescan(component.zeroMatchSessions[0]);

    expect(component.zeroMatchSessionTotal).toBe(1);
    expect(vainglory.requestScan).toHaveBeenCalledOnceWith(12);
    expect(vainglory.listZeroMatchSessions).toHaveBeenCalledTimes(2);
    expect(messages.success).toHaveBeenCalledWith(
      '已加入重新分析队列，并提升为手动优先',
    );
  });

  it('confirms a zero-match session should not be scanned again', () => {
    vainglory.listZeroMatchSessions.and.returnValue(
      of({
        total: 1,
        items: [
          {
            sessionId: 12,
            title: '其他游戏直播',
            sourceTitle: '其他游戏直播',
            anchorName: '主播',
            startedAt: 1_000,
            completedAt: 2_000,
            recordingDurationSeconds: 7_200,
            partCount: 3,
            bvid: 'BV1zero12345',
          },
        ],
      }),
    );
    vainglory.suppressZeroMatchSession.and.returnValue(of(void 0));

    component.loadZeroMatchSessions();
    component.suppressZeroMatchSession(component.zeroMatchSessions[0]);

    expect(vainglory.suppressZeroMatchSession).toHaveBeenCalledOnceWith(12);
    expect(vainglory.listZeroMatchSessions.calls.allArgs()).toEqual([
      [20, 0],
      [20, 0],
      [20, 0, true],
    ]);
    expect(messages.success).toHaveBeenCalledWith(
      '已确认无需扫描，今后的批量重扫也会跳过这场直播',
    );
  });

  it('retries a failed publication step and clears its failed label', () => {
    const failed: VaingloryMatchSession = {
      sessionId: 9,
      title: '直播标题',
      sourceTitle: '原始直播标题',
      anchorName: '主播',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: 'failed',
      descriptionState: 'confirmed',
      pinState: 'in_flight',
      chapterState: 'confirmed',
      publicationPriority: false,
      publicationUpdatedAt: null,
    };
    component.sessionsView = { state: 'ready', total: 1, items: [failed] };
    vainglory.retryPublicationStep.and.returnValue(of(void 0));

    component.retryPublicationStep(failed, 'pin');

    expect(vainglory.retryPublicationStep).toHaveBeenCalledOnceWith(9, 'pin');
    expect(component.isPublicationStepRetrying(9, 'pin')).toBeFalse();
    expect(component.sessions[0].publicationState).toBe('prepared');
    expect(component.sessions[0].pinState).toBe('prepared');
    expect(messages.success).toHaveBeenCalledWith(
      '已将置顶评论加入发布专用队列，并提升为最高优先级',
    );
  });

  it('shows the real step state while an automatic retry is scheduled', () => {
    const paused: VaingloryMatchSession = {
      sessionId: 9,
      title: '直播标题',
      sourceTitle: '原始直播标题',
      anchorName: '主播',
      startedAt: 1_000,
      liveStartedAt: 1_000,
      partCount: 1,
      originalPartCount: 1,
      ignoredPartCount: 0,
      recordingDurationSeconds: 585,
      matchCount: 1,
      tealWinCount: 1,
      orangeWinCount: 0,
      winCount: 1,
      lossCount: 0,
      unknownCount: 0,
      surrenderCount: 0,
      durationSeconds: 585,
      gameModes: ['3v3'],
      publicationState: 'paused',
      descriptionState: 'prepared',
      pinState: 'prepared',
      chapterState: 'prepared',
      publicationPriority: false,
      publicationUpdatedAt: 1_500,
    };

    expect(component.descriptionStateLabel(paused)).toBe('等待回填');
    expect(component.pinStateLabel(paused)).toBe('等待处理');
    expect(component.chapterStateLabel(paused)).toBe('等待设置');
    expect(component.publicationRetryLabel(paused, 'chapter')).toBe('重试');

    const failedChapter = { ...paused, publicationState: 'failed' as const };
    expect(component.descriptionStateLabel(failedChapter)).toBe('等待回填');
    expect(component.pinStateLabel(failedChapter)).toBe('等待处理');
    expect(component.chapterStateLabel(failedChapter)).toBe('失败');

    const invalidAnalysis = {
      ...failedChapter,
      publicationStatus: 'analysis_data_invalid' as const,
    };
    expect(
      component.canRetryPublicationStep(invalidAnalysis, 'chapter'),
    ).toBeFalse();
    const legacyTiming = {
      ...paused,
      publicationStatus: 'legacy_chapter_timing' as const,
    };
    expect(component.canRetryPublicationStep(legacyTiming, 'chapter')).toBeTrue();
    expect(
      component.canRetryPublicationStep(legacyTiming, 'description'),
    ).toBeFalse();
  });

  it('creates a player, renames it, and manages its room binding', () => {
    const player: VaingloryPlayer = {
      id: 5,
      name: '直播名',
      origin: 'automatic',
      publicVisible: true,
      rooms: [],
      createdAt: 1_000,
      updatedAt: 1_000,
    };
    const renamed = { ...player, name: '游戏名' };
    const hidden = { ...renamed, publicVisible: false };
    const bound = {
      ...renamed,
      rooms: [{ roomId: 100, anchorUid: 42, anchorName: '直播名' }],
    };
    vainglory.createPlayer.and.returnValue(of(player));
    vainglory.renamePlayer.and.returnValue(of(renamed));
    vainglory.setPlayerPublicVisibility.and.returnValue(of(hidden));
    vainglory.bindPlayerRoom.and.returnValue(of(bound));
    vainglory.unbindPlayerRoom.and.returnValue(of(renamed));

    component.newPlayerName = ' 手工玩家 ';
    component.createPlayer();
    component.setPlayerNameDraft(player.id, ' 游戏名 ');
    component.savePlayerName(player);
    component.playersView = { state: 'ready', items: [renamed] };
    component.setPlayerPublicVisibility(renamed, false);
    expect(component.playerLibrary[0].publicVisible).toBeFalse();
    component.setPlayerRoomDraft(player.id, 100);
    component.bindPlayerRoom(renamed);
    component.unbindPlayerRoom(bound, 100);

    expect(vainglory.createPlayer).toHaveBeenCalledOnceWith('手工玩家');
    expect(vainglory.renamePlayer).toHaveBeenCalledOnceWith(5, '游戏名');
    expect(vainglory.setPlayerPublicVisibility).toHaveBeenCalledOnceWith(
      5,
      false,
    );
    expect(vainglory.bindPlayerRoom).toHaveBeenCalledOnceWith(5, 100);
    expect(vainglory.unbindPlayerRoom).toHaveBeenCalledOnceWith(5, 100);
    expect(component.newPlayerName).toBe('');
    expect(component.playerRoomDraft(5)).toBe('');
  });

  it('confirms a suspected duplicate from the match card', () => {
    const suspected: VaingloryMatch = {
      ...match(),
      statsEligible: false,
      statsExclusionReason: 'duplicate',
      duplicateOfMatchId: 2,
      duplicateResultFrameUrl: '/api/v1/vainglory/matches/2/result-frame',
      duplicateReviewState: 'pending',
    };
    const confirmed: VaingloryMatch = {
      ...suspected,
      duplicateReviewState: 'confirmed',
    };
    component.matchDetails.set(9, { state: 'ready', items: [suspected] });
    component.duplicateReviewView = {
      state: 'ready',
      total: 1,
      items: [suspected],
    };
    vainglory.reviewMatchDuplicate.and.returnValue(of(confirmed));

    component.reviewMatchDuplicate(suspected, 'confirmed');

    expect(vainglory.reviewMatchDuplicate).toHaveBeenCalledOnceWith(
      3,
      'confirmed',
    );
    expect(component.detailsFor(9)?.state).toBe('ready');
    expect(component.matchDetails.get(9)).toEqual({
      state: 'ready',
      items: [confirmed],
    });
    expect(component.duplicateReviews).toEqual([]);
    expect(component.duplicateReviewTotal).toBe(0);
    expect(messages.success).toHaveBeenCalledWith(
      '已确认重复，这一局继续不计分',
    );
  });
});
