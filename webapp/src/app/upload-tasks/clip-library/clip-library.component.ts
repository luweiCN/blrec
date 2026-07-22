import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';

import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { finalize } from 'rxjs/operators';

import { RoomUploadPolicyRequest } from '../../tasks/upload-policy-dialog/room-upload-policy.model';
import { HighlightClipSummary } from '../shared/highlight.model';
import { HighlightService } from '../shared/highlight.service';

type ClipLibraryView =
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly clips: readonly HighlightClipSummary[];
    }
  | { readonly state: 'error'; readonly message: string };

@Component({
  selector: 'app-clip-library',
  templateUrl: './clip-library.component.html',
  styleUrls: ['./clip-library.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ClipLibraryComponent implements OnInit {
  view: ClipLibraryView = { state: 'loading' };
  pageIndex = 1;
  pageSize = 20;
  readonly pageSizeOptions = [20, 50, 100];
  query = '';
  previewClip: HighlightClipSummary | null = null;
  previewUrl: string | null = null;
  previewLoading = false;
  uploadClip: HighlightClipSummary | null = null;
  uploadSubmitting = false;

  constructor(
    private highlights: HighlightService,
    private message: NzMessageService,
    private modal: NzModalService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.load();
  }

  get clips(): readonly HighlightClipSummary[] {
    if (this.view.state !== 'ready') {
      return [];
    }
    const query = this.query.trim().toLowerCase();
    if (!query) {
      return this.view.clips;
    }
    return this.view.clips.filter((clip) =>
      [
        clip.name,
        clip.sourceAnchorName ?? '',
        clip.sourceTitle ?? '',
        String(clip.roomId),
      ].some((value) => value.toLowerCase().includes(query)),
    );
  }

  get total(): number {
    return this.view.state === 'ready' ? this.view.total : 0;
  }

  load(): void {
    this.view = { state: 'loading' };
    this.highlights
      .listAllClips(this.pageSize, (this.pageIndex - 1) * this.pageSize)
      .subscribe({
        next: (response) => {
          this.view = {
            state: 'ready',
            total: response.total,
            clips: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.view = { state: 'error', message: this.errorMessage(error) };
          this.changeDetector.markForCheck();
        },
      });
  }

  pageIndexChanged(pageIndex: number): void {
    this.pageIndex = pageIndex;
    this.load();
  }

  pageSizeChanged(pageSize: number): void {
    this.pageSize = pageSize;
    this.pageIndex = 1;
    this.load();
  }

  openPreview(clip: HighlightClipSummary): void {
    this.previewClip = clip;
    this.previewUrl = null;
    this.previewLoading = true;
    this.highlights
      .createMediaAccess(clip.id)
      .pipe(
        finalize(() => {
          this.previewLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (access) => {
          this.previewUrl = this.highlights.mediaUrl(clip.id, access);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.message.error(`打开片段失败：${this.errorMessage(error)}`);
          this.closePreview();
        },
      });
  }

  closePreview(): void {
    this.previewClip = null;
    this.previewUrl = null;
    this.previewLoading = false;
    this.changeDetector.markForCheck();
  }

  download(clip: HighlightClipSummary): void {
    this.highlights.createMediaAccess(clip.id).subscribe({
      next: (access) => {
        const anchor = document.createElement('a');
        anchor.href = this.highlights.downloadUrl(clip.id, access);
        anchor.download = clip.name;
        anchor.rel = 'noopener';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      },
      error: (error: unknown) => {
        this.message.error(`下载片段失败：${this.errorMessage(error)}`);
      },
    });
  }

  openUpload(clip: HighlightClipSummary): void {
    this.uploadClip = clip;
    this.changeDetector.markForCheck();
  }

  closeUpload(): void {
    if (!this.uploadSubmitting) {
      this.uploadClip = null;
      this.changeDetector.markForCheck();
    }
  }

  submitUpload(settings: RoomUploadPolicyRequest): void {
    const clip = this.uploadClip;
    if (clip === null || this.uploadSubmitting) {
      return;
    }
    this.uploadSubmitting = true;
    this.highlights
      .createUploadTask(clip.id, settings)
      .pipe(
        finalize(() => {
          this.uploadSubmitting = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.message.success('片段已加入上传队列');
          this.uploadClip = null;
          this.load();
        },
        error: (error: unknown) => {
          this.message.error(`创建上传任务失败：${this.errorMessage(error)}`);
        },
      });
  }

  retry(clip: HighlightClipSummary): void {
    if (clip.deletionState !== 'none') {
      this.message.error('片段正在删除，不能重试生成');
      return;
    }
    if (clip.sourceSessionId === null) {
      this.message.error('源录像关联已丢失，无法重试，请删除后重新创建片段');
      return;
    }
    this.highlights.retryClip(clip.id).subscribe({
      next: () => {
        this.message.success('片段已重新排队生成');
        this.load();
      },
      error: (error: unknown) => {
        this.message.error(`重试片段失败：${this.errorMessage(error)}`);
      },
    });
  }

  delete(clip: HighlightClipSummary): void {
    this.modal.confirm({
      nzTitle: `删除片段“${clip.name}”？`,
      nzContent: '将删除片段视频、弹幕文件和本地记录，不会删除 B 站稿件。',
      nzOkDanger: true,
      nzOnOk: () =>
        new Promise<void>((resolve, reject) => {
          this.highlights.deleteClip(clip.id).subscribe({
            next: () => {
              this.message.success('已提交删除，正在处理');
              this.load();
              resolve();
            },
            error: (error: unknown) => {
              this.message.error(`删除片段失败：${this.errorMessage(error)}`);
              reject(error);
            },
          });
        }),
    });
  }

  stateLabel(clip: HighlightClipSummary): string {
    if (clip.deletionState === 'failed') {
      return '删除失败';
    }
    if (this.deletionActive(clip)) {
      return '正在删除';
    }
    return {
      queued: '等待生成',
      processing: '正在生成',
      ready: '可用',
      failed: '生成失败',
      cancelled: '已取消',
    }[clip.state];
  }

  stateColor(clip: HighlightClipSummary): string {
    if (clip.deletionState === 'failed') {
      return 'error';
    }
    if (this.deletionActive(clip)) {
      return 'processing';
    }
    return {
      queued: 'default',
      processing: 'processing',
      ready: 'success',
      failed: 'error',
      cancelled: 'default',
    }[clip.state];
  }

  deletionActive(clip: HighlightClipSummary): boolean {
    return (
      clip.deletionState === 'requested' ||
      clip.deletionState === 'quiescing' ||
      clip.deletionState === 'deleting'
    );
  }

  uploadLabel(clip: HighlightClipSummary): string {
    if (clip.uploadJobId === null) {
      return '未创建任务';
    }
    return {
      waiting_artifacts: '等待文件',
      ready: '等待上传',
      uploading: '正在上传',
      submitting: '正在投稿',
      waiting_review: '等待审核',
      approved: '审核通过',
      rejected: '审核未通过',
      paused: '已暂停',
      completed: '已完成',
    }[clip.uploadState ?? ''] ?? '处理中';
  }

  archiveUrl(clip: HighlightClipSummary): string | null {
    return clip.uploadBvid
      ? `https://www.bilibili.com/video/${clip.uploadBvid}`
      : null;
  }

  formatDuration(milliseconds: number): string {
    const seconds = Math.max(0, Math.round(milliseconds / 1_000));
    if (seconds < 60) {
      return `${seconds} 秒`;
    }
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  }

  formatBytes(bytes: number | null): string {
    if (bytes === null) {
      return '大小待索引';
    }
    const size = Math.max(0, bytes);
    if (size < 1_024) {
      return `${size} B`;
    }
    if (size < 1_048_576) {
      return `${(size / 1_024).toFixed(1)} KB`;
    }
    if (size < 1_073_741_824) {
      const megabytes = size / 1_048_576;
      return `${
        Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)
      } MB`;
    }
    return `${(size / 1_073_741_824).toFixed(1)} GB`;
  }

  private errorMessage(error: unknown): string {
    if (error && typeof error === 'object') {
      const candidate = error as { error?: { detail?: unknown }; message?: unknown };
      if (typeof candidate.error?.detail === 'string') {
        return candidate.error.detail;
      }
      if (typeof candidate.message === 'string') {
        return candidate.message;
      }
    }
    return '未知错误';
  }
}
