import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  Input,
  OnDestroy,
  ChangeDetectorRef,
  Output,
  EventEmitter,
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';

import { NzNotificationService } from 'ng-zorro-antd/notification';
import { Subscription, zip } from 'rxjs';
import { retry } from 'src/app/shared/rx-operators';
import { RealtimeService } from '../../core/services/realtime.service';

import { Metadata, RunningStatus, TaskData } from '../shared/task.model';
import { TaskService } from '../shared/services/task.service';
import { StreamProfile } from '../shared/task.model';

@Component({
  selector: 'app-info-panel',
  templateUrl: './info-panel.component.html',
  styleUrls: ['./info-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InfoPanelComponent implements OnInit, OnDestroy {
  @Input() data!: TaskData;
  @Input() profile!: StreamProfile;
  @Input() metadata: Metadata | null = null;
  @Output() close = new EventEmitter<undefined>();

  readonly RunningStatus = RunningStatus;
  private dataSubscription?: Subscription;
  private realtimeSubscription?: Subscription;
  private streamIdentity = '';

  constructor(
    private changeDetector: ChangeDetectorRef,
    private notification: NzNotificationService,
    private taskService: TaskService,
    private realtime: RealtimeService
  ) {}

  get fps(): string {
    const avgFrameRate: string | undefined =
      this.profile?.streams![0]?.avg_frame_rate;
    if (avgFrameRate) {
      return eval(avgFrameRate).toFixed();
    } else {
      return 'N/A';
    }
  }

  ngOnInit(): void {
    this.streamIdentity = this.taskStreamIdentity(this.data);
    this.syncData();
    this.realtimeSubscription = this.realtime.events$.subscribe((event) => {
      if (event.type === 'resync') {
        this.syncData();
        return;
      }
      if (event.type !== 'tasks') {
        return;
      }
      const tasks = this.tasksFromEvent(event.data);
      const task = tasks?.find(
        (value) => value.room_info.room_id === this.data.room_info.room_id
      );
      if (!task) {
        return;
      }
      this.data = task;
      const identity = this.taskStreamIdentity(task);
      if (identity !== this.streamIdentity) {
        this.streamIdentity = identity;
        this.syncData();
      } else {
        this.changeDetector.markForCheck();
      }
    });
  }

  ngOnDestroy(): void {
    this.desyncData();
  }

  isBlurayStreamQuality(): boolean {
    return /_bluray/.test(this.data.task_status.stream_url);
  }

  closePanel(event: Event): void {
    event.preventDefault();
    event.stopPropagation();
    this.close.emit();
  }

  private syncData(): void {
    this.dataSubscription?.unsubscribe();
    this.dataSubscription = zip(
      this.taskService.getStreamProfile(this.data.room_info.room_id),
      this.taskService.getMetadata(this.data.room_info.room_id)
    )
      .pipe(
        retry(3, 1000)
      )
      .subscribe({
        next: ([profile, metadata]) => {
          this.profile = profile;
          this.metadata = metadata;
          this.changeDetector.markForCheck();
        },
        error: (_error: HttpErrorResponse) => {
          this.notification.error(
            '获取数据出错',
            '网络连接异常, 请待网络正常后刷新。',
            { nzDuration: 0 }
          );
        },
      });
  }
  private desyncData(): void {
    this.dataSubscription?.unsubscribe();
    this.realtimeSubscription?.unsubscribe();
  }

  private tasksFromEvent(data: unknown): TaskData[] | null {
    if (typeof data !== 'object' || data === null || !('tasks' in data)) {
      return null;
    }
    const tasks = (data as { tasks: unknown }).tasks;
    return Array.isArray(tasks) ? (tasks as TaskData[]) : null;
  }

  private taskStreamIdentity(data: TaskData): string {
    return [
      data.task_status.running_status,
      data.task_status.stream_url,
      data.task_status.recording_path ?? '',
    ].join('|');
  }
}
