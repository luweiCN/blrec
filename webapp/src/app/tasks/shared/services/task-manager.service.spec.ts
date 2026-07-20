import { TestBed } from '@angular/core/testing';
import { NzMessageService } from 'ng-zorro-antd/message';

import { TaskService } from './task.service';
import { TaskManagerService } from './task-manager.service';
import { ControlOperationService } from 'src/app/core/services/control-operation.service';
import { of } from 'rxjs';

describe('TaskManagerService', () => {
  let service: TaskManagerService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>(
            'NzMessageService',
            ['success']
          ),
        },
        {
          provide: TaskService,
          useValue: jasmine.createSpyObj<TaskService>('TaskService', [
            'getAllTaskData',
            'runBatchAction',
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
});
