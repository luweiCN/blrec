import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { RecordingSessionService } from '../shared/recording-session.service';
import { RecordingSessionsComponent } from './recording-sessions.component';

describe('RecordingSessionsComponent', () => {
  let fixture: ComponentFixture<RecordingSessionsComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['listSessions']
    );
    service.listSessions.and.returnValue(
      of({
        degradedReason: null,
        sessions: [
          {
            id: 1,
            roomId: 100,
            broadcastSessionKey: '100:900',
            liveStartTime: 900,
            state: 'closed',
            startedAt: 900,
            endedAt: 1_000,
            parts: [
              {
                id: 2,
                runId: 'run-1',
                partIndex: 1,
                sourcePath: '/rec/p1.flv',
                finalPath: '/rec/p1.mp4',
                xmlPath: '/rec/p1.xml',
                recordStartTime: 901,
                artifactState: 'ready',
                xmlCompleted: true,
                sourceExists: false,
                finalExists: true,
                errorMessage: null,
              },
            ],
          },
        ],
      })
    );

    await TestBed.configureTestingModule({
      declarations: [RecordingSessionsComponent],
      imports: [
        CommonModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzCardModule,
        NzCollapseModule,
        NzEmptyModule,
        NzSpinModule,
        NzTagModule,
      ],
      providers: [{ provide: RecordingSessionService, useValue: service }],
    }).compileComponents();

    fixture = TestBed.createComponent(RecordingSessionsComponent);
  });

  it('shows persisted sessions, part order, final files, and XML state', () => {
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(service.listSessions).toHaveBeenCalledOnceWith(50);
    expect(text).toContain('上传任务列表');
    expect(text).toContain('房间 100');
    expect(text).toContain('已归集');
    expect(text).toContain('P1');
    expect(text).toContain('/rec/p1.mp4');
    expect(text).toContain('/rec/p1.xml');
  });

  it('shows a retry action when session loading fails', () => {
    service.listSessions.and.returnValue(
      throwError(() => new Error('upload database is unavailable'))
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'upload database is unavailable'
    );
    expect(
      fixture.nativeElement.querySelector('[data-testid="retry-sessions"]')
    ).not.toBeNull();
  });
});
