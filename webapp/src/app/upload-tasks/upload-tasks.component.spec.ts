import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';

import { UploadTasksComponent } from './upload-tasks.component';

@Component({ selector: 'app-recording-sessions', template: '' })
class RecordingSessionsStubComponent {}

describe('UploadTasksComponent', () => {
  let fixture: ComponentFixture<UploadTasksComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [UploadTasksComponent, RecordingSessionsStubComponent],
      imports: [NoopAnimationsModule, NzPageHeaderModule],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadTasksComponent);
  });

  it('renders the upload-task heading and list', () => {
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('上传任务');
    expect(
      fixture.nativeElement.querySelector('app-recording-sessions')
    ).not.toBeNull();
  });
});
