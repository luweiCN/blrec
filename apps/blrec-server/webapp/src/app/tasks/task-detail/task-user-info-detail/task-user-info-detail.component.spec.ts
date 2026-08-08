import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { UserInfo } from '../../shared/task.model';
import { TaskUserInfoDetailComponent } from './task-user-info-detail.component';

const userInfo: UserInfo = {
  name: '',
  gender: '',
  face: '',
  uid: 1,
  level: 0,
  sign: '',
};

describe('TaskUserInfoDetailComponent', () => {
  let component: TaskUserInfoDetailComponent;
  let fixture: ComponentFixture<TaskUserInfoDetailComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [TaskUserInfoDetailComponent],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskUserInfoDetailComponent);
    component = fixture.componentInstance;
    component.userInfo = userInfo;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
