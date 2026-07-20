import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
  AddTaskResultMessage,
  TaskManagerService,
} from '../shared/services/task-manager.service';
import { AddTaskDialogComponent } from './add-task-dialog.component';
import { of, Subject } from 'rxjs';

describe('AddTaskDialogComponent', () => {
  let component: AddTaskDialogComponent;
  let fixture: ComponentFixture<AddTaskDialogComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [AddTaskDialogComponent],
      providers: [
        {
          provide: TaskManagerService,
          useValue: jasmine.createSpyObj<TaskManagerService>(
            'TaskManagerService',
            ['addTask']
          ),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(AddTaskDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('ends loading on admission and cancels terminal polling when destroyed', () => {
    const pending = new Subject<{
      type: 'info' | 'success';
      message: string;
    }>();
    const taskManager = TestBed.inject(
      TaskManagerService
    ) as jasmine.SpyObj<TaskManagerService>;
    taskManager.addTask.and.returnValue(pending);
    component.inputControl.setValue('100');

    component.handleConfirm();
    expect(component.pending).toBeTrue();
    pending.next({ type: 'info', message: '100: 添加任务已提交' });
    expect(component.pending).toBeFalse();
    expect(pending.observers.length).toBe(1);

    fixture.destroy();
    expect(pending.observers.length).toBe(0);
  });

  it('closes after submitted operations reach terminal success', () => {
    const taskManager = TestBed.inject(
      TaskManagerService
    ) as jasmine.SpyObj<TaskManagerService>;
    taskManager.addTask.and.returnValue(
      of(
        {
          type: 'info',
          message: '100: 添加任务已提交',
        } as AddTaskResultMessage,
        {
          type: 'success',
          message: '100: 成功添加任务',
        } as AddTaskResultMessage
      )
    );
    component.setVisible(true);
    component.inputControl.setValue('100');

    component.handleConfirm();

    expect(component.visible).toBeFalse();
  });
});
