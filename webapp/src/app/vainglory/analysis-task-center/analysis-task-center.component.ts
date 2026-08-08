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
  VaingloryAnalysisMatchPreview,
} from '../vainglory.model';

@Component({
  selector: 'app-vainglory-analysis-task-center',
  templateUrl: './analysis-task-center.component.html',
  styleUrls: ['./analysis-task-center.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AnalysisTaskCenterComponent {
  @Input() queue: VaingloryAnalysisQueue | null = null;
  @Input() sampledAt: number | null = null;
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

  workerLabel(queue: VaingloryAnalysisQueue): string {
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
}
