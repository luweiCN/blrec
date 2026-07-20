import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NzMessageService } from 'ng-zorro-antd/message';

import { TaskService } from './task.service';
import {
  AddTaskResultMessage,
  TaskManagerService,
} from './task-manager.service';
import { ControlOperationService } from 'src/app/core/services/control-operation.service';
import { of, throwError } from 'rxjs';
import { TaskData } from '../task.model';

describe('TaskManagerService', () => {
  let service: TaskManagerService;
  let message: jasmine.SpyObj<NzMessageService>;

  beforeEach(() => {
    message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'error',
      'info',
      'success',
      'warning',
    ]);
    TestBed.configureTestingModule({
      providers: [
        {
          provide: NzMessageService,
          useValue: message,
        },
        {
          provide: TaskService,
          useValue: jasmine.createSpyObj<TaskService>('TaskService', [
            'getAllTaskData',
            'runBatchAction',
            'addTask',
            'removeTask',
            'removeAllTasks',
          ]),
        },
        {
          provide: ControlOperationService,
          useValue: jasmine.createSpyObj<ControlOperationService>(
            'ControlOperationService',
            ['poll']
          ),
        },
      ],
    });
    service = TestBed.inject(TaskManagerService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('emits submitted then terminal add result and refreshes with real room ID', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    taskService.addTask.and.returnValue(
      of({
        operationId: 'membership-operation-1',
        status: 'accepted',
        requestedRoomId: 6,
      })
    );
    taskService.getAllTaskData.and.returnValue(of([]));
    operations.poll.and.returnValue(
      of({
        id: 'membership-operation-1',
        lane: 'room-membership',
        kind: 'add',
        targetKey: '6',
        attempt: 1,
        generation: 1,
        status: 'succeeded',
        result: {
          requestedRoomId: 6,
          resolvedRoomId: 3582149,
          collected: true,
          upload: false,
        },
        errorCode: null,
        createdAt: 1,
        updatedAt: 2,
        steps: [],
      })
    );
    const results: AddTaskResultMessage[] = [];

    service.addTask(6).subscribe((result) => results.push(result));

    expect(results).toEqual([
      { type: 'info', message: '6: 添加任务已提交' },
      { type: 'success', message: '3582149: 成功添加任务' },
    ]);
    expect(operations.poll).toHaveBeenCalledOnceWith(
      'membership-operation-1'
    );
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);
  });

  it('emits admission then terminal per-item results and refreshes once', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    taskService.runBatchAction.and.returnValue(
      of({
        operationId: 'operation-1',
        status: 'accepted',
        results: [
          {
            roomId: 100,
            accepted: true,
            status: 'queued',
            operationId: 'operation-1',
            errorCode: null,
            message: '操作已提交',
          },
        ],
      })
    );
    taskService.getAllTaskData.and.returnValue(of([]));
    operations.poll.and.returnValue(
      of({
        id: 'operation-1',
        lane: 'task-state',
        kind: 'start',
        targetKey: '100',
        attempt: 1,
        generation: 1,
        status: 'succeeded',
        result: null,
        errorCode: null,
        createdAt: 1,
        updatedAt: 2,
        steps: [
          {
            key: '100',
            generation: 1,
            status: 'succeeded',
            result: { roomId: 100 },
            errorCode: null,
          },
        ],
      })
    );
    const statuses: string[] = [];

    service.runBatchAction('start', [100]).subscribe((response) => {
      statuses.push(response.status ?? 'none');
    });

    expect(statuses).toEqual(['accepted', 'succeeded']);
    expect(operations.poll).toHaveBeenCalledOnceWith('operation-1');
    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);
  });

  it('publishes the terminal task snapshot to the page owner', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    const refreshed = [{ room_info: { room_id: 100 } }] as TaskData[];
    taskService.runBatchAction.and.returnValue(
      of({
        operationId: 'operation-1',
        status: 'accepted',
        results: [],
      })
    );
    taskService.getAllTaskData.and.returnValue(of(refreshed));
    operations.poll.and.returnValue(
      of({
        id: 'operation-1',
        lane: 'task-state',
        kind: 'start',
        targetKey: '100',
        attempt: 1,
        generation: 1,
        status: 'succeeded',
        result: null,
        errorCode: null,
        createdAt: 1,
        updatedAt: 2,
        steps: [],
      })
    );
    const snapshots: TaskData[][] = [];
    service.taskDataRefresh$.subscribe((tasks) => snapshots.push(tasks));

    service.runBatchAction('start', [100]).subscribe();

    expect(snapshots).toEqual([refreshed]);
  });

  it('refreshes task state when admission is already terminal', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    const refreshed = [{ room_info: { room_id: 100 } }] as TaskData[];
    taskService.runBatchAction.and.returnValue(
      of({
        operationId: 'operation-1',
        status: 'succeeded',
        results: [batchResult(100, true, 'succeeded')],
      })
    );
    taskService.getAllTaskData.and.returnValue(of(refreshed));
    const snapshots: TaskData[][] = [];
    service.taskDataRefresh$.subscribe((tasks) => snapshots.push(tasks));

    service.runBatchAction('start', [100]).subscribe();

    expect(taskService.getAllTaskData).toHaveBeenCalledTimes(1);
    expect(snapshots).toEqual([refreshed]);
    expect(operations.poll).not.toHaveBeenCalled();
  });

  it('preserves terminal success when the follow-up refresh fails', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    taskService.runBatchAction.and.returnValue(
      of({
        operationId: 'operation-1',
        status: 'accepted',
        results: [batchResult(100, true, 'queued')],
      })
    );
    taskService.getAllTaskData.and.returnValue(
      throwError(
        () =>
          new HttpErrorResponse({
            status: 503,
            statusText: 'Unavailable',
            url: '/api/v1/tasks/data',
          })
      )
    );
    operations.poll.and.returnValue(
      of(controlOperation('succeeded', [controlStep(100, 'succeeded')]))
    );
    const statuses: string[] = [];
    let observedError: unknown;

    service.runBatchAction('start', [100]).subscribe({
      next: (response) => statuses.push(response.status ?? 'none'),
      error: (error) => {
        observedError = error;
      },
    });

    expect(statuses).toEqual(['accepted', 'succeeded']);
    expect(observedError).toBeUndefined();
    expect(message.warning).toHaveBeenCalledOnceWith(
      jasmine.stringMatching(/^任务操作已完成，但刷新任务状态失败：/)
    );
    expect(message.error).not.toHaveBeenCalled();
  });

  it('reports batch admission once and terminal counts from real statuses', () => {
    const taskService = TestBed.inject(
      TaskService
    ) as jasmine.SpyObj<TaskService>;
    const operations = TestBed.inject(
      ControlOperationService
    ) as jasmine.SpyObj<ControlOperationService>;
    taskService.runBatchAction.and.returnValue(
      of({
        operationId: 'operation-1',
        status: 'accepted',
        results: [
          batchResult(100, true, 'queued'),
          batchResult(200, true, 'queued'),
          batchResult(300, false, 'rejected'),
        ],
      })
    );
    taskService.getAllTaskData.and.returnValue(of([]));
    operations.poll.and.returnValue(
      of(
        controlOperation('accepted', [
          controlStep(100, 'queued'),
          controlStep(200, 'queued'),
          controlStep(300, 'rejected'),
        ]),
        controlOperation('accepted', [
          controlStep(100, 'queued'),
          controlStep(200, 'queued'),
          controlStep(300, 'rejected'),
        ]),
        controlOperation('running', [
          controlStep(100, 'running'),
          controlStep(200, 'queued'),
          controlStep(300, 'rejected'),
        ]),
        controlOperation('failed', [
          controlStep(100, 'succeeded'),
          controlStep(200, 'failed'),
          controlStep(300, 'rejected'),
        ])
      )
    );

    service.runBatchAction('start', [100, 200, 300]).subscribe();

    expect(message.success).toHaveBeenCalledOnceWith('已提交 2 个任务');
    expect(message.warning).toHaveBeenCalledOnceWith(
      jasmine.stringMatching(/^成功 1 个，失败 2 个：/)
    );
  });
});

function batchResult(
  roomId: number,
  accepted: boolean,
  status: 'queued' | 'rejected' | 'succeeded'
) {
  return {
    roomId,
    accepted,
    status,
    operationId: 'operation-1',
    errorCode: status === 'rejected' ? 'TASK_NOT_FOUND' : null,
    message: '',
  } as const;
}

function controlStep(
  roomId: number,
  status: 'queued' | 'rejected' | 'running' | 'succeeded' | 'failed'
) {
  return {
    key: String(roomId),
    generation: 1,
    status,
    result: null,
    errorCode:
      status === 'failed'
        ? 'TASK_LIFECYCLE_FAILED'
        : status === 'rejected'
        ? 'TASK_NOT_FOUND'
        : null,
  } as const;
}

function controlOperation(
  status: 'accepted' | 'running' | 'succeeded' | 'failed',
  steps: ReturnType<typeof controlStep>[]
) {
  return {
    id: 'operation-1',
    lane: 'task-state',
    kind: 'start',
    targetKey: '100,200,300',
    attempt: 1,
    generation: 1,
    status,
    result: null,
    errorCode: status === 'failed' ? 'TASK_LIFECYCLE_FAILED' : null,
    createdAt: 1,
    updatedAt: 2,
    steps,
  } as const;
}
