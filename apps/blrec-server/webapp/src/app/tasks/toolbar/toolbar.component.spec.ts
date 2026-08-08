import { Clipboard } from '@angular/cdk/clipboard';
import { BreakpointObserver } from '@angular/cdk/layout';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NEVER } from 'rxjs';

import { DataSelection } from '../shared/task.model';
import { TaskManagerService } from '../shared/services/task-manager.service';
import { ToolbarComponent } from './toolbar.component';

describe('ToolbarComponent', () => {
  let component: ToolbarComponent;
  let fixture: ComponentFixture<ToolbarComponent>;

  beforeEach(async () => {
    const breakpointObserver = jasmine.createSpyObj<BreakpointObserver>(
      'BreakpointObserver',
      ['observe'],
    );
    breakpointObserver.observe.and.returnValue(NEVER);

    await TestBed.configureTestingModule({
      declarations: [ToolbarComponent],
      imports: [NzDropDownModule],
      providers: [
        { provide: BreakpointObserver, useValue: breakpointObserver },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
          ]),
        },
        {
          provide: NzModalService,
          useValue: jasmine.createSpyObj<NzModalService>('NzModalService', [
            'confirm',
          ]),
        },
        {
          provide: Clipboard,
          useValue: jasmine.createSpyObj<Clipboard>('Clipboard', ['copy']),
        },
        {
          provide: TaskManagerService,
          useValue: jasmine.createSpyObj<TaskManagerService>(
            'TaskManagerService',
            ['getAllTaskRoomIds'],
          ),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(ToolbarComponent);
    component = fixture.componentInstance;
    component.selection = DataSelection.ALL;
    component.reverse = false;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('uses the combined monitoring and recording terminology', () => {
    expect(component.selections.map((item) => item.label)).toEqual([
      '全部',
      '录制中',
      '监控已开启',
      '监控已关闭',
      '直播',
      '轮播',
      '闲置',
    ]);
    expect(fixture.nativeElement.textContent).not.toContain('关闭录制');
    expect(
      component.automaticSubmissionOptions.map((item) => item.label),
    ).toEqual([
      '全部投稿状态',
      '自动投稿已开启',
      '自动投稿已关闭',
      '未设置投稿',
    ]);
    expect(fixture.nativeElement.textContent).not.toContain('开播开始日期');
  });

  it('offers submission visibility and account filters', () => {
    const filterState = component as ToolbarComponent & {
      submissionVisibilityOptions?: readonly { label: string }[];
    };

    expect(
      filterState.submissionVisibilityOptions?.map((item) => item.label),
    ).toEqual(['全部可见性', '公开', '仅自己可见']);
    expect(
      fixture.nativeElement.querySelector('[data-testid="visibility-filter"]'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('[data-testid="account-filter"]'),
    ).not.toBeNull();
  });

  it('keeps global actions read-only with respect to room configuration', () => {
    const legacyActions = component as ToolbarComponent & {
      startAllTasks?: unknown;
      stopAllTasks?: unknown;
      removeAllTasks?: unknown;
    };

    expect(legacyActions.startAllTasks).toBeUndefined();
    expect(legacyActions.stopAllTasks).toBeUndefined();
    expect(legacyActions.removeAllTasks).toBeUndefined();
    expect(component.updateAllTaskInfos).toEqual(jasmine.any(Function));
    expect(component.copyAllTaskRoomIds).toEqual(jasmine.any(Function));
  });
});
