import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';

import { Subject, forkJoin } from 'rxjs';
import { finalize, takeUntil } from 'rxjs/operators';

import { TaskData } from '../tasks/shared/task.model';
import { TaskService } from '../tasks/shared/services/task.service';
import { BiliAccount } from '../uploads/shared/bili-account.model';
import { BiliAccountService } from '../uploads/shared/bili-account.service';
import {
  RoomUploadPolicy,
  RoomUploadPolicyDraft,
  RoomUploadPolicyRequest,
  UploadAccountMode,
} from './shared/room-upload-policy.model';
import { RoomUploadPolicyService } from './shared/room-upload-policy.service';

const DEFAULT_DRAFT: RoomUploadPolicyDraft = {
  roomId: null,
  accountMode: 'primary',
  accountId: null,
  enabled: true,
  titleTemplate: '{{ title }} 录播',
  descriptionTemplate: '主播：{{ anchor_name }}',
  tid: 17,
  tags: '直播,录播',
  copyright: 1,
  source: '',
  autoComment: false,
  danmakuBackfill: false,
  filters: {},
};

@Component({
  selector: 'app-upload-policies',
  templateUrl: './upload-policies.component.html',
  styleUrls: ['./upload-policies.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UploadPoliciesComponent implements OnInit, OnDestroy {
  loading = true;
  loadError: string | null = null;
  actionError: string | null = null;
  actionMessage: string | null = null;
  policies: readonly RoomUploadPolicy[] = [];
  accounts: readonly BiliAccount[] = [];
  tasks: readonly TaskData[] = [];
  dialogVisible = false;
  editing = false;
  saving = false;
  deletingRoomId: number | null = null;
  draft: RoomUploadPolicyDraft = { ...DEFAULT_DRAFT };

  readonly primaryAccountTip =
    '跟随主账号：每次创建新上传任务时读取当时的主账号。任务创建后会锁定账号，之后切换主账号不会改绑旧任务。';
  readonly fixedAccountTip =
    '固定账号：这个房间以后创建的新上传任务始终绑定所选账号，除非再次修改规则。';
  readonly templateTip =
    '可用变量：{{ title }}、{{ anchor_name }}、{{ area_name }}、{{ parent_area_name }}、{{ room_id }}、{{ live_start_time }}、{{ live_end_time }}、{{ part_count }}。';

  private readonly destroy$ = new Subject<void>();

  constructor(
    private policyService: RoomUploadPolicyService,
    private accountService: BiliAccountService,
    private taskService: TaskService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.load();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get activeAccounts(): readonly BiliAccount[] {
    return this.accounts.filter((account) => account.state === 'active');
  }

  get availableTasks(): readonly TaskData[] {
    if (this.editing) {
      return this.tasks;
    }
    const configured = new Set(this.policies.map((policy) => policy.roomId));
    return this.tasks.filter((task) => !configured.has(task.room_info.room_id));
  }

  get canSave(): boolean {
    return Boolean(
      !this.saving &&
      this.draft.roomId &&
      this.draft.tid > 0 &&
      this.draft.titleTemplate.trim() &&
      this.draft.tags.trim() &&
      (this.draft.accountMode === 'primary' || this.draft.accountId) &&
      (this.draft.copyright === 1 || this.draft.source.trim()),
    );
  }

  load(): void {
    this.loading = true;
    this.loadError = null;
    forkJoin({
      policies: this.policyService.list(),
      accounts: this.accountService.listAccounts(),
      tasks: this.taskService.getAllTaskData(),
    })
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: ({ policies, accounts, tasks }) => {
          this.policies = [...policies].sort((a, b) => a.roomId - b.roomId);
          this.accounts = accounts;
          this.tasks = tasks;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loadError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  openCreate(): void {
    this.editing = false;
    this.actionError = null;
    const firstTask = this.availableTasks[0];
    this.draft = {
      ...DEFAULT_DRAFT,
      roomId: firstTask?.room_info.room_id ?? null,
      filters: {},
    };
    this.dialogVisible = true;
    this.changeDetector.markForCheck();
  }

  openEdit(policy: RoomUploadPolicy): void {
    this.editing = true;
    this.actionError = null;
    this.draft = {
      roomId: policy.roomId,
      accountMode: policy.accountMode,
      accountId: policy.accountId,
      enabled: policy.enabled,
      titleTemplate: policy.titleTemplate,
      descriptionTemplate: policy.descriptionTemplate,
      tid: policy.tid,
      tags: policy.tags,
      copyright: policy.copyright,
      source: policy.source,
      autoComment: policy.autoComment,
      danmakuBackfill: policy.danmakuBackfill,
      filters: { ...policy.filters },
    };
    this.dialogVisible = true;
    this.changeDetector.markForCheck();
  }

  closeDialog(): void {
    if (this.saving) {
      return;
    }
    this.dialogVisible = false;
    this.actionError = null;
    this.changeDetector.markForCheck();
  }

  accountModeChanged(mode: UploadAccountMode): void {
    this.draft.accountMode = mode;
    this.draft.accountId =
      mode === 'primary' ? null : (this.activeAccounts[0]?.id ?? null);
  }

  save(): void {
    const roomId = this.draft.roomId;
    if (!roomId || !this.canSave) {
      return;
    }
    const request: RoomUploadPolicyRequest = {
      accountMode: this.draft.accountMode,
      accountId:
        this.draft.accountMode === 'fixed' ? this.draft.accountId : null,
      enabled: this.draft.enabled,
      titleTemplate: this.draft.titleTemplate.trim(),
      descriptionTemplate: this.draft.descriptionTemplate.trim(),
      tid: this.draft.tid,
      tags: this.draft.tags.trim(),
      copyright: this.draft.copyright,
      source: this.draft.source.trim(),
      autoComment: this.draft.autoComment,
      danmakuBackfill: this.draft.danmakuBackfill,
      filters: this.draft.filters,
    };
    this.saving = true;
    this.actionError = null;
    this.policyService
      .save(roomId, request)
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (saved) => {
          this.policies = [
            ...this.policies.filter((policy) => policy.roomId !== saved.roomId),
            saved,
          ].sort((a, b) => a.roomId - b.roomId);
          this.dialogVisible = false;
          this.actionMessage = `房间 ${saved.roomId} 的投稿规则已保存`;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  deletePolicy(roomId: number): void {
    if (this.deletingRoomId !== null) {
      return;
    }
    this.deletingRoomId = roomId;
    this.actionError = null;
    this.policyService
      .delete(roomId)
      .pipe(
        finalize(() => {
          this.deletingRoomId = null;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.policies = this.policies.filter(
            (policy) => policy.roomId !== roomId,
          );
          this.actionMessage = `房间 ${roomId} 的投稿规则已删除`;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  roomLabel(roomId: number): string {
    const task = this.tasks.find((item) => item.room_info.room_id === roomId);
    if (!task) {
      return `房间 ${roomId}`;
    }
    return `${task.user_info.name} · ${roomId}`;
  }

  policyAccountLabel(policy: RoomUploadPolicy): string {
    const prefix = policy.accountMode === 'primary' ? '跟随主账号' : '固定账号';
    return policy.resolvedAccountName
      ? `${prefix} · ${policy.resolvedAccountName}`
      : prefix;
  }

  trackPolicy(_index: number, policy: RoomUploadPolicy): number {
    return policy.roomId;
  }

  trackTask(_index: number, task: TaskData): number {
    return task.room_info.room_id;
  }

  trackAccount(_index: number, account: BiliAccount): number {
    return account.id;
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
