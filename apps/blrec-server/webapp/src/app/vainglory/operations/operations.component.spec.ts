import { ChangeDetectorRef } from '@angular/core';
import { Router } from '@angular/router';

import { NEVER, of, Subject } from 'rxjs';

import {
  RealtimeEvent,
  RealtimeService,
} from '../../core/services/realtime.service';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  VaingloryAnalysisQueue,
  VaingloryArchiveDownloadQueue,
  VaingloryPublicationAudit,
} from '../vainglory.model';
import { VaingloryService } from '../vainglory.service';
import { OperationsComponent } from './operations.component';

function analysisQueue(): VaingloryAnalysisQueue {
  return {
    workerState: 'running',
    worker: {
      state: 'running',
      remoteEnabled: true,
      workerId: 'worker-1',
      modelPackageId: 'model-v1',
      pipelineVersion: 'timeline-v2',
      lastSeenAt: 1_000,
    },
    workers: [
      {
        state: 'running',
        workerId: 'worker-1',
        displayName: '分析节点',
        enabled: true,
        modelPackageId: 'model-v1',
        pipelineVersion: 'timeline-v2',
        lastSeenAt: 1_000,
        activeTaskCount: 1,
        activePartIds: [3],
        concurrency: 2,
        completedTaskCount: 20,
        failedTaskCount: 1,
        totalProcessingSeconds: 3_600,
        profiledTaskCount: 10,
        profiledVideoSeconds: 18_000,
        totalDecodeAnalysisSeconds: 600,
        totalProfiledTaskSeconds: 900,
        lastTaskFinishedAt: 990,
      },
    ],
    active: [],
    queued: [],
    recentCompletions: [],
    pendingCount: 7,
    manualPending: 0,
    realtimePending: 2,
    archivePending: 5,
    migrationPending: 0,
    backlogPending: 0,
    liveStreamCount: 1,
    liveRunningCount: 1,
    livePendingWindowCount: 0,
    liveSampleCount: 10,
    liveProvisionalMatchCount: 1,
    liveLastObservedAt: 1_000,
    liveItems: [],
  };
}

function audit(staleCount = 2): VaingloryPublicationAudit {
  return {
    totalCount: 30,
    verifiedCount: 24,
    staleCount,
    pendingCount: 5,
    failedCount: 1,
    oldestVerifiedAt: 900,
    staleBefore: 1_000,
  };
}

function downloadQueue(): VaingloryArchiveDownloadQueue {
  return {
    pendingDownloadCount: 2_385,
    activeDownloadCount: 6,
    downloadedWaitingAnalysisCount: 243,
    activeAnalysisCount: 3,
    failedDownloadCount: 86,
    downloadsPerInterface: 3,
    interfaceCount: 2,
    totalConcurrency: 6,
  };
}

describe('OperationsComponent', () => {
  it('separates live, worker, queue, history, and publication evidence views', () => {
    const events = new Subject<RealtimeEvent>();
    const changeDetector = jasmine.createSpyObj<ChangeDetectorRef>(
      'ChangeDetectorRef',
      ['markForCheck'],
    );
    const vainglory = jasmine.createSpyObj<VaingloryService>(
      'VaingloryService',
      [
        'getPublicationAudit',
        'queuePublicationAudit',
        'listPublicationRecords',
        'retryPublication',
        'retryPublicationStep',
        'requestScan',
        'updateAnalysisWorker',
        'getArchiveDownloadQueue',
        'updateArchiveDownloadQueue',
      ],
    );
    vainglory.getPublicationAudit.and.returnValue(of(audit()));
    vainglory.queuePublicationAudit.and.returnValue(
      of({ ...audit(0), queuedCount: 2 }),
    );
    vainglory.listPublicationRecords.and.returnValue(
      of({ total: 0, items: [] }),
    );
    vainglory.retryPublication.and.returnValue(of(void 0));
    vainglory.retryPublicationStep.and.returnValue(of(void 0));
    vainglory.updateAnalysisWorker.and.returnValue(
      of({ ...analysisQueue().workers[0], desiredConcurrency: 4 }),
    );
    vainglory.getArchiveDownloadQueue.and.returnValue(of(downloadQueue()));
    vainglory.updateArchiveDownloadQueue.and.returnValue(
      of({ ...downloadQueue(), downloadsPerInterface: 4, totalConcurrency: 8 }),
    );
    const accounts = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      [
        'listAccounts',
        'listArchiveMigrations',
        'retryArchiveMigrationItem',
      ],
    );
    accounts.listAccounts.and.returnValue(of([]));
    accounts.listArchiveMigrations.and.returnValue(of([]));
    const router = jasmine.createSpyObj<Router>('Router', ['navigate']);
    const realtime = { events$: events.asObservable() } as RealtimeService;
    const component = new OperationsComponent(
      changeDetector,
      realtime,
      vainglory,
      accounts,
      router,
    );

    component.ngOnInit();
    events.next({
      type: 'vainglory_index',
      data: {
        sampledAt: 1_001,
        analysisQueue: analysisQueue(),
        indexSummary: {
          matchCount: 0,
          sessionCount: 0,
          anchorCount: 0,
          unassignedSessionCount: 0,
          winCount: 0,
          lossCount: 0,
          unknownCount: 0,
          playerSlotCount: 0,
          recognizedHeroCount: 0,
        },
      },
    });
    events.next({
      type: 'archive_backfill',
      data: {
        syncs: [
          {
            accountId: 7,
            state: 'running',
            progress: 0.5,
            discoveredCount: 10,
            completedCount: 5,
            error: null,
            requestedAt: 900,
            startedAt: 901,
            completedAt: null,
            updatedAt: 1_000,
            operatorPaused: false,
            dailyLimit: 20,
            dailyUsed: 4,
            quotaDay: '2026-08-16',
            nextPage: 3,
            discoveryComplete: false,
            seasonStartedAt: null,
            seasonEndedAt: null,
            todayAnalyzedCount: 0,
          },
        ],
        items: { '7': [] },
      },
    });

    expect(component.onlineWorkerCount).toBe(1);
    expect(component.pendingTaskCount).toBe(7);
    expect(component.activeHistoryCount).toBe(1);
    expect(component.publicationAudit?.verifiedCount).toBe(24);
    expect(component.archiveDownloadQueue?.pendingDownloadCount).toBe(2_385);

    component.queuePublicationAudit();

    expect(vainglory.queuePublicationAudit).toHaveBeenCalledOnceWith(168, 20);
    expect(component.actionMessage).toContain('2 条稿件');

    component.setWorkerConcurrency({ workerId: 'worker-1', concurrency: 4 });
    expect(vainglory.updateAnalysisWorker).toHaveBeenCalledOnceWith(
      'worker-1',
      { desiredConcurrency: 4 },
    );
    expect(component.queue?.workers[0].desiredConcurrency).toBe(4);

    component.setArchiveDownloadConcurrency(4);
    component.saveArchiveDownloadConcurrency();
    expect(vainglory.updateArchiveDownloadQueue).toHaveBeenCalledOnceWith(4);
    expect(component.archiveDownloadQueue?.totalConcurrency).toBe(8);
    component.ngOnDestroy();
  });

  it('keeps publication records visible while a realtime resync reloads them', () => {
    const events = new Subject<RealtimeEvent>();
    const changeDetector = jasmine.createSpyObj<ChangeDetectorRef>(
      'ChangeDetectorRef',
      ['markForCheck'],
    );
    const vainglory = jasmine.createSpyObj<VaingloryService>(
      'VaingloryService',
      [
        'getPublicationAudit',
        'listPublicationRecords',
        'getArchiveDownloadQueue',
      ],
    );
    vainglory.getPublicationAudit.and.returnValues(of(audit()), NEVER);
    vainglory.listPublicationRecords.and.returnValues(
      of({
        total: 1,
        items: [
          {
            id: 7,
            sessionId: 9,
            bvid: 'BV1abcdefgh',
            title: '直播回放',
            sourceKind: 'upload',
            state: 'confirmed',
            visibilityScope: 'public',
            matchCount: 3,
            updatedAt: 1_000,
            remoteVerifiedAt: 1_000,
            statusCode: 'confirmed',
            statusLabel: '已验证',
            detail: null,
            recommendedAction: 'none',
            nextAttemptAt: null,
          },
        ],
      }),
      NEVER,
    );
    vainglory.getArchiveDownloadQueue.and.returnValues(
      of(downloadQueue()),
      NEVER,
    );
    const accounts = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      ['listAccounts', 'listArchiveMigrations'],
    );
    accounts.listAccounts.and.returnValues(of([]), NEVER);
    accounts.listArchiveMigrations.and.returnValues(of([]), NEVER);
    const component = new OperationsComponent(
      changeDetector,
      { events$: events.asObservable() } as RealtimeService,
      vainglory,
      accounts,
      jasmine.createSpyObj<Router>('Router', ['navigate']),
    );
    component.ngOnInit();

    events.next({ type: 'resync', data: {} });

    expect(component.publicationRecordsLoading).toBeFalse();
    expect(component.auditLoading).toBeFalse();
    expect(component.publicationRecords.map((record) => record.id)).toEqual([7]);
    component.ngOnDestroy();
  });

  it('opens paged processing and history queue summaries', () => {
    const vainglory = jasmine.createSpyObj<VaingloryService>(
      'VaingloryService',
      ['listAnalysisQueueItems', 'listArchiveSyncItemPage'],
    );
    vainglory.listAnalysisQueueItems.and.returnValue(
      of({ total: 21, items: [] }),
    );
    vainglory.listArchiveSyncItemPage.and.returnValue(
      of({ total: 22, items: [] }),
    );
    const accounts = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      ['listArchiveMigrationItemPage'],
    );
    accounts.listArchiveMigrationItemPage.and.returnValue(
      of({ total: 0, items: [] }),
    );
    const component = new OperationsComponent(
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
      {
        events$: new Subject<RealtimeEvent>().asObservable(),
      } as RealtimeService,
      vainglory,
      accounts,
      jasmine.createSpyObj<Router>('Router', ['navigate']),
    );
    component.archiveSyncs = [
      {
        accountId: 7,
        state: 'running',
        progress: 0.5,
        discoveredCount: 10,
        completedCount: 5,
        error: null,
        requestedAt: 900,
        startedAt: 901,
        completedAt: null,
        updatedAt: 1_000,
        operatorPaused: false,
        dailyLimit: 20,
        dailyUsed: 4,
        quotaDay: '2026-08-16',
        nextPage: 3,
        discoveryComplete: false,
        seasonStartedAt: null,
        seasonEndedAt: null,
        todayAnalyzedCount: 2,
      },
    ];

    component.openProcessingQueue();
    component.changeProcessingQueuePage(2);
    component.openHistoryQueue();
    component.changeHistoryQueuePage(2);

    expect(component.processingQueueVisible).toBeTrue();
    expect(component.processingQueueTotal).toBe(21);
    expect(vainglory.listAnalysisQueueItems.calls.allArgs()).toEqual([
      [20, 0],
      [20, 20],
    ]);
    expect(component.historyQueueVisible).toBeTrue();
    expect(component.historyQueueSource).toBe('archive:7');
    expect(component.historyQueueTotal).toBe(22);
    expect(vainglory.listArchiveSyncItemPage.calls.allArgs()).toEqual([
      [7, 20, 0],
      [7, 20, 20],
    ]);
    component.ngOnDestroy();
  });
});
