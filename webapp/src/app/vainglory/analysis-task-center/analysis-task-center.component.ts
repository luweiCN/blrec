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
