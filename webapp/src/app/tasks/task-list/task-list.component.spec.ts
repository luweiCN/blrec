import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzMessageService } from 'ng-zorro-antd/message';
import { of } from 'rxjs';

import { SettingService } from '../../settings/shared/services/setting.service';
import { TaskManagerService } from '../shared/services/task-manager.service';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../shared/task.model';
import { TaskListComponent } from './task-list.component';

const taskData: TaskData = {
  user_info: {
    name: '测试主播',
    gender: '',
    face: '',
    uid: 1,
    level: 0,
    sign: '',
  },
  room_info: {
    uid: 1,
    room_id: 1,
    short_room_id: 0,
    area_id: 1,
    area_name: '单机游戏',
    parent_area_id: 2,
    parent_area_name: '游戏',
    live_status: 0,
    live_start_time: 1_000,
    online: 0,
    title: '测试直播',
    cover: '',
    tags: '',
    description: '',
  },
  task_status: {
    monitor_enabled: true,
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

describe('TaskListComponent', () => {
  let component: TaskListComponent;
  let fixture: ComponentFixture<TaskListComponent>;
  let taskManager: jasmine.SpyObj<TaskManagerService>;

  beforeEach(async () => {
    taskManager = jasmine.createSpyObj<TaskManagerService>(
      'TaskManagerService',
      ['runBatchAction']
    );
    taskManager.runBatchAction.and.returnValue(of({ results: [] }));
    await TestBed.configureTestingModule({
      declarations: [TaskListComponent],
      providers: [
        { provide: TaskManagerService, useValue: taskManager },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'getTaskOptions',
            'getSettings',
            'changeTaskOptions',
          ]),
        },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>(
            'NzMessageService',
            ['success', 'error']
          ),
        },
        {
          provide: NzModalService,
          useValue: jasmine.createSpyObj<NzModalService>('NzModalService', [
            'confirm',
          ]),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskListComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('renders a selectable list and runs actions for eligible rooms', () => {
    fixture.componentRef.setInput('dataList', [taskData]);
    fixture.detectChanges();

    component.setTaskSelected(1, true);
    component.runBatchAction('start');

    expect(component.selectedCount).toBe(1);
    expect(taskManager.runBatchAction).toHaveBeenCalledOnceWith('start', [1]);
    expect(fixture.nativeElement.querySelector('.task-list-header')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('nz-card')).toBeNull();
  });

  it('presents monitoring and recording as one task state', () => {
    fixture.componentRef.setInput('dataList', [taskData]);
    fixture.detectChanges();

    const headerElements = fixture.nativeElement.querySelectorAll(
      '.task-list-header [role="columnheader"]',
    ) as NodeListOf<HTMLElement>;
    const headers = Array.from(headerElements).map(
      (header) => header.textContent?.trim() ?? '',
    );
    expect(headers).toEqual(['', '直播间', '直播状态', '监控与录制', '操作']);

    component.setTaskSelected(1, true);
    fixture.detectChanges();
    const actions = fixture.nativeElement.querySelector(
      '[data-testid="recording-task-batch-actions"]',
    );
    expect(actions.textContent).not.toContain('开启录制');
    expect(actions.textContent).not.toContain('关闭录制');
    expect(actions.textContent).not.toContain('强制关闭录制');
  });
});
