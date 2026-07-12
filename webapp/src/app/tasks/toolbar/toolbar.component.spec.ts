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
      ['observe']
    );
    breakpointObserver.observe.and.returnValue(NEVER);

    await TestBed.configureTestingModule({
      declarations: [ToolbarComponent],
      imports: [NzDropDownModule],
      providers: [
        { provide: BreakpointObserver, useValue: breakpointObserver },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>(
            'NzMessageService',
            ['success']
          ),
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
            ['getAllTaskRoomIds']
          ),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
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
});
