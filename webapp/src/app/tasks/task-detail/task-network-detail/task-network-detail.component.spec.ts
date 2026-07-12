import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SharedModule } from 'src/app/shared/shared.module';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskStatus,
} from '../../shared/task.model';
import { TaskNetworkDetailComponent } from './task-network-detail.component';

const taskStatus: TaskStatus = {
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
};

describe('TaskNetworkDetailComponent', () => {
  let component: TaskNetworkDetailComponent;
  let fixture: ComponentFixture<TaskNetworkDetailComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [TaskNetworkDetailComponent],
      imports: [SharedModule],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskNetworkDetailComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('taskStatus', taskStatus);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
