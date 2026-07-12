import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { RoomInfo } from '../../shared/task.model';
import { TaskRoomInfoDetailComponent } from './task-room-info-detail.component';

const roomInfo: RoomInfo = {
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
};

describe('TaskRoomInfoDetailComponent', () => {
  let component: TaskRoomInfoDetailComponent;
  let fixture: ComponentFixture<TaskRoomInfoDetailComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [TaskRoomInfoDetailComponent],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskRoomInfoDetailComponent);
    component = fixture.componentInstance;
    component.roomInfo = roomInfo;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
