import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute, ParamMap, Router } from '@angular/router';

import { Subscription, zip } from 'rxjs';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { retry } from 'src/app/shared/rx-operators';
import { RealtimeService } from '../../core/services/realtime.service';
import { TaskService } from '../shared/services/task.service';
import {
  TaskData,
  DanmakuFileDetail,
  VideoFileDetail,
} from '../shared/task.model';

@Component({
  selector: 'app-task-detail',
  templateUrl: './task-detail.component.html',
  styleUrls: ['./task-detail.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaskDetailComponent implements OnInit, OnDestroy {
  roomId!: number;
  taskData!: TaskData;
  videoFileDetails: VideoFileDetail[] = [];
  danmakuFileDetails: DanmakuFileDetail[] = [];

  loading: boolean = true;
  private dataSubscription?: Subscription;
  private fileSubscription?: Subscription;
  private realtimeSubscription?: Subscription;
  private fileIdentity = '';

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private changeDetector: ChangeDetectorRef,
    private notification: NzNotificationService,
    private taskService: TaskService,
    private realtime: RealtimeService
  ) {}

  ngOnInit(): void {
    this.route.paramMap.subscribe((params: ParamMap) => {
      this.roomId = parseInt(params.get('id')!);
      this.syncData();
    });
    this.realtimeSubscription = this.realtime.events$.subscribe((event) => {
      if (event.type === 'resync') {
        this.syncData();
        return;
      }
      if (event.type !== 'tasks') {
        return;
      }
      const task = this.tasksFromEvent(event.data)?.find(
        (value) => value.room_info.room_id === this.roomId
      );
      if (!task) {
        return;
      }
      this.taskData = task;
      const identity = this.taskFileIdentity(task);
      if (identity !== this.fileIdentity) {
        this.fileIdentity = identity;
        this.syncFileDetails();
      }
      this.loading = false;
      this.changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.desyncData();
  }

  private syncData(): void {
    this.dataSubscription?.unsubscribe();
    this.dataSubscription = zip(
      this.taskService.getTaskData(this.roomId),
      this.taskService.getVideoFileDetails(this.roomId),
      this.taskService.getDanmakuFileDetails(this.roomId)
    )
      .pipe(
        retry(10, 3000)
      )
      .subscribe({
        next: ([taskData, videoFileDetails, danmakuFileDetails]) => {
          this.loading = false;
          this.taskData = taskData;
          this.fileIdentity = this.taskFileIdentity(taskData);
          this.videoFileDetails = videoFileDetails;
          this.danmakuFileDetails = danmakuFileDetails;
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.notification.error(
            '获取任务数据出错',
            '网络连接异常, 请待网络正常后刷新。',
            { nzDuration: 0 }
          );
        },
      });
  }

  private desyncData(): void {
    this.dataSubscription?.unsubscribe();
    this.fileSubscription?.unsubscribe();
    this.realtimeSubscription?.unsubscribe();
  }

  private syncFileDetails(): void {
    this.fileSubscription?.unsubscribe();
    this.fileSubscription = zip(
      this.taskService.getVideoFileDetails(this.roomId),
      this.taskService.getDanmakuFileDetails(this.roomId)
    ).subscribe({
      next: ([videoFileDetails, danmakuFileDetails]) => {
        this.videoFileDetails = videoFileDetails;
        this.danmakuFileDetails = danmakuFileDetails;
        this.changeDetector.markForCheck();
      },
    });
  }

  private tasksFromEvent(data: unknown): TaskData[] | null {
    if (typeof data !== 'object' || data === null || !('tasks' in data)) {
      return null;
    }
    const tasks = (data as { tasks: unknown }).tasks;
    return Array.isArray(tasks) ? (tasks as TaskData[]) : null;
  }

  private taskFileIdentity(data: TaskData): string {
    return [
      data.task_status.running_status,
      data.task_status.recording_path ?? '',
      data.task_status.postprocessing_path ?? '',
    ].join('|');
  }
}
