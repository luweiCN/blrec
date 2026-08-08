import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
} from '@angular/core';
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzMessageService } from 'ng-zorro-antd/message';

import { forkJoin, Subject, zip } from 'rxjs';
import { finalize, takeUntil } from 'rxjs/operators';

import { retry } from '../../shared/rx-operators';
import {
  GlobalTaskSettings,
  TaskOptions,
  TaskOptionsIn,
} from '../../settings/shared/setting.model';
import { SettingService } from '../../settings/shared/services/setting.service';
import { TaskManagerService } from '../shared/services/task-manager.service';
import {
  AutomaticSubmissionFilter,
  RunningStatus,
  SubmissionVisibilityFilter,
  TaskBatchAction,
  TaskData,
} from '../shared/task.model';
import { RoomUploadPolicy } from '../upload-policy-dialog/room-upload-policy.model';
import { RoomUploadPolicyService } from '../upload-policy-dialog/room-upload-policy.service';

@Component({
  selector: 'app-task-list',
  templateUrl: './task-list.component.html',
  styleUrls: ['./task-list.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaskListComponent implements OnChanges, OnDestroy {
  @Input() dataList: TaskData[] = [];
  @Input() automaticSubmissionFilter: AutomaticSubmissionFilter = null;
  @Input() submissionVisibilityFilter: SubmissionVisibilityFilter = null;
  @Input() submissionAccountFilter: number | null = null;
  @Input() roomUploadPolicies: readonly RoomUploadPolicy[] = [];
  @Output() roomUploadPoliciesRefresh = new EventEmitter<void>();
  readonly selectedRoomIds = new Set<number>();
  batchLoading = false;
  batchSettingsLoading = false;
  batchSettingsDialogVisible = false;
  batchUploadPolicyDialogVisible = false;
  batchTaskOptions?: TaskOptions;
  batchGlobalSettings?: GlobalTaskSettings;
  policiesByRoomId = new Map<number, RoomUploadPolicy>();
  collectionLabelsByKey = new Map<string, string>();
  private readonly destroy$ = new Subject<void>();

  constructor(
    private changeDetector: ChangeDetectorRef,
    private message: NzMessageService,
    private modal: NzModalService,
    private settingService: SettingService,
    private taskManager: TaskManagerService,
    private policyService: RoomUploadPolicyService,
  ) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (changes.roomUploadPolicies) {
      this.policiesByRoomId = new Map(
        this.roomUploadPolicies.map((policy) => [policy.roomId, policy]),
      );
      this.collectionLabelsByKey.clear();
      this.refreshCollectionLabels(this.roomUploadPolicies);
    }
    const visibleRooms = new Set(
      this.visibleDataList.map((data) => data.room_info.room_id),
    );
    for (const roomId of this.selectedRoomIds) {
      if (!visibleRooms.has(roomId)) {
        this.selectedRoomIds.delete(roomId);
      }
    }
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get selectedCount(): number {
    return this.selectedRoomIds.size;
  }

  get visibleDataList(): TaskData[] {
    return this.dataList.filter((data) => {
      const policy = this.policyFor(data.room_info.room_id);
      if (
        this.automaticSubmissionFilter === 'unconfigured' &&
        policy !== null
      ) {
        return false;
      }
      if (
        this.automaticSubmissionFilter === 'enabled' &&
        policy?.enabled !== true
      ) {
        return false;
      }
      if (
        this.automaticSubmissionFilter === 'disabled' &&
        (policy === null || policy.enabled)
      ) {
        return false;
      }
      if (
        this.submissionVisibilityFilter === 'private' &&
        policy?.isOnlySelf !== true
      ) {
        return false;
      }
      if (
        this.submissionVisibilityFilter === 'public' &&
        (policy === null || policy.isOnlySelf)
      ) {
        return false;
      }
      if (
        this.submissionAccountFilter !== null &&
        policy?.resolvedAccountId !== this.submissionAccountFilter
      ) {
        return false;
      }
      return true;
    });
  }

  get selectedRoomIdsArray(): number[] {
    return this.visibleDataList
      .map((data) => data.room_info.room_id)
      .filter((roomId) => this.selectedRoomIds.has(roomId));
  }

  get selectedReferenceTask(): TaskData | null {
    return (
      this.visibleDataList.find((data) =>
        this.selectedRoomIds.has(data.room_info.room_id),
      ) ?? null
    );
  }

  get allSelected(): boolean {
    return (
      this.visibleDataList.length > 0 &&
      this.visibleDataList.every((data) =>
        this.selectedRoomIds.has(data.room_info.room_id),
      )
    );
  }

  get someSelected(): boolean {
    return this.selectedCount > 0 && !this.allSelected;
  }

  setTaskSelected(roomId: number, selected: boolean): void {
    if (selected) {
      this.selectedRoomIds.add(roomId);
    } else {
      this.selectedRoomIds.delete(roomId);
    }
    this.changeDetector.markForCheck();
  }

  setAllSelected(selected: boolean): void {
    for (const data of this.visibleDataList) {
      this.setTaskSelected(data.room_info.room_id, selected);
    }
  }

  isSelected(roomId: number): boolean {
    return this.selectedRoomIds.has(roomId);
  }

  policyFor(roomId: number): RoomUploadPolicy | null {
    return this.policiesByRoomId.get(roomId) ?? null;
  }

  collectionLabelFor(policy: RoomUploadPolicy | null): string | null {
    if (
      policy?.collectionSeasonId === null ||
      policy?.collectionSeasonId === undefined
    ) {
      return null;
    }
    const accountId = policy.resolvedAccountId;
    if (accountId === null) {
      return `合集 #${policy.collectionSeasonId}`;
    }
    return (
      this.collectionLabelsByKey.get(
        `${accountId}:${policy.collectionSeasonId}`,
      ) ?? `合集 #${policy.collectionSeasonId}`
    );
  }

  requestPolicyRefresh(): void {
    this.roomUploadPoliciesRefresh.emit();
  }

  eligibleCount(action: TaskBatchAction): number {
    return this.eligibleRoomIds(action).length;
  }

  runBatchAction(action: TaskBatchAction): void {
    if (this.batchLoading) {
      return;
    }
    const roomIds = this.eligibleRoomIds(action);
    if (roomIds.length === 0) {
      return;
    }
    if (action === 'delete') {
      this.modal.confirm({
        nzTitle: `确定删除选中的 ${roomIds.length} 个房间？`,
        nzContent:
          '系统会先停止这些房间的监控与录制，再删除房间配置。历史录像、弹幕、片段和 B 站稿件都会保留。',
        nzOkDanger: true,
        nzOnOk: () =>
          new Promise<void>((resolve, reject) => {
            this.executeBatch(action, roomIds, resolve, reject);
          }),
      });
      return;
    }
    if (action === 'recorder_force_disable') {
      this.modal.confirm({
        nzTitle: `强制关闭选中的 ${roomIds.length} 个录制？`,
        nzContent: '正在写入的录像文件会被中断，仅在普通停止无效时使用。',
        nzOkDanger: true,
        nzOnOk: () =>
          new Promise<void>((resolve, reject) => {
            this.executeBatch(action, roomIds, resolve, reject);
          }),
      });
      return;
    }
    this.executeBatch(action, roomIds);
  }

  openBatchSettingsDialog(): void {
    const roomId = this.selectedRoomIdsArray[0];
    if (!roomId || this.batchSettingsLoading) {
      return;
    }
    this.batchSettingsLoading = true;
    zip(
      this.settingService.getTaskOptions(roomId),
      this.settingService.getSettings([
        'output',
        'header',
        'danmaku',
        'recorder',
        'postprocessing',
      ]),
    )
      .pipe(
        finalize(() => {
          this.batchSettingsLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: ([taskOptions, globalSettings]) => {
          this.batchTaskOptions = taskOptions;
          this.batchGlobalSettings = globalSettings;
          this.batchSettingsDialogVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: HttpErrorResponse) => {
          this.message.error(`获取批量录制设置出错：${error.message}`);
        },
      });
  }

  openBatchUploadPolicyDialog(): void {
    if (this.selectedCount === 0) {
      return;
    }
    this.batchUploadPolicyDialogVisible = true;
    this.changeDetector.markForCheck();
  }

  closeBatchUploadPolicyDialog(): void {
    this.batchUploadPolicyDialogVisible = false;
    this.requestPolicyRefresh();
  }

  changeBatchTaskOptions(options: TaskOptionsIn): void {
    const roomIds = this.selectedRoomIdsArray;
    if (roomIds.length === 0 || this.batchSettingsLoading) {
      return;
    }
    this.batchSettingsLoading = true;
    forkJoin(
      roomIds.map((roomId) =>
        this.settingService
          .changeTaskOptions(roomId, options)
          .pipe(retry(3, 300)),
      ),
    )
      .pipe(
        finalize(() => {
          this.batchSettingsLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.message.success(`已修改 ${roomIds.length} 个房间的录制设置`);
        },
        error: (error: HttpErrorResponse) => {
          this.message.error(`修改批量录制设置出错：${error.message}`);
        },
      });
  }

  cleanBatchSettingsData(): void {
    delete this.batchTaskOptions;
    delete this.batchGlobalSettings;
    this.changeDetector.markForCheck();
  }

  trackByRoomId(index: number, data: TaskData): number {
    return data.room_info.room_id;
  }

  private eligibleRoomIds(action: TaskBatchAction): number[] {
    return this.dataList
      .filter((data) => this.selectedRoomIds.has(data.room_info.room_id))
      .filter((data) => {
        const status = data.task_status;
        switch (action) {
          case 'start':
            return status.running_status === RunningStatus.STOPPED;
          case 'stop':
            return status.running_status !== RunningStatus.STOPPED;
          case 'recorder_enable':
            return status.monitor_enabled && !status.recorder_enabled;
          case 'recorder_disable':
          case 'recorder_force_disable':
            return status.recorder_enabled;
          case 'delete':
            return true;
          default:
            return true;
        }
      })
      .map((data) => data.room_info.room_id);
  }

  private executeBatch(
    action: TaskBatchAction,
    roomIds: readonly number[],
    resolve?: () => void,
    reject?: (reason?: unknown) => void,
  ): void {
    this.batchLoading = true;
    this.taskManager
      .runBatchAction(action, roomIds)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.batchLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (response) => {
          for (const result of response.results) {
            if (result.accepted) {
              this.selectedRoomIds.delete(result.roomId);
            }
          }
          resolve?.();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => reject?.(error),
      });
  }

  private refreshCollectionLabels(policies: readonly RoomUploadPolicy[]): void {
    const selections = new Map<
      string,
      Pick<RoomUploadPolicy, 'accountMode' | 'accountId'>
    >();
    for (const policy of policies) {
      if (
        policy.collectionSeasonId === null ||
        policy.resolvedAccountId === null ||
        policy.blockedReason !== null
      ) {
        continue;
      }
      const key = `${policy.accountMode}:${policy.accountId ?? ''}`;
      selections.set(key, policy);
    }
    for (const selection of selections.values()) {
      this.policyService
        .collections(selection.accountMode, selection.accountId)
        .subscribe({
          next: (catalog) => {
            for (const collection of catalog.collections) {
              this.collectionLabelsByKey.set(
                `${catalog.accountId}:${collection.id}`,
                collection.title,
              );
            }
            this.changeDetector.markForCheck();
          },
          error: () => undefined,
        });
    }
  }
}
