import { AnalysisTaskCenterComponent } from './analysis-task-center.component';
import {
  VaingloryAnalysisQueue,
  VaingloryAnalysisQueueItem,
  VaingloryAnalysisSummary,
} from '../vainglory.model';

describe('AnalysisTaskCenterComponent', () => {
  it('calculates visible task elapsed time from the SSE sample', () => {
    const component = new AnalysisTaskCenterComponent();
    const item: VaingloryAnalysisQueueItem = {
      partId: 7,
      sessionId: 9,
      partIndex: 1,
      title: '直播标题',
      anchorName: '主播',
      state: 'analyzing',
      stage: 'video_scan',
      category: 'realtime',
      progress: 0.5,
      requestedAt: 1_100,
      startedAt: 1_200,
      updatedAt: 1_250,
      liveStartedAt: 1_000,
      partDurationSeconds: 7_200,
      recordingDurationSeconds: 10_800,
      matchCount: 2,
      partCount: 2,
      completedPartCount: 1,
      originalPartCount: 3,
      ignoredPartCount: 1,
      runtimeStage: 'fine_scan',
      runtimeDetail: '正在精扫第 2/4 个疑似结算区间',
      runtimeElapsedSeconds: 120,
      coarseFrames: 240,
      gameplayRuns: 5,
      resultWindows: 4,
      currentWindow: 2,
      totalWindows: 4,
      candidateCount: 1,
      currentCandidate: 0,
      totalCandidates: 0,
      rejectedCandidates: 0,
      recognizedMatches: 0,
      modelPackageId: 'vision-package-v1',
      keyframeFrames: 180,
      seekFillFrames: 60,
      decodedResultFrames: 0,
      modeConflictCount: 0,
      hudLineupCandidateCount: 0,
      trainingCandidateCount: 0,
      events: [],
      imageCount: 2,
      matchPreviews: [],
    };
    component.sampledAt = 1_500;

    expect(component.elapsedSeconds(item)).toBe(300);
    expect(component.stageLabel(item)).toBe('4 FPS 结算精扫');

    let browserRequest: unknown;
    let sessionRequest: number | undefined;
    component.browseMatches.subscribe((value) => (browserRequest = value));
    component.viewSession.subscribe((value) => (sessionRequest = value));

    component.requestImageBrowser(item, 'session');
    component.requestDetails(item.sessionId);

    expect(browserRequest).toEqual({
      sessionId: 9,
      title: '直播标题 · 已识别对局',
    });
    expect(sessionRequest).toBe(9);
  });

  it('formats model output and training candidate counts for the new pipeline', () => {
    const component = new AnalysisTaskCenterComponent();
    const summary: VaingloryAnalysisSummary = {
      schemaVersion: 1,
      pipeline: 'timeline-v2',
      modelPackageId: 'vision-package-v1',
      sampledFrames: 10,
      keyframeFrames: 7,
      seekFillFrames: 3,
      decodedResultFrames: 20,
      resultHitFrames: 4,
      resultCandidateCount: 2,
      hudLineupCandidateCount: 2,
      modeConflictCount: 1,
      timelineCounts: {
        matchFlow: { match_flow: 8, not_match_flow: 2 },
        heroSelect: { not_select: 9, select_3v3: 1 },
        matchMode: { '3v3': 7, aram: 3 },
      },
      timelineSegments: [],
      resultWindows: [],
      trainingCandidateCounts: { match_mode: 2, result_detector: 3 },
      timingsSeconds: { scanTotal: 12.5 },
    };

    expect(component.timelineCountText(summary, 'matchMode')).toBe(
      '3V3 7 · 大乱斗 3',
    );
    expect(component.trainingCandidateText(summary)).toBe(
      '结算检测 3 · 对局模式 2',
    );
    expect(component.trainingCandidateCount(summary)).toBe(5);
  });

  it('shows the loaded worker model and detects an active task version mismatch', () => {
    const component = new AnalysisTaskCenterComponent();
    const active = {
      modelPackageId: 'vision-package-v1',
    } as VaingloryAnalysisQueueItem;
    const queue = {
      workerState: 'running',
      worker: {
        state: 'running',
        remoteEnabled: true,
        workerId: 'macbook-pro',
        modelPackageId: 'vision-package-v2',
        pipelineVersion: 'timeline-v2',
        lastSeenAt: 1_500,
      },
      workers: [
        {
          state: 'running',
          workerId: 'macbook-pro',
          displayName: 'MacBook Pro',
          enabled: true,
          modelPackageId: 'vision-package-v2',
          pipelineVersion: 'timeline-v2',
          lastSeenAt: 1_500,
          activeTaskCount: 1,
          activePartIds: [7],
          concurrency: 3,
          completedTaskCount: 12,
          failedTaskCount: 1,
          totalProcessingSeconds: 260,
          profiledTaskCount: 4,
          profiledVideoSeconds: 7_200,
          totalDecodeAnalysisSeconds: 240,
          totalProfiledTaskSeconds: 360,
          lastTaskFinishedAt: 1_400,
        },
      ],
      active: [active],
      queued: [],
      recentCompletions: [],
      pendingCount: 0,
      manualPending: 0,
      realtimePending: 0,
      archivePending: 0,
      migrationPending: 0,
      backlogPending: 0,
    } satisfies VaingloryAnalysisQueue;

    expect(component.workerModelMismatch(queue, queue.workers[0])).toBeFalse();
    expect(
      component.workerMinutesPerVideoHour(queue.workers[0], 'decodeAnalysis'),
    ).toBe(2);
    expect(
      component.workerMinutesPerVideoHour(queue.workers[0], 'wholeTask'),
    ).toBe(3);

    const assignedQueue = {
      ...queue,
      active: [{ ...active, workerId: 'macbook-pro' }],
    };
    expect(
      component.workerModelMismatch(assignedQueue, queue.workers[0]),
    ).toBeTrue();
    expect(component.workerModelState(assignedQueue, queue.workers[0])).toBe(
      '任务版本与当前版本不一致',
    );

    const idleQueue = { ...queue, active: [] };
    const idleWorker = { ...queue.workers[0], activeTaskCount: 0 };
    expect(component.workerTaskLabel(idleQueue, idleWorker)).toBe(
      '空闲，正在轮询新任务',
    );

    const pausedWorker = { ...idleWorker, enabled: false };
    expect(component.workerLabel(pausedWorker)).toBe('已暂停');
    expect(component.workerTaskLabel(idleQueue, pausedWorker)).toBe(
      '已停止领取新任务',
    );
  });
});
