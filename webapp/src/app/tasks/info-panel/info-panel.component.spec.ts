import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzNotificationService } from 'ng-zorro-antd/notification';
import { of, Subject } from 'rxjs';

import { RealtimeEvent, RealtimeService } from '../../core/services/realtime.service';
import { TaskService } from '../shared/services/task.service';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../shared/task.model';
import { InfoPanelComponent } from './info-panel.component';

const taskData: TaskData = {
  user_info: { name: '', gender: '', face: '', uid: 1, level: 0, sign: '' },
  room_info: {
    uid: 1,
    room_id: 1,
    short_room_id: 0,
    area_id: 1,
    area_name: '',
    parent_area_id: 1,
    parent_area_name: '',
    live_status: 0,
    live_start_time: 0,
    online: 0,
    title: '',
    cover: '',
    tags: '',
    description: '',
  },
  task_status: {
    monitor_enabled: false,
    recorder_enabled: false,
    running_status: RunningStatus.STOPPED,
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

describe('InfoPanelComponent', () => {
  let component: InfoPanelComponent;
  let fixture: ComponentFixture<InfoPanelComponent>;
  let taskService: jasmine.SpyObj<TaskService>;
  let realtimeEvents: Subject<RealtimeEvent>;

  beforeEach(async () => {
    taskService = jasmine.createSpyObj<TaskService>('TaskService', [
      'getStreamProfile',
      'getMetadata',
    ]);
    taskService.getStreamProfile.and.returnValue(of({}));
    taskService.getMetadata.and.returnValue(of(null));
    realtimeEvents = new Subject<RealtimeEvent>();

    await TestBed.configureTestingModule({
      declarations: [InfoPanelComponent],
      providers: [
        {
          provide: NzNotificationService,
          useValue: jasmine.createSpyObj<NzNotificationService>(
            'NzNotificationService',
            ['error']
          ),
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
    fixture = TestBed.createComponent(InfoPanelComponent);
    component = fixture.componentInstance;
    component.data = taskData;
    component.profile = {};
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('refreshes detail data only when the task stream identity changes', () => {
    expect(taskService.getStreamProfile).toHaveBeenCalledTimes(1);
    expect(taskService.getMetadata).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'tasks', data: { tasks: [taskData] } });
    expect(taskService.getStreamProfile).toHaveBeenCalledTimes(1);

    realtimeEvents.next({
      type: 'tasks',
      data: {
        tasks: [
          {
            ...taskData,
            task_status: {
              ...taskData.task_status,
              running_status: RunningStatus.RECORDING,
              stream_url: 'https://example.invalid/live.flv',
            },
          },
        ],
      },
    });

    expect(taskService.getStreamProfile).toHaveBeenCalledTimes(2);
    expect(taskService.getMetadata).toHaveBeenCalledTimes(2);
  });
});
