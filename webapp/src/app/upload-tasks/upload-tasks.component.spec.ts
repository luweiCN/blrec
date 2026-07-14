import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { UploadTasksComponent } from './upload-tasks.component';

@Component({ selector: 'app-recording-sessions', template: '' })
class RecordingSessionsStubComponent {}

describe('UploadTasksComponent', () => {
  let fixture: ComponentFixture<UploadTasksComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [UploadTasksComponent, RecordingSessionsStubComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadTasksComponent);
  });

  it('renders the upload-task list once', () => {
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelectorAll('app-recording-sessions').length
    ).toBe(1);
  });
});
