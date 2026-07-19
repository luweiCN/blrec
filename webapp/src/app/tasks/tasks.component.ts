import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';

import { NzNotificationService } from 'ng-zorro-antd/notification';
import { Subscription } from 'rxjs';

import { retry } from 'src/app/shared/rx-operators';
import { StorageService } from '../core/services/storage.service';
import { RealtimeService } from '../core/services/realtime.service';
import { TaskService } from './shared/services/task.service';
import {
  AutomaticSubmissionFilter,
  DataSelection,
  RunningStatus,
  SubmissionVisibilityFilter,
  TaskData,
} from './shared/task.model';
import { RoomUploadPolicy } from './upload-policy-dialog/room-upload-policy.model';
import { RoomUploadPolicyService } from './upload-policy-dialog/room-upload-policy.service';

const SELECTION_STORAGE_KEY = 'app-tasks-selection';
const REVERSE_STORAGE_KEY = 'app-tasks-reverse';

@Component({
  selector: 'app-tasks',
  templateUrl: './tasks.component.html',
  styleUrls: ['./tasks.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TasksComponent implements OnInit, OnDestroy {
  loading: boolean = true;
  dataList: TaskData[] = [];
  selection: DataSelection;
  reverse: boolean;
  filterTerm = '';
  automaticSubmissionFilter: AutomaticSubmissionFilter = null;
  submissionVisibilityFilter: SubmissionVisibilityFilter = null;
  submissionAccountFilter: number | null = null;
  roomUploadPolicies: readonly RoomUploadPolicy[] = [];
  submissionAccountOptions: readonly {
    label: string;
    value: number;
  }[] = [];

  private dataSubscription?: Subscription;
  private policySubscription?: Subscription;
  private realtimeSubscription?: Subscription;
  private allDataList: TaskData[] = [];

  constructor(
    private changeDetector: ChangeDetectorRef,
    private notification: NzNotificationService,
    private storage: StorageService,
    private taskService: TaskService,
    private realtime: RealtimeService,
    private roomUploadPolicyService: RoomUploadPolicyService,
  ) {
    this.selection = this.retrieveSelection();
    this.reverse = this.retrieveReverse();
  }

  ngOnInit(): void {
    this.syncTaskData();
    this.syncRoomUploadPolicies();
    this.realtimeSubscription = this.realtime.events$.subscribe((event) => {
      if (event.type === 'resync') {
        this.syncTaskData();
        return;
      }
      if (event.type !== 'tasks') {
        return;
      }
      const tasks = this.tasksFromEvent(event.data);
      if (tasks !== null) {
        this.applyTaskData(tasks);
      }
    });
  }

  ngOnDestroy(): void {
    this.desyncTaskData();
    this.policySubscription?.unsubscribe();
  }

  refreshRoomUploadPolicies(): void {
    this.syncRoomUploadPolicies();
  }

  onSelectionChanged(selection: DataSelection): void {
    this.selection = selection;
    this.storeSelection(selection);
    this.applyTaskData(this.allDataList);
  }

  onReverseChanged(reverse: boolean): void {
    this.reverse = reverse;
    this.storeReverse(reverse);
    this.applyTaskData(this.allDataList);
  }

  private retrieveSelection(): DataSelection {
    const selection = this.storage.getData(
      SELECTION_STORAGE_KEY,
    ) as DataSelection | null;
    return selection !== null ? selection : DataSelection.ALL;
  }

  private retrieveReverse(): boolean {
    return this.storage.getData(REVERSE_STORAGE_KEY) === 'true';
  }

  private storeSelection(value: DataSelection): void {
    this.storage.setData(SELECTION_STORAGE_KEY, value);
  }

  private storeReverse(value: boolean): void {
    this.storage.setData(REVERSE_STORAGE_KEY, value.toString());
  }

  private syncTaskData(): void {
    this.dataSubscription?.unsubscribe();
    this.dataSubscription = this.taskService
      .getAllTaskData(DataSelection.ALL)
      .pipe(retry(10, 3000))
      .subscribe({
        next: (dataList) => {
          this.loading = false;
          this.applyTaskData(dataList);
        },
        error: () => {
          this.notification.error(
            '获取任务数据出错',
            '网络连接异常, 请待网络正常后刷新。',
            { nzDuration: 0 },
          );
        },
      });
  }

  private syncRoomUploadPolicies(): void {
    this.policySubscription?.unsubscribe();
    this.policySubscription = this.roomUploadPolicyService.list().subscribe({
      next: (policies) => {
        this.roomUploadPolicies = policies;
        const accounts = new Map<number, string>();
        for (const policy of policies) {
          if (policy.resolvedAccountId === null) {
            continue;
          }
          accounts.set(
            policy.resolvedAccountId,
            policy.resolvedAccountName || `账号 #${policy.resolvedAccountId}`,
          );
        }
        this.submissionAccountOptions = [...accounts.entries()].map(
          ([value, label]) => ({ label, value }),
        );
        if (
          this.submissionAccountFilter !== null &&
          !accounts.has(this.submissionAccountFilter)
        ) {
          this.submissionAccountFilter = null;
        }
        this.changeDetector.markForCheck();
      },
      error: () => {
        this.notification.error(
          '获取投稿设置出错',
          '暂时无法加载投稿筛选，请稍后刷新。',
        );
      },
    });
  }

  private desyncTaskData(): void {
    this.dataSubscription?.unsubscribe();
    this.realtimeSubscription?.unsubscribe();
  }

  private applyTaskData(dataList: TaskData[]): void {
    this.allDataList = [...dataList];
    const filtered = this.allDataList.filter((data) =>
      this.matchesSelection(data),
    );
    this.dataList = this.reverse ? [...filtered].reverse() : filtered;
    this.loading = false;
    this.changeDetector.markForCheck();
  }

  private tasksFromEvent(data: unknown): TaskData[] | null {
    if (typeof data !== 'object' || data === null || !('tasks' in data)) {
      return null;
    }
    const tasks = (data as { tasks: unknown }).tasks;
    return Array.isArray(tasks) ? (tasks as TaskData[]) : null;
  }

  private matchesSelection(data: TaskData): boolean {
    switch (this.selection) {
      case DataSelection.ALL:
        return true;
      case DataSelection.PREPARING:
        return data.room_info.live_status === 0;
      case DataSelection.LIVING:
        return data.room_info.live_status === 1;
      case DataSelection.ROUNDING:
        return data.room_info.live_status === 2;
      case DataSelection.MONITOR_ENABLED:
        return data.task_status.monitor_enabled;
      case DataSelection.MONITOR_DISABLED:
        return !data.task_status.monitor_enabled;
      case DataSelection.RECORDER_ENABLED:
        return data.task_status.recorder_enabled;
      case DataSelection.RECORDER_DISABLED:
        return !data.task_status.recorder_enabled;
      case DataSelection.STOPPED:
        return data.task_status.running_status === RunningStatus.STOPPED;
      case DataSelection.WAITTING:
        return data.task_status.running_status === RunningStatus.WAITING;
      case DataSelection.RECORDING:
        return data.task_status.running_status === RunningStatus.RECORDING;
      case DataSelection.REMUXING:
        return data.task_status.running_status === RunningStatus.REMUXING;
      case DataSelection.INJECTING:
        return data.task_status.running_status === RunningStatus.INJECTING;
      default: {
        const exhaustive: never = this.selection;
        throw new Error(`unknown task selection: ${exhaustive}`);
      }
    }
  }
}
