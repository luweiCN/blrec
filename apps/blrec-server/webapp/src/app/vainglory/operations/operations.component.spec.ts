import { ChangeDetectorRef } from '@angular/core';
import { Router } from '@angular/router';

import { of, Subject } from 'rxjs';

import {
  RealtimeEvent,
  RealtimeService,
} from '../../core/services/realtime.service';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  VaingloryAnalysisQueue,
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

describe('OperationsComponent', () => {
  it('separates worker, queue, history, and publication evidence summaries', () => {
    const events = new Subject<RealtimeEvent>();
    const changeDetector = jasmine.createSpyObj<ChangeDetectorRef>(
      'ChangeDetectorRef',
      ['markForCheck'],
    );
    const vainglory = jasmine.createSpyObj<VaingloryService>(
      'VaingloryService',
      ['getPublicationAudit', 'queuePublicationAudit'],
    );
    vainglory.getPublicationAudit.and.returnValue(of(audit()));
    vainglory.queuePublicationAudit.and.returnValue(
      of({ ...audit(0), queuedCount: 2 }),
    );
    const accounts = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      ['listAccounts', 'listArchiveMigrations'],
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
          },
        ],
        items: { '7': [] },
      },
    });

    expect(component.onlineWorkerCount).toBe(1);
    expect(component.pendingTaskCount).toBe(7);
    expect(component.activeHistoryCount).toBe(1);
    expect(component.publicationAudit?.verifiedCount).toBe(24);

    component.queuePublicationAudit();

    expect(vainglory.queuePublicationAudit).toHaveBeenCalledOnceWith(168, 20);
    expect(component.actionMessage).toContain('2 条稿件');
    component.ngOnDestroy();
  });
});
