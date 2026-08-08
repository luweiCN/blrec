import { HttpEventType, HttpResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { Subject } from 'rxjs';
import { finalize, takeUntil } from 'rxjs/operators';

import { RecordingSubmissionService } from '../tasks/upload-policy-dialog/recording-submission.service';
import type {
  RecordingPart,
  RecordingSessionDetail,
} from '../upload-tasks/shared/recording-session.model';
import { RecordingSessionService } from '../upload-tasks/shared/recording-session.service';
import {
  MediaLibraryItem,
  MediaLibraryKind,
  MediaLibraryPart,
  MediaLibraryState,
} from './media-library.model';
import { MediaLibraryService } from './media-library.service';

type MediaLibraryView =
  | { readonly state: 'loading' }
  | {
      readonly state: 'ready';
      readonly total: number;
      readonly items: readonly MediaLibraryItem[];
    }
  | { readonly state: 'error'; readonly message: string };

type ImportFileState = 'pending' | 'uploading' | 'done' | 'failed';

interface ImportFileDraft {
  readonly file: File;
  progress: number;
  state: ImportFileState;
  error: string | null;
}

@Component({
  selector: 'app-media-library',
  templateUrl: './media-library.component.html',
  styleUrls: ['./media-library.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MediaLibraryComponent implements OnInit, OnDestroy {
  view: MediaLibraryView = { state: 'loading' };
  kind: MediaLibraryKind = 'broadcast';
  pageIndex = 1;
  pageSize = 20;
  readonly pageSizeOptions = [20, 50, 100];
  query = '';

  importVisible = false;
  importKind: MediaLibraryKind = 'broadcast';
  importDisplayName = '';
  importNote = '';
  importTags = '';
  importRoomId: number | null = null;
  importAnchorName = '';
  importFiles: ImportFileDraft[] = [];
  importSubmitting = false;
  importError: string | null = null;
  importItemId: number | null = null;

  editItem: MediaLibraryItem | null = null;
  editDisplayName = '';
  editNote = '';
  editTags = '';
  editSaving = false;
  editError: string | null = null;

  previewSession: RecordingSessionDetail | null = null;
  previewRecordingPart: RecordingPart | null = null;
  previewVisible = false;
  previewOpeningPartId: number | null = null;

  submissionItem: MediaLibraryItem | null = null;
  submissionStarting = false;

  private readonly destroy$ = new Subject<void>();
  private importQueryHandled = false;

  constructor(
    private mediaLibrary: MediaLibraryService,
    private recordingSessions: RecordingSessionService,
    private submissions: RecordingSubmissionService,
    private route: ActivatedRoute,
    private router: Router,
    private message: NzMessageService,
    private modal: NzModalService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.route.queryParamMap
      .pipe(takeUntil(this.destroy$))
      .subscribe((params) => {
        const requestedKind = params.get('kind');
        this.kind = requestedKind === 'clip' ? 'clip' : 'broadcast';
        this.pageIndex = 1;
        this.load();
        if (params.get('import') === '1' && !this.importQueryHandled) {
          this.importQueryHandled = true;
          this.openImport(this.kind);
        }
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get items(): readonly MediaLibraryItem[] {
    return this.view.state === 'ready' ? this.view.items : [];
  }

  get total(): number {
    return this.view.state === 'ready' ? this.view.total : 0;
  }

  get pageTitle(): string {
    return this.kind === 'broadcast' ? '直播收藏' : '片段';
  }

  get pageSubtitle(): string {
    return this.kind === 'broadcast'
      ? '永久保存整场直播，统一管理分 P、剪辑和投稿历史'
      : '统一管理从直播剪出的片段和从系统外导入的独立片段';
  }

  load(): void {
    this.view = { state: 'loading' };
    this.mediaLibrary
      .list(
        this.kind,
        this.pageSize,
        (this.pageIndex - 1) * this.pageSize,
        this.query,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.view = {
            state: 'ready',
            total: response.total,
            items: response.items,
          };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.view = { state: 'error', message: this.errorMessage(error) };
          this.changeDetector.markForCheck();
        },
      });
  }

  selectKind(kind: MediaLibraryKind): void {
    if (kind === this.kind) {
      return;
    }
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { kind, import: null },
      queryParamsHandling: 'merge',
    });
  }

  applySearch(): void {
    this.pageIndex = 1;
    this.load();
  }

  clearSearch(): void {
    this.query = '';
    this.applySearch();
  }

  pageIndexChanged(pageIndex: number): void {
    if (pageIndex === this.pageIndex) {
      return;
    }
    this.pageIndex = pageIndex;
    this.load();
  }

  pageSizeChanged(pageSize: number): void {
    if (pageSize === this.pageSize) {
      return;
    }
    this.pageSize = pageSize;
    this.pageIndex = 1;
    this.load();
  }

  openImport(kind: MediaLibraryKind = this.kind): void {
    this.importKind = kind;
    this.importVisible = true;
    this.importDisplayName = '';
    this.importNote = '';
    this.importTags = '';
    this.importRoomId = null;
    this.importAnchorName = '';
    this.importFiles = [];
    this.importError = null;
    this.importItemId = null;
    this.changeDetector.markForCheck();
  }

  closeImport(): void {
    if (this.importSubmitting) {
      return;
    }
    this.importVisible = false;
    this.changeDetector.markForCheck();
  }

  importKindChanged(kind: MediaLibraryKind): void {
    this.importKind = kind;
    if (kind === 'clip' && this.importFiles.length > 1) {
      this.importFiles = this.importFiles.slice(0, 1);
      this.message.info('外部片段只保留第一个文件');
    }
  }

  filesSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    const selected = Array.from(input.files ?? []);
    input.value = '';
    if (selected.length === 0) {
      return;
    }
    const files = this.importKind === 'clip' ? selected.slice(0, 1) : selected;
    if (this.importKind === 'clip' && selected.length > 1) {
      this.message.info('外部片段一次只能上传一个文件');
    }
    this.importFiles = files.map((file) => ({
      file,
      progress: 0,
      state: 'pending',
      error: null,
    }));
    if (!this.importDisplayName.trim() && files[0]) {
      this.importDisplayName = files[0].name.replace(/\.[^.]+$/, '');
    }
    this.importError = null;
    this.changeDetector.markForCheck();
  }

  moveImportFile(index: number, direction: -1 | 1): void {
    const target = index + direction;
    if (
      this.importSubmitting ||
      target < 0 ||
      target >= this.importFiles.length
    ) {
      return;
    }
    const reordered = [...this.importFiles];
    [reordered[index], reordered[target]] = [
      reordered[target],
      reordered[index],
    ];
    this.importFiles = reordered;
  }

  removeImportFile(index: number): void {
    if (this.importSubmitting) {
      return;
    }
    this.importFiles = this.importFiles.filter(
      (_file, fileIndex) => fileIndex !== index,
    );
  }

  submitImport(): void {
    if (this.importSubmitting) {
      return;
    }
    const validationError = this.importValidationError();
    if (validationError) {
      this.importError = validationError;
      this.changeDetector.markForCheck();
      return;
    }
    this.importSubmitting = true;
    this.importError = null;
    for (const draft of this.importFiles) {
      if (draft.state === 'failed') {
        draft.state = 'pending';
        draft.progress = 0;
        draft.error = null;
      }
    }
    if (this.importItemId !== null) {
      this.uploadNextImportPart(0);
      return;
    }
    this.mediaLibrary
      .createImport({
        kind: this.importKind,
        displayName: this.importDisplayName.trim(),
        note: this.importNote.trim(),
        tags: this.parseTags(this.importTags),
        roomId: this.importRoomId ?? 0,
        anchorName: this.importAnchorName.trim(),
        parts: this.importFiles.map((draft) => ({
          filename: draft.file.name,
          sizeBytes: draft.file.size,
        })),
      })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (item) => {
          this.importItemId = item.id;
          this.uploadNextImportPart(0);
        },
        error: (error: unknown) => this.failImport(error),
      });
  }

  openEdit(item: MediaLibraryItem): void {
    this.editItem = item;
    this.editDisplayName = item.displayName;
    this.editNote = item.note;
    this.editTags = item.tags.join(', ');
    this.editError = null;
    this.changeDetector.markForCheck();
  }

  closeEdit(): void {
    if (!this.editSaving) {
      this.editItem = null;
      this.changeDetector.markForCheck();
    }
  }

  saveEdit(): void {
    const item = this.editItem;
    if (item === null || this.editSaving) {
      return;
    }
    if (!this.editDisplayName.trim()) {
      this.editError = '名称不能为空';
      return;
    }
    this.editSaving = true;
    this.editError = null;
    this.mediaLibrary
      .update(item.id, {
        displayName: this.editDisplayName.trim(),
        note: this.editNote.trim(),
        tags: this.parseTags(this.editTags),
      })
      .pipe(
        finalize(() => {
          this.editSaving = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.message.success('媒体库信息已更新');
          this.editItem = null;
          this.load();
        },
        error: (error: unknown) => {
          this.editError = this.errorMessage(error);
        },
      });
  }

  openPreview(item: MediaLibraryItem, part: MediaLibraryPart): void {
    if (part.recordingPartId === null) {
      this.message.error('该分 P 尚未准备好');
      return;
    }
    const recordingPartId = part.recordingPartId;
    this.previewOpeningPartId = recordingPartId;
    this.previewSession = null;
    this.previewRecordingPart = null;
    this.previewVisible = false;
    this.recordingSessions
      .getSession(item.sessionId)
      .pipe(
        finalize(() => {
          if (this.previewOpeningPartId === recordingPartId) {
            this.previewOpeningPartId = null;
            this.changeDetector.markForCheck();
          }
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (session) => {
          if (this.previewOpeningPartId !== recordingPartId) {
            return;
          }
          const recordingPart =
            session.parts.find(
              (candidate) => candidate.id === recordingPartId,
            ) ?? null;
          if (recordingPart === null) {
            this.message.error('该分 P 的本地录像已不存在');
            return;
          }
          this.previewSession = {
            ...session,
            title: item.displayName,
          };
          this.previewRecordingPart = recordingPart;
          this.previewVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (this.previewOpeningPartId !== recordingPartId) {
            return;
          }
          this.message.error(`打开视频失败：${this.errorMessage(error)}`);
        },
      });
  }

  previewVisibilityChanged(visible: boolean): void {
    this.previewVisible = visible;
    if (!visible) {
      this.previewSession = null;
      this.previewRecordingPart = null;
    }
    this.changeDetector.markForCheck();
  }

  download(item: MediaLibraryItem, part: MediaLibraryPart): void {
    if (part.recordingPartId === null) {
      return;
    }
    this.recordingSessions.createMediaAccess(part.recordingPartId).subscribe({
      next: (access) => {
        const anchor = document.createElement('a');
        anchor.href = this.recordingSessions.mediaUrl(
          part.recordingPartId!,
          access,
        );
        anchor.download =
          part.originalFilename || `${item.displayName}-P${part.partIndex}`;
        anchor.rel = 'noopener';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      },
      error: (error: unknown) => {
        this.message.error(`下载视频失败：${this.errorMessage(error)}`);
      },
    });
  }

  openSubmission(item: MediaLibraryItem): void {
    this.submissionItem = item;
    this.changeDetector.markForCheck();
  }

  closeSubmission(): void {
    if (!this.submissionStarting) {
      this.submissionItem = null;
      this.changeDetector.markForCheck();
    }
  }

  submissionSettingsSaved(): void {
    const item = this.submissionItem;
    if (item === null || this.submissionStarting) {
      return;
    }
    this.submissionStarting = true;
    this.submissions
      .setDecision(item.sessionId, 'upload')
      .pipe(
        finalize(() => {
          this.submissionStarting = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.message.success('已保存设置并加入上传队列');
          this.submissionItem = null;
          this.load();
        },
        error: (error: unknown) => {
          this.message.error(`启动上传失败：${this.errorMessage(error)}`);
        },
      });
  }

  repost(item: MediaLibraryItem): void {
    this.modal.confirm({
      nzTitle: `重新投稿“${item.displayName}”？`,
      nzContent: '当前 aid/bvid 会保留在投稿历史中，并以新稿件重新上传。',
      nzOnOk: () =>
        new Promise<void>((resolve, reject) => {
          this.recordingSessions
            .runSessionAction('repost_as_new', [item.sessionId])
            .subscribe({
              next: (response) => {
                const result = response.results[0];
                if (!result?.accepted) {
                  const error = new Error(
                    result?.message || '重新投稿未被接受',
                  );
                  this.message.error(error.message);
                  reject(error);
                  return;
                }
                this.message.success(result.message);
                this.load();
                resolve();
              },
              error: (error: unknown) => {
                this.message.error(`重新投稿失败：${this.errorMessage(error)}`);
                reject(error);
              },
            });
        }),
    });
  }

  deleteItem(item: MediaLibraryItem): void {
    this.modal.confirm({
      nzTitle: `删除“${item.displayName}”？`,
      nzContent:
        '将删除媒体库中的视频、弹幕和本地记录，不会删除 B 站已投稿件。',
      nzOkDanger: true,
      nzOnOk: () =>
        new Promise<void>((resolve, reject) => {
          this.mediaLibrary.delete(item.id).subscribe({
            next: () => {
              this.message.success('已提交删除，正在处理');
              this.load();
              resolve();
            },
            error: (error: unknown) => {
              this.message.error(`删除失败：${this.errorMessage(error)}`);
              reject(error);
            },
          });
        }),
    });
  }

  trackItem(_index: number, item: MediaLibraryItem): number {
    return item.id;
  }

  trackPart(_index: number, part: MediaLibraryPart): number {
    return part.partIndex;
  }

  stateLabel(state: MediaLibraryState): string {
    return {
      uploading: '等待上传',
      moving: '正在收藏',
      ready: '永久保存',
      failed: '处理失败',
    }[state];
  }

  stateColor(state: MediaLibraryState): string {
    return {
      uploading: 'processing',
      moving: 'processing',
      ready: 'success',
      failed: 'error',
    }[state];
  }

  originLabel(item: MediaLibraryItem): string {
    return item.origin === 'recording' ? '系统收藏' : '外部导入';
  }

  sourceLabel(item: MediaLibraryItem): string {
    if (item.roomId <= 0) {
      return item.anchorName || '外部来源';
    }
    return `${item.anchorName || '未知主播'} · 房间 ${item.roomId}`;
  }

  submissionUrl(bvid: string): string {
    return `https://www.bilibili.com/video/${encodeURIComponent(bvid)}`;
  }

  formatBytes(bytes: number): string {
    const size = Math.max(0, bytes);
    if (size < 1_024) {
      return `${size} B`;
    }
    if (size < 1_048_576) {
      return `${(size / 1_024).toFixed(1)} KB`;
    }
    if (size < 1_073_741_824) {
      return `${(size / 1_048_576).toFixed(1)} MB`;
    }
    return `${(size / 1_073_741_824).toFixed(1)} GB`;
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) {
      return '时长待探测';
    }
    const hours = Math.floor(seconds / 3_600);
    const minutes = Math.floor((seconds % 3_600) / 60);
    const rest = seconds % 60;
    return [
      hours > 0 ? `${hours} 小时` : '',
      minutes > 0 ? `${minutes} 分` : '',
      rest > 0 || (hours === 0 && minutes === 0) ? `${rest} 秒` : '',
    ]
      .filter(Boolean)
      .join(' ');
  }

  importFileStateLabel(draft: ImportFileDraft): string {
    return {
      pending: '等待上传',
      uploading: '正在上传',
      done: '上传完成',
      failed: '上传失败',
    }[draft.state];
  }

  private uploadNextImportPart(startIndex: number): void {
    const itemId = this.importItemId;
    if (itemId === null) {
      this.failImport(new Error('导入草稿不存在'));
      return;
    }
    const index = this.importFiles.findIndex(
      (draft, fileIndex) => fileIndex >= startIndex && draft.state !== 'done',
    );
    if (index < 0) {
      this.completeImport(itemId);
      return;
    }
    const draft = this.importFiles[index];
    draft.state = 'uploading';
    draft.error = null;
    this.changeDetector.markForCheck();
    this.mediaLibrary
      .uploadPart(itemId, index + 1, draft.file)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (event) => {
          if (event.type === HttpEventType.UploadProgress) {
            draft.progress = Math.round(
              (100 * event.loaded) / (event.total ?? draft.file.size),
            );
          } else if (event instanceof HttpResponse) {
            draft.progress = 100;
            draft.state = 'done';
            this.uploadNextImportPart(index + 1);
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          draft.state = 'failed';
          draft.error = this.errorMessage(error);
          this.failImport(error);
        },
      });
  }

  private completeImport(itemId: number): void {
    this.mediaLibrary
      .completeImport(itemId)
      .pipe(
        finalize(() => {
          this.importSubmitting = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (item) => {
          this.message.success(
            item.kind === 'broadcast'
              ? '外部直播已永久保存'
              : '外部片段已永久保存',
          );
          this.importVisible = false;
          if (this.kind === item.kind) {
            this.load();
          } else {
            this.selectKind(item.kind);
          }
        },
        error: (error: unknown) => {
          this.markRejectedImportPart(error);
          this.failImport(error);
        },
      });
  }

  private failImport(error: unknown): void {
    this.importSubmitting = false;
    this.importError = this.errorMessage(error);
    this.changeDetector.markForCheck();
  }

  private markRejectedImportPart(error: unknown): void {
    if (!error || typeof error !== 'object') {
      return;
    }
    const candidate = error as {
      status?: unknown;
      error?: { detail?: unknown };
    };
    if (
      candidate.status !== 409 ||
      typeof candidate.error?.detail !== 'string'
    ) {
      return;
    }
    const match = /^第 (\d+) 个分 P .*请重新上传/.exec(candidate.error.detail);
    const index = match ? Number(match[1]) - 1 : -1;
    const draft = this.importFiles[index];
    if (!draft) {
      return;
    }
    draft.state = 'failed';
    draft.progress = 0;
    draft.error = candidate.error.detail;
  }

  private importValidationError(): string | null {
    if (!this.importDisplayName.trim()) {
      return '请填写展示名称';
    }
    if (this.importFiles.length === 0) {
      return '请选择至少一个视频文件';
    }
    if (this.importKind === 'clip' && this.importFiles.length !== 1) {
      return '外部片段只能包含一个视频文件';
    }
    if (this.importFiles.some((draft) => draft.file.size <= 0)) {
      return '不能上传空文件';
    }
    return null;
  }

  private parseTags(value: string): readonly string[] {
    return [
      ...new Set(
        value
          .split(/[,，]/)
          .map((tag) => tag.trim())
          .filter(Boolean),
      ),
    ];
  }

  private errorMessage(error: unknown): string {
    if (error && typeof error === 'object') {
      const candidate = error as {
        error?: { detail?: unknown };
        message?: unknown;
      };
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
