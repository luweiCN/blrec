import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { NzNotificationService } from 'ng-zorro-antd/notification';
import { of } from 'rxjs';

import {
  EVENT_SOURCE_FACTORY,
  EventSourceLike,
  RealtimeEvent,
  RealtimeService,
} from '../core/services/realtime.service';
import { StorageService } from '../core/services/storage.service';
import { UrlService } from '../core/services/url.service';
import { FilterTasksPipe } from './shared/pipes/filter-tasks.pipe';
import { TaskService } from './shared/services/task.service';
import {
  DataSelection,
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from './shared/task.model';
import { TasksComponent } from './tasks.component';
import { RoomUploadPolicy } from './upload-policy-dialog/room-upload-policy.model';
import { RoomUploadPolicyService } from './upload-policy-dialog/room-upload-policy.service';

class FakeRealtimeSource implements EventSourceLike {
  private readonly listeners = new Map<string, EventListener[]>();

  addEventListener(type: string, listener: EventListener): void {
    const values = this.listeners.get(type) ?? [];
    values.push(listener);
    this.listeners.set(type, values);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((value) => value !== listener),
    );
  }

  close(): void {}

  next(event: RealtimeEvent): void {
    const message = new MessageEvent(event.type, {
      data: JSON.stringify(event.data),
    });
    for (const listener of this.listeners.get(event.type) ?? []) {
      listener(message);
    }
  }
}

describe('TasksComponent', () => {
  let component: TasksComponent;
  let fixture: ComponentFixture<TasksComponent>;
  let taskService: jasmine.SpyObj<TaskService>;
  let policyService: jasmine.SpyObj<RoomUploadPolicyService>;
  let realtimeEvents: FakeRealtimeSource;

  const taskData: TaskData = {
    user_info: {
      name: '主播',
      gender: '',
      face: '',
      uid: 1,
      level: 0,
      sign: '',
    },
    room_info: {
      uid: 1,
      room_id: 100,
      short_room_id: 0,
      area_id: 1,
      area_name: '',
      parent_area_id: 1,
      parent_area_name: '',
      live_status: 1,
      live_start_time: 1,
      online: 0,
      title: '直播',
      cover: '',
      tags: '',
      description: '',
    },
    task_status: {
      monitor_enabled: true,
      recorder_enabled: true,
      running_status: RunningStatus.WAITING,
      stream_url: '',
      stream_host: '',
      dl_total: 0,
      dl_rate: 0,
      rec_elapsed: 0,
      rec_total: 0,
      rec_rate: 0,
      danmu_total: 0,
      danmu_rate: 0,
      real_stream_format: null,
      real_quality_number: null,
      recording_path: null,
      postprocessor_status: PostprocessorStatus.WAITING,
      postprocessing_path: null,
      postprocessing_progress: null,
    },
  };

  beforeEach(async () => {
    taskService = jasmine.createSpyObj<TaskService>('TaskService', [
      'getAllTaskData',
    ]);
    taskService.getAllTaskData.and.returnValue(of([]));
    policyService = jasmine.createSpyObj<RoomUploadPolicyService>(
      'RoomUploadPolicyService',
      ['list'],
    );
    policyService.list.and.returnValue(
      of([
        {
          roomId: 100,
          resolvedAccountId: 7,
          resolvedAccountName: '主账号',
        } as RoomUploadPolicy,
        {
          roomId: 101,
          resolvedAccountId: 7,
          resolvedAccountName: '主账号',
        } as RoomUploadPolicy,
        {
          roomId: 102,
          resolvedAccountId: 9,
          resolvedAccountName: '剪辑账号',
        } as RoomUploadPolicy,
      ]),
    );
    realtimeEvents = new FakeRealtimeSource();
    const storage = jasmine.createSpyObj<StorageService>('StorageService', [
      'getData',
      'setData',
    ]);
    storage.getData.and.returnValue(null);

    await TestBed.configureTestingModule({
      declarations: [TasksComponent, FilterTasksPipe],
      providers: [
        {
          provide: NzNotificationService,
          useValue: jasmine.createSpyObj<NzNotificationService>(
            'NzNotificationService',
            ['error'],
          ),
        },
        {
          provide: StorageService,
          useValue: storage,
        },
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: () => realtimeEvents },
        { provide: Router, useValue: { url: '/tasks' } },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
        { provide: TaskService, useValue: taskService },
        { provide: RoomUploadPolicyService, useValue: policyService },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TasksComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(component.selection).toBe(DataSelection.ALL);
  });

  it('uses one shared primary-page container', () => {
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1,
    );
  });

  it('identifies the page as room management', () => {
    expect(fixture.nativeElement.textContent).toContain('房间管理');
  });

  it('loads one snapshot and applies task SSE updates without polling HTTP', () => {
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);

    realtimeEvents.next({
      type: 'tasks',
      data: { tasks: [taskData] },
    });

    expect(component.dataList).toEqual([taskData]);
    expect(component.loading).toBeFalse();
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);
  });

  it('skips the bootstrap resync and reloads for the next resync', () => {
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(2);
  });

  it('loads one room policy snapshot and derives unique account filters', () => {
    const policyState = component as TasksComponent & {
      submissionAccountOptions?: readonly { label: string; value: number }[];
    };

    expect(policyService.list).toHaveBeenCalledTimes(1);
    expect(policyState.submissionAccountOptions).toEqual([
      { label: '主账号', value: 7 },
      { label: '剪辑账号', value: 9 },
    ]);
  });
});
