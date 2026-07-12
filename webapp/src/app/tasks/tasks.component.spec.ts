import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzNotificationService } from 'ng-zorro-antd/notification';
import { NEVER } from 'rxjs';

import { StorageService } from '../core/services/storage.service';
import { FilterTasksPipe } from './shared/pipes/filter-tasks.pipe';
import { TaskService } from './shared/services/task.service';
import { DataSelection } from './shared/task.model';
import { TasksComponent } from './tasks.component';

describe('TasksComponent', () => {
  let component: TasksComponent;
  let fixture: ComponentFixture<TasksComponent>;

  beforeEach(async () => {
    const taskService = jasmine.createSpyObj<TaskService>('TaskService', [
      'getAllTaskData',
    ]);
    taskService.getAllTaskData.and.returnValue(NEVER);
    const storage = jasmine.createSpyObj<StorageService>('StorageService', [
      'getData',
      'setData',
    ]);
    storage.getData.and.returnValue(null);

    await TestBed.configureTestingModule({
      declarations: [TasksComponent, FilterTasksPipe],
      providers: [
        {
          provide: NzNotificationService,
          useValue: jasmine.createSpyObj<NzNotificationService>(
            'NzNotificationService',
            ['error']
          ),
        },
        {
          provide: StorageService,
          useValue: storage,
        },
        { provide: TaskService, useValue: taskService },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TasksComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(component.selection).toBe(DataSelection.ALL);
  });
});
