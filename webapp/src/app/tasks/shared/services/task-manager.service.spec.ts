import { TestBed } from '@angular/core/testing';
import { NzMessageService } from 'ng-zorro-antd/message';

import { TaskService } from './task.service';
import { TaskManagerService } from './task-manager.service';

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
          ]),
        },
      ],
    });
    service = TestBed.inject(TaskManagerService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
