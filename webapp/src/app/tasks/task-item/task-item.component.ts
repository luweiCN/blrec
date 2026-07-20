import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  HostBinding,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
} from '@angular/core';

import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { Subject, zip } from 'rxjs';
import { finalize, takeUntil } from 'rxjs/operators';

import { retry } from '../../shared/rx-operators';
import { SettingService } from '../../settings/shared/services/setting.service';
import { RunningStatus, TaskData } from '../shared/task.model';
import { TaskManagerService } from '../shared/services/task-manager.service';
import {
  TaskOptions,
  GlobalTaskSettings,
  TaskOptionsIn,
} from '../../settings/shared/setting.model';
import {
  RoomUploadPolicy,
  RoomUploadPolicyRequest,
} from '../upload-policy-dialog/room-upload-policy.model';
import { RoomUploadPolicyService } from '../upload-policy-dialog/room-upload-policy.service';

@Component({
  selector: 'app-task-item',
  templateUrl: './task-item.component.html',
  styleUrls: ['./task-item.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaskItemComponent implements OnChanges, OnDestroy {
  @Input() data!: TaskData;
  @Input() selected = false;
  @Input() uploadPolicy: RoomUploadPolicy | null = null;
  @Input() collectionLabel: string | null = null;
  @Output() selectedChange = new EventEmitter<boolean>();
  @Output() uploadPolicyChanged = new EventEmitter<void>();
  @HostBinding('class.stopped') stopped = false;

  taskOptions?: TaskOptions;
  globalSettings?: GlobalTaskSettings;

  switchPending = false;
  settingsDialogVisible = false;
  uploadPolicyDialogVisible = false;
  automaticSubmissionPending = false;

  readonly RunningStatus = RunningStatus;
  private readonly destroy$ = new Subject<void>();

  constructor(
    private changeDetector: ChangeDetectorRef,
    private message: NzMessageService,
    private modal: NzModalService,
    private settingService: SettingService,
    private taskManager: TaskManagerService,
    private policyService: RoomUploadPolicyService,
  ) {}

  get roomId() {
    return this.data.room_info.room_id;
  }

  get taskEnabled(): boolean {
    return (
      this.data.task_status.monitor_enabled ||
      this.data.task_status.recorder_enabled
    );
  }

  get canForceInterrupt(): boolean {
    return this.data.task_status.running_status === RunningStatus.RECORDING;
  }

  get automaticSubmissionEnabled(): boolean {
    return this.uploadPolicy?.enabled ?? false;
  }

  get titleKeywords(): readonly string[] {
    return this.data.title_keywords ?? [];
  }

  get titleKeywordSummary(): string {
    if (this.titleKeywords.length <= 2) {
      return this.titleKeywords.join('、');
    }
    return `${this.titleKeywords.slice(0, 2).join('、')} 等 ${
      this.titleKeywords.length
    } 项`;
  }

  get creationTypeLabel(): string {
    return this.uploadPolicy?.creationStatementId === -2 ? '转载' : '原创';
  }

  get publishTimingLabel(): string {
    const delay = this.uploadPolicy?.publishDelaySeconds ?? 0;
    if (delay <= 0) {
      return '审核通过后发布';
    }
    const hours = delay / 3_600;
    return `定时发布 · ${
      Number.isInteger(hours) ? hours : hours.toFixed(1)
    } 小时后`;
  }

  get retentionLabel(): string {
    const policy = this.uploadPolicy;
    if (policy === null) {
      return '';
    }
    const suffix =
      policy.retentionDays > 0 ? ` ${policy.retentionDays} 天后` : '立即';
    return {
      never: '永久保留本地录像',
      upload_completed: `上传完成${suffix}删除`,
      submitted: `投稿成功${suffix}删除`,
      approved: `审核通过${suffix}删除`,
      capacity: '容量超限时清理',
    }[policy.retentionMode];
  }

  setSelected(selected: boolean): void {
    this.selectedChange.emit(selected);
  }

  liveStatusLabel(status: number): string {
    return { 0: '未开播', 1: '直播中', 2: '轮播中' }[status] ?? '未知';
  }

  liveStatusColor(status: number): string {
    return { 0: 'default', 1: 'red', 2: 'green' }[status] ?? 'default';
  }

  runningStatusLabel(status: RunningStatus): string {
    return {
      [RunningStatus.STOPPED]: '已停止',
      [RunningStatus.WAITING]: '监控中',
      [RunningStatus.RECORDING]: '录制中',
      [RunningStatus.REMUXING]: '转封装中',
      [RunningStatus.INJECTING]: '写入元数据',
    }[status];
  }

  runningStatusColor(status: RunningStatus): string {
    return {
      [RunningStatus.STOPPED]: 'default',
      [RunningStatus.WAITING]: 'blue',
      [RunningStatus.RECORDING]: 'red',
      [RunningStatus.REMUXING]: 'processing',
      [RunningStatus.INJECTING]: 'processing',
    }[status];
  }

  ngOnChanges(changes: SimpleChanges): void {
    console.debug('[ngOnChanges]', this.roomId, changes);
    this.stopped =
      this.data.task_status.running_status === RunningStatus.STOPPED;
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  updateTaskInfo(): void {
    this.taskManager.updateTaskInfo(this.roomId).subscribe();
  }

  toggleTask(): void {
    if (this.switchPending) {
      return;
    }
    this.switchPending = true;
    const request = this.taskEnabled
      ? this.taskManager.stopTask(this.roomId)
      : this.taskManager.startTask(this.roomId);
    request
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.switchPending = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe();
  }

  removeTask(): void {
    this.modal.confirm({
      nzTitle: `确定删除房间 ${this.roomId} 的录制任务？`,
      nzContent: '任务配置会被删除，已录制文件不会因此删除。',
      nzOkDanger: true,
      nzOnOk: () =>
        new Promise((resolve, reject) => {
          this.taskManager.removeTask(this.roomId).subscribe(resolve, reject);
        }),
    });
  }

  stopTask(force: boolean = false): void {
    if (this.data.task_status.running_status === RunningStatus.STOPPED) {
      this.message.warning('任务处于停止状态，忽略操作。');
      return;
    }

    if (
      force &&
      this.data.task_status.running_status == RunningStatus.RECORDING
    ) {
      this.modal.confirm({
        nzTitle: '确定强制中断当前录制？',
        nzContent:
          '仅在普通停止无效时使用。当前录像文件可能中断，系统之后仍会尝试恢复可用内容。',
        nzOnOk: () =>
          new Promise((resolve, reject) => {
            this.taskManager
              .stopTask(this.roomId, force)
              .subscribe(resolve, reject);
          }),
      });
    } else {
      this.taskManager.stopTask(this.roomId).subscribe();
    }
  }

  openSettingsDialog(): void {
    zip(
      this.settingService.getTaskOptions(this.roomId),
      this.settingService.getSettings([
        'output',
        'header',
        'danmaku',
        'recorder',
        'postprocessing',
      ]),
    ).subscribe(
      ([taskOptions, globalSettings]) => {
        this.taskOptions = taskOptions;
        this.globalSettings = globalSettings;
        this.settingsDialogVisible = true;
        this.changeDetector.markForCheck();
      },
      (error: HttpErrorResponse) => {
        this.message.error(`获取任务设置出错: ${error.message}`);
      },
    );
  }

  openUploadPolicyDialog(): void {
    this.uploadPolicyDialogVisible = true;
    this.changeDetector.markForCheck();
  }

  toggleAutomaticSubmission(): void {
    if (this.automaticSubmissionPending) {
      return;
    }
    if (this.uploadPolicy === null) {
      this.openUploadPolicyDialog();
      return;
    }
    this.automaticSubmissionPending = true;
    const request = this.policyRequest(this.uploadPolicy);
    this.policyService
      .save(this.roomId, {
        ...request,
        enabled: !this.uploadPolicy.enabled,
      })
      .pipe(
        finalize(() => {
          this.automaticSubmissionPending = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (policy) => {
          this.uploadPolicy = policy;
          this.uploadPolicyChanged.emit();
          this.message.success(
            policy.enabled ? '已开启自动投稿' : '已关闭自动投稿',
          );
        },
        error: (error: HttpErrorResponse) => {
          this.message.error(`修改自动投稿失败：${error.message}`);
        },
      });
  }

  closeUploadPolicyDialog(): void {
    this.uploadPolicyDialogVisible = false;
    this.uploadPolicyChanged.emit();
  }

  cleanSettingsData(): void {
    delete this.taskOptions;
    delete this.globalSettings;
    this.changeDetector.markForCheck();
  }

  changeTaskOptions(options: TaskOptionsIn): void {
    this.settingService
      .changeTaskOptions(this.roomId, options)
      .pipe(retry(3, 300))
      .subscribe(
        (options) => {
          this.message.success('修改任务设置成功');
        },
        (error: HttpErrorResponse) => {
          this.message.error(`修改任务设置出错: ${error.message}`);
        },
      );
  }

  private policyRequest(policy: RoomUploadPolicy): RoomUploadPolicyRequest {
    const {
      roomId: _roomId,
      resolvedAccountId: _resolvedAccountId,
      resolvedAccountName: _resolvedAccountName,
      blockedReason: _blockedReason,
      createdAt: _createdAt,
      updatedAt: _updatedAt,
      ...request
    } = policy;
    return request;
  }
}
