import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  OnInit,
  Output,
} from '@angular/core';

import type { NzCascaderOption } from 'ng-zorro-antd/cascader';
import { NzMessageService } from 'ng-zorro-antd/message';
import { Observable, Subject, forkJoin, of, throwError } from 'rxjs';
import { catchError, finalize, takeUntil } from 'rxjs/operators';

import { BiliAccount } from '../../uploads/shared/bili-account.model';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  RoomUploadPolicy,
  RoomUploadPolicyDraft,
  RoomUploadPolicyRequest,
  UploadAccountMode,
  UploadCategoryCatalog,
  UploadCategoryNode,
} from './room-upload-policy.model';
import { RoomUploadPolicyService } from './room-upload-policy.service';

const DEFAULT_DRAFT: RoomUploadPolicyDraft = {
  accountMode: 'primary',
  accountId: null,
  enabled: true,
  titleTemplate: '{{ title }} 录播',
  descriptionTemplate: '主播：{{ anchor_name }}',
  partTitleTemplate: 'P{{ part_index }}',
  dynamicTemplate: '{{ title }} 录播',
  tid: null,
  tags: '直播,录播',
  copyright: 1,
  source: '',
  isOnlySelf: false,
  publishDynamic: true,
  noReprint: true,
  upSelectionReply: false,
  upCloseReply: false,
  upCloseDanmu: false,
  autoComment: true,
  danmakuBackfill: true,
  filters: {},
};

type PolicyValidationErrors = Partial<
  Record<
    'account' | 'title' | 'partTitle' | 'category' | 'tags' | 'source',
    string
  >
>;

@Component({
  selector: 'app-upload-policy-dialog',
  templateUrl: './upload-policy-dialog.component.html',
  styleUrls: ['./upload-policy-dialog.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UploadPolicyDialogComponent implements OnInit, OnDestroy {
  @Input() roomId!: number;
  @Input() roomName = '';
  @Output() closed = new EventEmitter<void>();

  visible = true;
  loading = true;
  categoryLoading = false;
  saving = false;
  deleting = false;
  existingPolicy = false;
  error: string | null = null;
  categoryError: string | null = null;
  saveAttempted = false;
  accounts: readonly BiliAccount[] = [];
  catalog: UploadCategoryCatalog | null = null;
  categoryPath: number[] = [];
  draft: RoomUploadPolicyDraft = this.newDraft();

  readonly templateTip =
    '可用变量：{{ title }}、{{ anchor_name }}、{{ area_name }}、{{ parent_area_name }}、{{ room_id }}、{{ live_start_time }}、{{ live_end_time }}、{{ part_count }}。分 P 标题还可使用 {{ part_index }}。';
  readonly partTemplateTip =
    '每个录制分段分别渲染。{{ part_index }} 是从 1 开始的分 P 序号，例如 P1、P2。';

  private readonly destroy$ = new Subject<void>();

  constructor(
    private policyService: RoomUploadPolicyService,
    private accountService: BiliAccountService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    forkJoin({
      policy: this.loadPolicy(),
      accounts: this.accountService.listAccounts(),
    })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: ({ policy, accounts }) => {
          this.accounts = accounts;
          this.existingPolicy = policy !== null;
          this.draft = policy ? this.fromPolicy(policy) : this.newDraft();
          this.loading = false;
          this.loadCategories();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loading = false;
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get activeAccounts(): readonly BiliAccount[] {
    return this.accounts.filter((account) => account.state === 'active');
  }

  get categories(): UploadCategoryNode[] {
    return this.catalog?.categories ?? [];
  }

  get categoryOptions(): NzCascaderOption[] {
    return this.categories.map((parent) => ({
      value: parent.id,
      label: parent.name,
      isLeaf: false,
      children: parent.children.map((child) => ({
        value: child.id,
        label: child.name,
        isLeaf: true,
      })),
    }));
  }

  get selectedCategoryDescription(): string {
    const tid = this.draft.tid;
    if (tid === null) {
      return '';
    }
    for (const parent of this.categories) {
      const child = parent.children.find((item) => item.id === tid);
      if (child) {
        return child.description;
      }
    }
    return '';
  }

  get allowReplies(): boolean {
    return !this.draft.upCloseReply;
  }

  get allowDanmaku(): boolean {
    return !this.draft.upCloseDanmu;
  }

  get validationErrors(): PolicyValidationErrors {
    return this.saveAttempted ? this.validateDraft() : {};
  }

  get saveDisabled(): boolean {
    return this.loading || this.saving || this.deleting;
  }

  accountModeChanged(mode: UploadAccountMode): void {
    this.draft.accountMode = mode;
    this.draft.accountId =
      mode === 'primary' ? null : (this.activeAccounts[0]?.id ?? null);
    this.clearCategorySelectionForAccountChange();
    this.loadCategories();
  }

  fixedAccountChanged(accountId: number | null): void {
    this.draft.accountId = accountId;
    this.clearCategorySelectionForAccountChange();
    this.loadCategories();
  }

  categoryChanged(path: number[] | null): void {
    this.categoryPath = path ?? [];
    this.draft.tid =
      this.categoryPath.length === 2 ? this.categoryPath[1] : null;
  }

  allowRepliesChanged(allowed: boolean): void {
    this.draft.upCloseReply = !allowed;
    if (!allowed) {
      this.draft.upSelectionReply = false;
      this.draft.autoComment = false;
    }
  }

  allowDanmakuChanged(allowed: boolean): void {
    this.draft.upCloseDanmu = !allowed;
    if (!allowed) {
      this.draft.danmakuBackfill = false;
    }
  }

  refreshCategories(): void {
    this.loadCategories(true);
  }

  save(): void {
    if (this.loading || this.saving || this.deleting) {
      return;
    }
    this.saveAttempted = true;
    this.error = null;
    const validationErrors = this.validationErrors;
    const tid = this.draft.tid;
    if (Object.keys(validationErrors).length > 0 || tid === null) {
      this.changeDetector.markForCheck();
      return;
    }
    const request: RoomUploadPolicyRequest = {
      accountMode: this.draft.accountMode,
      accountId:
        this.draft.accountMode === 'fixed' ? this.draft.accountId : null,
      enabled: this.draft.enabled,
      titleTemplate: this.draft.titleTemplate.trim(),
      descriptionTemplate: this.draft.descriptionTemplate.trim(),
      partTitleTemplate: this.draft.partTitleTemplate.trim(),
      dynamicTemplate: this.draft.dynamicTemplate.trim(),
      tid,
      tags: this.draft.tags.trim(),
      copyright: this.draft.copyright,
      source: this.draft.source.trim(),
      isOnlySelf: this.draft.isOnlySelf,
      publishDynamic: this.draft.publishDynamic,
      noReprint: this.draft.noReprint,
      upSelectionReply: this.draft.upSelectionReply,
      upCloseReply: this.draft.upCloseReply,
      upCloseDanmu: this.draft.upCloseDanmu,
      autoComment: this.draft.autoComment,
      danmakuBackfill: this.draft.danmakuBackfill,
      filters: { ...this.draft.filters },
    };
    this.saving = true;
    this.error = null;
    this.policyService
      .save(this.roomId, request)
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.existingPolicy = true;
          this.message.success(`房间 ${this.roomId} 的投稿设置已保存`);
          this.visible = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  deletePolicy(): void {
    if (!this.existingPolicy || this.deleting || this.saving) {
      return;
    }
    this.deleting = true;
    this.error = null;
    this.policyService
      .delete(this.roomId)
      .pipe(
        finalize(() => {
          this.deleting = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.message.success(`房间 ${this.roomId} 的投稿设置已删除`);
          this.visible = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  close(): void {
    if (!this.saving && !this.deleting) {
      this.visible = false;
    }
  }

  private loadPolicy(): Observable<RoomUploadPolicy | null> {
    return this.policyService.get(this.roomId).pipe(
      catchError((error: unknown) => {
        if (error instanceof HttpErrorResponse && error.status === 404) {
          return of(null);
        }
        return throwError(() => error);
      }),
    );
  }

  private loadCategories(forceRefresh = false): void {
    if (this.draft.accountMode === 'fixed' && this.draft.accountId === null) {
      this.catalog = null;
      this.categoryError = '请选择一个可用的固定投稿账号。';
      this.loading = false;
      this.changeDetector.markForCheck();
      return;
    }
    this.categoryLoading = true;
    this.categoryError = null;
    this.policyService
      .categories(
        this.draft.accountMode,
        this.draft.accountId,
        forceRefresh,
      )
      .pipe(
        finalize(() => {
          this.categoryLoading = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (catalog) => {
          this.catalog = catalog;
          this.syncCategoryPath();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.catalog = null;
          this.categoryError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private syncCategoryPath(): void {
    const tid = this.draft.tid;
    this.categoryPath = [];
    if (tid === null) {
      return;
    }
    for (const parent of this.categories) {
      if (parent.children.some((child) => child.id === tid)) {
        this.categoryPath = [parent.id, tid];
        return;
      }
    }
  }

  private clearCategorySelectionForAccountChange(): void {
    this.catalog = null;
    this.categoryPath = [];
    this.draft.tid = null;
  }

  private validateDraft(): PolicyValidationErrors {
    const errors: PolicyValidationErrors = {};
    if (this.draft.accountMode === 'fixed' && this.draft.accountId === null) {
      errors.account = '请选择投稿账号';
    }
    if (!this.draft.titleTemplate.trim()) {
      errors.title = '请填写标题模板';
    }
    if (!this.draft.partTitleTemplate.trim()) {
      errors.partTitle = '请填写分 P 标题模板';
    }
    if (
      this.draft.tid === null ||
      this.categoryPath.length !== 2 ||
      this.categoryPath[1] !== this.draft.tid
    ) {
      errors.category = '请选择投稿分区';
    }
    if (!this.draft.tags.trim()) {
      errors.tags = '请填写至少一个标签';
    }
    if (this.draft.copyright === 2 && !this.draft.source.trim()) {
      errors.source = '转载稿件必须填写来源';
    }
    return errors;
  }

  private newDraft(): RoomUploadPolicyDraft {
    return { ...DEFAULT_DRAFT, filters: {} };
  }

  private fromPolicy(policy: RoomUploadPolicy): RoomUploadPolicyDraft {
    return {
      accountMode: policy.accountMode,
      accountId: policy.accountId,
      enabled: policy.enabled,
      titleTemplate: policy.titleTemplate,
      descriptionTemplate: policy.descriptionTemplate,
      partTitleTemplate: policy.partTitleTemplate,
      dynamicTemplate: policy.dynamicTemplate,
      tid: policy.tid,
      tags: policy.tags,
      copyright: policy.copyright,
      source: policy.source,
      isOnlySelf: policy.isOnlySelf,
      publishDynamic: policy.publishDynamic,
      noReprint: policy.noReprint,
      upSelectionReply: policy.upSelectionReply,
      upCloseReply: policy.upCloseReply,
      upCloseDanmu: policy.upCloseDanmu,
      autoComment: policy.autoComment,
      danmakuBackfill: policy.danmakuBackfill,
      filters: { ...policy.filters },
    };
  }

  private errorMessage(error: unknown): string {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail;
      if (typeof detail === 'string' && detail) {
        return detail;
      }
      return error.message || '请求失败，请稍后重试';
    }
    return error instanceof Error ? error.message : '请求失败，请稍后重试';
  }
}
