import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { NzNotificationService } from 'ng-zorro-antd/notification';
import { of } from 'rxjs';

import { TaskService } from '../shared/services/task.service';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../shared/task.model';
import { TaskDetailComponent } from './task-detail.component';

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

describe('TaskDetailComponent', () => {
  let component: TaskDetailComponent;
  let fixture: ComponentFixture<TaskDetailComponent>;
  let taskService: jasmine.SpyObj<TaskService>;

  beforeEach(async () => {
    taskService = jasmine.createSpyObj<TaskService>('TaskService', [
      'getTaskData',
      'getVideoFileDetails',
      'getDanmakuFileDetails',
    ]);
    taskService.getTaskData.and.returnValue(of(taskData));
    taskService.getVideoFileDetails.and.returnValue(of([]));
    taskService.getDanmakuFileDetails.and.returnValue(of([]));

    await TestBed.configureTestingModule({
      declarations: [TaskDetailComponent],
      providers: [
        {
          provide: ActivatedRoute,
          useValue: jasmine.createSpyObj<ActivatedRoute>('ActivatedRoute', [], {
            paramMap: of(convertToParamMap({ id: '1' })),
          }),
        },
        {
          provide: Router,
          useValue: jasmine.createSpyObj<Router>('Router', ['navigate']),
        },
        {
          provide: NzNotificationService,
          useValue: jasmine.createSpyObj<NzNotificationService>(
            'NzNotificationService',
            ['error']
          ),
        },
        {
          provide: TaskService,
          useValue: taskService,
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskDetailComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(component.roomId).toBe(1);
    expect(taskService.getTaskData).toHaveBeenCalledWith(1);
    expect(taskService.getVideoFileDetails).toHaveBeenCalledWith(1);
    expect(taskService.getDanmakuFileDetails).toHaveBeenCalledWith(1);
  });
});
