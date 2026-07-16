import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzNotificationService } from 'ng-zorro-antd/notification';
import { of, Subject } from 'rxjs';

import { RealtimeEvent, RealtimeService } from '../core/services/realtime.service';
import { StorageService } from '../core/services/storage.service';
import { FilterTasksPipe } from './shared/pipes/filter-tasks.pipe';
import { TaskService } from './shared/services/task.service';
import {
  DataSelection,
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from './shared/task.model';
import { TasksComponent } from './tasks.component';

describe('TasksComponent', () => {
  let component: TasksComponent;
  let fixture: ComponentFixture<TasksComponent>;
  let taskService: jasmine.SpyObj<TaskService>;
  let realtimeEvents: Subject<RealtimeEvent>;

  const taskData: TaskData = {
    user_info: { name: '主播', gender: '', face: '', uid: 1, level: 0, sign: '' },
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
    realtimeEvents = new Subject<RealtimeEvent>();
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
            ['error']
          ),
        },
        {
          provide: StorageService,
          useValue: storage,
        },
        {
          provide: RealtimeService,
          useValue: { events$: realtimeEvents.asObservable() },
        },
        { provide: TaskService, useValue: taskService },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
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
      1
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
});
