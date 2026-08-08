import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SharedModule } from 'src/app/shared/shared.module';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskStatus,
} from '../../shared/task.model';
import { TaskPostprocessingDetailComponent } from './task-postprocessing-detail.component';

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

describe('TaskPostprocessingDetailComponent', () => {
  let component: TaskPostprocessingDetailComponent;
  let fixture: ComponentFixture<TaskPostprocessingDetailComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [TaskPostprocessingDetailComponent],
      imports: [SharedModule],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskPostprocessingDetailComponent);
    component = fixture.componentInstance;
    component.taskStatus = taskStatus;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
