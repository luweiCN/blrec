import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { PART_PLAYER_LOADER } from './part-video-dialog/part-player.loader';
import type { PartPlayerLoader } from './part-video-dialog/part-player.loader';
import { UploadTasksComponent } from './upload-tasks.component';

@Component({ selector: 'app-recording-sessions', template: '' })
class RecordingSessionsStubComponent {
  @Input() scope: 'recordings' | 'uploads' = 'uploads';
}

describe('UploadTasksComponent', () => {
  let fixture: ComponentFixture<UploadTasksComponent>;
  let playerLoader: jasmine.Spy<PartPlayerLoader>;

  beforeEach(async () => {
    playerLoader = jasmine.createSpy<PartPlayerLoader>('partPlayerLoader');
    await TestBed.configureTestingModule({
      declarations: [UploadTasksComponent, RecordingSessionsStubComponent],
      providers: [{ provide: PART_PLAYER_LOADER, useValue: playerLoader }],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadTasksComponent);
  });

  it('renders the upload-task list once', () => {
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelectorAll('app-recording-sessions').length,
    ).toBe(1);
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1,
    );
    expect(fixture.nativeElement.querySelector('app-clip-library')).toBeNull();
    expect('clipLibrary' in fixture.componentInstance).toBeFalse();
    expect(playerLoader).not.toHaveBeenCalled();
  });
});
