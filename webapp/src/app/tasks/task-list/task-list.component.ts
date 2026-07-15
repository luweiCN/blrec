import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
} from '@angular/core';
import { NzModalService } from 'ng-zorro-antd/modal';

import { finalize } from 'rxjs/operators';

import { TaskManagerService } from '../shared/services/task-manager.service';
import {
  RunningStatus,
  TaskBatchAction,
  TaskData,
} from '../shared/task.model';

@Component({
  selector: 'app-task-list',
  templateUrl: './task-list.component.html',
  styleUrls: ['./task-list.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaskListComponent implements OnChanges {
  @Input() dataList: TaskData[] = [];
  readonly selectedRoomIds = new Set<number>();
  batchLoading = false;

  constructor(
    private changeDetector: ChangeDetectorRef,
    private modal: NzModalService,
    private taskManager: TaskManagerService
  ) {}

  ngOnChanges(): void {
    const visibleRooms = new Set(
      this.dataList.map((data) => data.room_info.room_id)
    );
    for (const roomId of this.selectedRoomIds) {
      if (!visibleRooms.has(roomId)) {
        this.selectedRoomIds.delete(roomId);
      }
    }
  }

  get selectedCount(): number {
    return this.selectedRoomIds.size;
  }

  get allSelected(): boolean {
    return (
      this.dataList.length > 0 &&
      this.dataList.every((data) =>
        this.selectedRoomIds.has(data.room_info.room_id)
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
    for (const data of this.dataList) {
      this.setTaskSelected(data.room_info.room_id, selected);
    }
  }

  isSelected(roomId: number): boolean {
    return this.selectedRoomIds.has(roomId);
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
        nzTitle: `确定删除选中的 ${roomIds.length} 个录制任务？`,
        nzContent: '正在录制的任务不会被强制删除；请先停止录制。',
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
            return status.recorder_enabled;
          case 'cut':
            return status.running_status === RunningStatus.RECORDING;
          case 'delete':
            return status.running_status === RunningStatus.STOPPED;
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
    reject?: (reason?: unknown) => void
  ): void {
    this.batchLoading = true;
    this.taskManager
      .runBatchAction(action, roomIds)
      .pipe(
        finalize(() => {
          this.batchLoading = false;
          this.changeDetector.markForCheck();
        })
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
}
