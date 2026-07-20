import { CommonModule } from '@angular/common';
import { NO_ERRORS_SCHEMA, Pipe, PipeTransform } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import {
  CloudUploadOutline,
  MoreOutline,
  SettingOutline,
} from '@ant-design/icons-angular/icons';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzDropDownDirective, NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';
import { of } from 'rxjs';

import { SettingService } from 'src/app/settings/shared/services/setting.service';
import { TaskManagerService } from '../shared/services/task-manager.service';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../shared/task.model';
import { TaskItemComponent } from './task-item.component';
import { RoomUploadPolicy } from '../upload-policy-dialog/room-upload-policy.model';
import { RoomUploadPolicyService } from '../upload-policy-dialog/room-upload-policy.service';

const uploadPolicy: RoomUploadPolicy = {
  roomId: 1,
  accountMode: 'primary',
  accountId: null,
  resolvedAccountId: 1,
  resolvedAccountName: '投稿账号',
  enabled: true,
  titleTemplate: '{{ title }}',
  descriptionTemplate: '',
  partTitleTemplate: 'P{{ part_index }}',
  dynamicTemplate: '',
  tid: 17,
  tags: '直播,录播',
  creationStatementId: -1,
  originalAuthorization: true,
  source: '',
  isOnlySelf: false,
  publishDynamic: true,
  upSelectionReply: false,
  upCloseReply: false,
  upCloseDanmu: false,
  autoComment: true,
  danmakuBackfill: true,
  filters: {},
  collectionSeasonId: null,
  collectionSectionId: null,
  coverMode: 'live',
  coverAssetId: null,
  publishDelaySeconds: 0,
  retentionMode: 'submitted',
  retentionDays: 5,
  blockedReason: null,
  createdAt: 1,
  updatedAt: 1,
};

@Pipe({ name: 'dataurl' })
class DataurlStubPipe implements PipeTransform {
  transform(value: string) {
    return of(value);
  }
}

const taskData: TaskData = {
  title_keywords: ['比赛', 'HIGHLIGHT', '复盘'],
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
  let taskManager: jasmine.SpyObj<TaskManagerService>;
  let policyService: jasmine.SpyObj<RoomUploadPolicyService>;

  beforeEach(async () => {
    taskManager = jasmine.createSpyObj<TaskManagerService>(
      'TaskManagerService',
      ['updateTaskInfo', 'startTask', 'stopTask'],
    );
    taskManager.startTask.and.returnValue(
      of({ status: 'succeeded', results: [] })
    );
    taskManager.stopTask.and.returnValue(
      of({ status: 'succeeded', results: [] })
    );
    policyService = jasmine.createSpyObj<RoomUploadPolicyService>(
      'RoomUploadPolicyService',
      ['save'],
    );
    policyService.save.and.returnValue(of({ ...uploadPolicy, enabled: false }));
    await TestBed.configureTestingModule({
      declarations: [TaskItemComponent, DataurlStubPipe],
      imports: [CommonModule, NzButtonModule, NzDropDownModule, NzIconModule],
      providers: [
        {
          provide: NZ_ICONS,
          useValue: [CloudUploadOutline, MoreOutline, SettingOutline],
        },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'warning',
            'success',
            'error',
          ]),
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
          useValue: taskManager,
        },
        { provide: RoomUploadPolicyService, useValue: policyService },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
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
    expect(fixture.nativeElement.querySelector('nz-card')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('测试主播');
  });

  it('emits row selection changes', () => {
    const selectionChange = spyOn(component.selectedChange, 'emit');

    component.setSelected(true);

    expect(selectionChange).toHaveBeenCalledOnceWith(true);
  });

  it('creates the upload settings dialog only after its card action is used', () => {
    expect(
      fixture.nativeElement.querySelector('app-upload-policy-dialog'),
    ).toBeNull();

    component.openUploadPolicyDialog();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('app-upload-policy-dialog'),
    ).not.toBeNull();
  });

  it('renders more actions with the shared ellipsis dropdown', () => {
    const trigger = fixture.nativeElement.querySelector(
      '[aria-label="更多任务操作"]',
    ) as HTMLButtonElement | null;
    const dropdown = fixture.debugElement
      .query(By.directive(NzDropDownDirective))
      .injector.get(NzDropDownDirective);

    expect(trigger).not.toBeNull();
    expect(trigger?.classList).toContain('ant-btn-text');
    expect(trigger?.querySelector('i[nz-icon][nztype="more"]')).not.toBeNull();
    expect(trigger?.textContent?.trim()).toBe('');
    expect(dropdown.nzOverlayClassName).toBe('action-dropdown-overlay');
  });

  it('does not expose current-file cutting from room management', () => {
    expect(
      fixture.nativeElement.querySelector(
        '[nztooltiptitle="切割当前录制文件"]',
      ),
    ).toBeNull();
  });

  it('only offers force interruption for a running room', () => {
    const forceState = component as TaskItemComponent & {
      canForceInterrupt?: boolean;
    };
    expect(forceState.canForceInterrupt).toBeFalse();

    component.data = {
      ...taskData,
      task_status: {
        ...taskData.task_status,
        running_status: RunningStatus.RECORDING,
      },
    };

    expect(forceState.canForceInterrupt).toBeTrue();
  });

  it('explains that force interruption is only for abnormal stopping', () => {
    component.data = {
      ...taskData,
      task_status: {
        ...taskData.task_status,
        running_status: RunningStatus.RECORDING,
      },
    };

    component.stopTask(true);

    const modal = TestBed.inject(
      NzModalService,
    ) as jasmine.SpyObj<NzModalService>;
    expect(modal.confirm).toHaveBeenCalled();
    const confirmation = modal.confirm.calls.mostRecent().args[0]!;
    expect(confirmation.nzTitle).toBe('确定强制中断当前录制？');
    expect(confirmation.nzContent).toContain('普通停止无效');
  });

  it('uses one control to start monitoring and recording together', () => {
    expect(component.taskEnabled).toBeFalse();

    component.toggleTask();

    expect(taskManager.startTask).toHaveBeenCalledOnceWith(1);
    expect(taskManager.stopTask).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).not.toContain('监控已关闭');
    expect(fixture.nativeElement.textContent).not.toContain('监控已开启');
    expect(
      fixture.nativeElement.querySelector(
        '[data-testid="task-enabled-switch"]',
      ),
    ).not.toBeNull();
  });

  it('uses the same control to stop monitoring and recording together', () => {
    const runningTask: TaskData = {
      ...taskData,
      task_status: {
        ...taskData.task_status,
        monitor_enabled: true,
        recorder_enabled: true,
        running_status: RunningStatus.WAITING,
      },
    };
    fixture.componentRef.setInput('data', runningTask);
    fixture.detectChanges();

    expect(component.taskEnabled).toBeTrue();
    component.toggleTask();

    expect(taskManager.stopTask).toHaveBeenCalledOnceWith(1);
    expect(taskManager.startTask).not.toHaveBeenCalled();
  });

  it('opens投稿设置 before first enabling automatic submission', () => {
    expect(component.uploadPolicy).toBeNull();

    component.toggleAutomaticSubmission();

    expect(component.uploadPolicyDialogVisible).toBeTrue();
    expect(policyService.save).not.toHaveBeenCalled();
  });

  it('toggles an existing automatic submission policy directly', () => {
    fixture.componentRef.setInput('uploadPolicy', uploadPolicy);
    fixture.detectChanges();

    component.toggleAutomaticSubmission();

    expect(policyService.save).toHaveBeenCalledOnceWith(
      1,
      jasmine.objectContaining({ enabled: false }),
    );
  });

  it('shows recording title conditions and the effective submission summary', () => {
    fixture.componentRef.setInput('uploadPolicy', {
      ...uploadPolicy,
      isOnlySelf: true,
      publishDelaySeconds: 7_200,
      collectionSeasonId: 88,
      collectionSectionId: 99,
      retentionMode: 'approved',
      retentionDays: 5,
      danmakuBackfill: false,
    });
    fixture.componentRef.setInput('collectionLabel', '合集 #88');
    fixture.detectChanges();

    const recording = fixture.nativeElement.querySelector(
      '[data-testid="recording-summary"]',
    ) as HTMLElement;
    const submission = fixture.nativeElement.querySelector(
      '[data-testid="submission-summary"]',
    ) as HTMLElement;

    expect(recording.textContent).toContain('标题匹配');
    expect(recording.textContent).toContain('比赛');
    expect(recording.textContent).toContain('等 3 项');
    expect(submission.textContent).toContain('投稿账号');
    expect(submission.textContent).toContain('仅自己可见');
    expect(submission.textContent).toContain('原创');
    expect(submission.textContent).toContain('定时发布');
    expect(submission.textContent).toContain('合集 #88');
    expect(submission.textContent).toContain('审核通过 5 天后删除');
    expect(submission.textContent).toContain('不回灌弹幕');
  });
});
