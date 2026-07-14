import { BreakpointObserver } from '@angular/cdk/layout';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NEVER } from 'rxjs';

import { SettingService } from 'src/app/settings/shared/services/setting.service';
import { TaskManagerService } from '../shared/services/task-manager.service';
import { TaskSettingsService } from '../shared/services/task-settings.service';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../shared/task.model';
import { TaskItemComponent } from './task-item.component';

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

describe('TaskItemComponent', () => {
  let component: TaskItemComponent;
  let fixture: ComponentFixture<TaskItemComponent>;

  beforeEach(async () => {
    const breakpointObserver = jasmine.createSpyObj<BreakpointObserver>(
      'BreakpointObserver',
      ['observe']
    );
    breakpointObserver.observe.and.returnValue(NEVER);
    const taskSettings = jasmine.createSpyObj<TaskSettingsService>(
      'TaskSettingsService',
      ['getSettings', 'updateSettings']
    );
    taskSettings.getSettings.and.returnValue({});

    await TestBed.configureTestingModule({
      declarations: [TaskItemComponent],
      imports: [NzDropDownModule],
      providers: [
        { provide: BreakpointObserver, useValue: breakpointObserver },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>(
            'NzMessageService',
            ['warning']
          ),
        },
        {
          provide: NzModalService,
          useValue: jasmine.createSpyObj<NzModalService>('NzModalService', [
            'confirm',
          ]),
        },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'getTaskOptions',
          ]),
        },
        {
          provide: TaskManagerService,
          useValue: jasmine.createSpyObj<TaskManagerService>(
            'TaskManagerService',
            ['updateTaskInfo']
          ),
        },
        { provide: TaskSettingsService, useValue: taskSettings },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskItemComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('data', taskData);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(component.stopped).toBeTrue();
  });

  it('creates the upload settings dialog only after its card action is used', () => {
    expect(
      fixture.nativeElement.querySelector('app-upload-policy-dialog')
    ).toBeNull();

    component.openUploadPolicyDialog();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('app-upload-policy-dialog')
    ).not.toBeNull();
  });
});
