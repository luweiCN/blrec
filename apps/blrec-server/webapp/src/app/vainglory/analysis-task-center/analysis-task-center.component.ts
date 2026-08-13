import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';

import {
  VaingloryAnalysisQueue,
  VaingloryAnalysisQueueCategory,
  VaingloryAnalysisQueueCompletion,
  VaingloryAnalysisQueueEvent,
  VaingloryAnalysisQueueItem,
  VaingloryAnalysisWorkerNodeStatus,
  VaingloryAnalysisSummary,
  VaingloryAnalysisMatchPreview,
} from '../vainglory.model';

type TimelineCountGroup = 'matchFlow' | 'heroSelect' | 'matchMode';

const ANALYSIS_OUTPUT_LABELS: Readonly<Record<string, string>> = {
  match_flow: '对局中',
  not_match_flow: '非对局',
  select_3v3: '3V3 选英雄',
  select_aram: '大乱斗选英雄',
  select_5v5: '5V5 选英雄',
  not_select: '非选英雄',
  '3v3': '3V3',
  aram: '大乱斗',
  '5v5': '5V5',
  unknown: '不确定',
};

const TRAINING_TASK_LABELS: Readonly<Record<string, string>> = {
  match_flow: '是否在对局中',
  hero_select: '英雄选择',
  match_mode: '对局模式',
  result_detector: '结算检测',
  key_screen: '关键界面',
  hero_avatar: '头像位置',
  hero_identity: '英雄身份',
  player_position: '本人位置',
};

@Component({
  selector: 'app-vainglory-analysis-task-center',
  templateUrl: './analysis-task-center.component.html',
  styleUrls: ['./analysis-task-center.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AnalysisTaskCenterComponent {
  @Input() queue: VaingloryAnalysisQueue | null = null;
  @Input() sampledAt: number | null = null;
  @Input() updatingWorkerIds: ReadonlySet<string> = new Set<string>();
  @Output() addWorker = new EventEmitter<void>();
  @Output() editWorker = new EventEmitter<VaingloryAnalysisWorkerNodeStatus>();
  @Output() workerEnabledChange = new EventEmitter<{
    readonly workerId: string;
    readonly enabled: boolean;
  }>();
  @Output() playPart = new EventEmitter<{
    readonly sessionId: number;
    readonly partId: number;
  }>();
  @Output() viewSession = new EventEmitter<number>();
  @Output() browseMatches = new EventEmitter<{
    readonly sessionId: number;
    readonly partId?: number;
    readonly title: string;
  }>();

  toggleWorker(worker: VaingloryAnalysisWorkerNodeStatus): void {
    this.workerEnabledChange.emit({
      workerId: worker.workerId,
      enabled: !worker.enabled,
    });
  }

  workerLabel(worker: VaingloryAnalysisWorkerNodeStatus): string {
    if (!worker.enabled) {
      if (worker.state === 'stopped') {
        return '已暂停 · 离线';
      }
      return worker.activeTaskCount > 0 ? '暂停中（任务收尾）' : '已暂停';
    }
    if (worker.state === 'failed') {
      return '分析服务异常';
    }
    if (worker.state === 'stopped') {
      return '分析服务未运行';
    }
    if (worker.activeTaskCount > 0) {
      return '正在运行';
    }
    return '空闲';
  }

  workerNodes(
    queue: VaingloryAnalysisQueue,
  ): readonly VaingloryAnalysisWorkerNodeStatus[] {
    const workers = queue.workers ?? [];
    if (workers.length > 0) {
      return workers;
    }
    if (!queue.worker.workerId) {
      return [];
    }
    return [
      {
        state: queue.worker.state,
        workerId: queue.worker.workerId,
        displayName: '',
        enabled: true,
        modelPackageId: queue.worker.modelPackageId,
        pipelineVersion: queue.worker.pipelineVersion,
        lastSeenAt: queue.worker.lastSeenAt,
        activeTaskCount: queue.active.length,
        activePartIds: queue.active.map((item) => item.partId),
        concurrency: 0,
        completedTaskCount: 0,
        failedTaskCount: 0,
        totalProcessingSeconds: 0,
        profiledTaskCount: 0,
        profiledVideoSeconds: 0,
        totalDecodeAnalysisSeconds: 0,
        totalProfiledTaskSeconds: 0,
        lastTaskFinishedAt: null,
      },
    ];
  }

  workerTaskLabel(
    queue: VaingloryAnalysisQueue,
    worker: VaingloryAnalysisWorkerNodeStatus,
  ): string {
    const active = this.workerActiveItems(queue, worker);
    const first = active[0];
    if (first === undefined) {
      if (!worker.enabled) {
        return '已停止领取新任务';
      }
      if (worker.activeTaskCount > 0) {
        return `${worker.activeTaskCount} 个后台任务正在运行`;
      }
      return worker.state === 'running' ? '空闲，正在轮询新任务' : '无任务运行';
    }
    const prefix = active.length > 1 ? `${active.length} 个任务 · ` : '';
    return `${prefix}${this.stageLabel(first)} · ${this.percent(first)}%`;
  }

  workerAverageSeconds(worker: VaingloryAnalysisWorkerNodeStatus): number {
    const taskCount = worker.completedTaskCount + worker.failedTaskCount;
    return taskCount === 0 ? 0 : worker.totalProcessingSeconds / taskCount;
  }

  workerMinutesPerVideoHour(
    worker: VaingloryAnalysisWorkerNodeStatus,
    dimension: 'decodeAnalysis' | 'wholeTask',
  ): number | null {
    if (worker.profiledTaskCount === 0 || worker.profiledVideoSeconds <= 0) {
      return null;
    }
    const seconds =
      dimension === 'decodeAnalysis'
        ? worker.totalDecodeAnalysisSeconds
        : worker.totalProfiledTaskSeconds;
    return (seconds / worker.profiledVideoSeconds) * 60;
  }

  workerEfficiencyLabel(
    worker: VaingloryAnalysisWorkerNodeStatus,
    dimension: 'decodeAnalysis' | 'wholeTask',
  ): string {
    const minutes = this.workerMinutesPerVideoHour(worker, dimension);
    return minutes === null
      ? '暂无完整视频样本'
      : `${minutes.toFixed(1)} 分钟 / 1 小时视频`;
  }

  workerEfficiencySampleLabel(
    worker: VaingloryAnalysisWorkerNodeStatus,
  ): string {
    if (worker.profiledTaskCount === 0 || worker.profiledVideoSeconds <= 0) {
      return '等待 Worker 完成完整视频任务';
    }
    return `${worker.profiledTaskCount} 个任务 · ${(
      worker.profiledVideoSeconds / 3_600
    ).toFixed(1)} 小时视频`;
  }

  workerModelMismatch(
    queue: VaingloryAnalysisQueue,
    worker: VaingloryAnalysisWorkerNodeStatus,
  ): boolean {
    const loaded = worker.modelPackageId;
    return Boolean(
      loaded &&
      this.workerActiveItems(queue, worker).some(
        (item) => item.modelPackageId && item.modelPackageId !== loaded,
      ),
    );
  }

  workerModelState(
    queue: VaingloryAnalysisQueue,
    worker: VaingloryAnalysisWorkerNodeStatus,
  ): string {
    if (!worker.modelPackageId) {
      return worker.state === 'running' ? '等待版本上报' : '暂无版本记录';
    }
    if (this.workerModelMismatch(queue, worker)) {
      return '任务版本与当前版本不一致';
    }
    return worker.state === 'running' ? '已加载并通过启动校验' : '最后上报版本';
  }

  private workerActiveItems(
    queue: VaingloryAnalysisQueue,
    worker: VaingloryAnalysisWorkerNodeStatus,
  ): readonly VaingloryAnalysisQueueItem[] {
    const assigned = queue.active.filter(
      (item) => item.workerId === worker.workerId,
    );
    if (assigned.length > 0 || (queue.workers ?? []).length > 0) {
      return assigned;
    }
    return queue.active.filter((item) =>
      worker.activePartIds.includes(item.partId),
    );
  }

  categoryLabel(category: VaingloryAnalysisQueueCategory): string {
    return {
      manual: '手动任务',
      realtime: '当天直播',
      archive: '历史稿件接入',
      migration: '稿件迁移',
      backlog: '旧数据重扫',
    }[category];
  }

  stageLabel(item: VaingloryAnalysisQueueItem): string {
    if (item.state === 'pending') {
      return '等待扫描视频';
    }
    const runtimeLabels = {
      probing: '读取视频信息',
      coarse_scan: '5 秒分类粗扫',
      fine_scan: '4 FPS 结算精扫',
      timeline_scan: '新模型时间线扫描',
      timeline_analysis: '分析对局时间线',
      result_scan: '4 FPS 结算精扫',
      candidate_upload: '整理训练素材',
      ocr_waiting: '等待 OCR',
      ocr_recognition: 'OCR、英雄与主播识别',
      '': '',
    } as const;
    if (item.runtimeStage) {
      return runtimeLabels[item.runtimeStage];
    }
    return item.stage === 'ocr_waiting'
      ? '已定位结算画面，等待 OCR'
      : item.stage === 'ocr_recognition'
        ? 'OCR、英雄与主播识别'
        : '扫描视频';
  }

  percent(item: VaingloryAnalysisQueueItem): number {
    return Math.max(0, Math.min(100, Math.round(item.progress * 100)));
  }

  elapsedSeconds(item: VaingloryAnalysisQueueItem): number | null {
    if (item.startedAt === null || this.sampledAt === null) {
      return null;
    }
    return Math.max(0, this.sampledAt - item.startedAt);
  }

  currentLabel(item: VaingloryAnalysisQueueItem): string {
    return item.runtimeDetail || this.stageLabel(item);
  }

  timelineCountText(
    summary: VaingloryAnalysisSummary,
    group: TimelineCountGroup,
  ): string {
    return this.countText(
      summary.timelineCounts[group] ?? {},
      ANALYSIS_OUTPUT_LABELS,
    );
  }

  trainingCandidateText(summary: VaingloryAnalysisSummary): string {
    return this.countText(
      summary.trainingCandidateCounts,
      TRAINING_TASK_LABELS,
    );
  }

  trainingCandidateCount(summary: VaingloryAnalysisSummary): number {
    return Object.values(summary.trainingCandidateCounts).reduce(
      (total, count) => total + count,
      0,
    );
  }

  recentEvents(
    item: VaingloryAnalysisQueueItem,
  ): readonly VaingloryAnalysisQueueEvent[] {
    return item.events.slice(-6);
  }

  biliUrl(
    item: VaingloryAnalysisQueueItem | VaingloryAnalysisQueueCompletion,
  ): string | null {
    if (!item.bvid) {
      return null;
    }
    const page = item.archivePage ?? item.partIndex;
    return `https://www.bilibili.com/video/${item.bvid}?p=${Math.max(1, page)}`;
  }

  requestPlayback(
    item: VaingloryAnalysisQueueItem | VaingloryAnalysisQueueCompletion,
  ): void {
    this.playPart.emit({ sessionId: item.sessionId, partId: item.partId });
  }

  requestDetails(sessionId: number): void {
    this.viewSession.emit(sessionId);
  }

  requestImageBrowser(
    item: VaingloryAnalysisQueueItem | VaingloryAnalysisQueueCompletion,
    scope: 'session' | 'part',
  ): void {
    this.browseMatches.emit({
      sessionId: item.sessionId,
      ...(scope === 'part' ? { partId: item.partId } : {}),
      title:
        scope === 'part'
          ? `${item.title || '未命名直播'} · P${item.partIndex} 对局截图`
          : `${item.title || '未命名直播'} · 已识别对局`,
    });
  }

  previewAlt(preview: VaingloryAnalysisMatchPreview, index: number): string {
    return `${preview.title || `第 ${index + 1} 局`}，P${preview.partIndex}，结算画面`;
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) {
      return '待获取';
    }
    const wholeSeconds = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(wholeSeconds / 3_600);
    const minutes = Math.floor((wholeSeconds % 3_600) / 60);
    const remainingSeconds = wholeSeconds % 60;
    if (hours > 0) {
      return `${hours}小时${minutes}分`;
    }
    if (minutes > 0) {
      return `${minutes}分${remainingSeconds}秒`;
    }
    return `${remainingSeconds}秒`;
  }

  private countText(
    counts: Readonly<Record<string, number>>,
    labels: Readonly<Record<string, string>>,
  ): string {
    const entries = Object.entries(counts).sort(
      ([leftLabel, leftCount], [rightLabel, rightCount]) =>
        rightCount - leftCount || leftLabel.localeCompare(rightLabel),
    );
    if (entries.length === 0) {
      return '无';
    }
    return entries
      .map(([label, count]) => `${labels[label] ?? label} ${count}`)
      .join(' · ');
  }
}
