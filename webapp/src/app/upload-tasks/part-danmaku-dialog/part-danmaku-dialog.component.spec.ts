import { CommonModule } from '@angular/common';
import { OverlayContainer } from '@angular/cdk/overlay';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzModalModule } from 'ng-zorro-antd/modal';

import { RecordingPart, RecordingSession } from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PartDanmakuDialogComponent } from './part-danmaku-dialog.component';

describe('PartDanmakuDialogComponent', () => {
  let fixture: ComponentFixture<PartDanmakuDialogComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let overlayContainer: OverlayContainer;

  const part = {
    id: 2,
    runId: 'run-1',
    partIndex: 1,
    sourcePath: '/rec/p1.flv',
    finalPath: null,
    xmlPath: '/rec/p1.xml',
    recordStartTime: 901,
    recordEndTime: 960,
    recordDurationSeconds: 59,
    fileSizeBytes: 1_024,
    danmakuCount: 1,
    artifactState: 'ready',
    xmlCompleted: true,
    sourceExists: true,
    finalExists: false,
    errorMessage: null,
  } as RecordingPart;
  const session = {
    roomId: 100,
    title: '直播标题',
  } as RecordingSession;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'listDanmaku']
    );
    service.listDanmaku.and.returnValue(
      of({
        items: [
          {
            index: 0,
            progressMs: 1_250,
            mode: 1,
            fontSize: 25,
            color: 16_777_215,
            user: '主播',
            uid: 42,
            content: '<script>不会执行</script>',
          },
          {
            index: 1,
            progressMs: 2_500,
            mode: 1,
            fontSize: 25,
            color: 16_777_215,
            user: null,
            uid: null,
            content: '未署名弹幕',
          },
        ],
        nextCursor: null,
      })
    );

    await TestBed.configureTestingModule({
      declarations: [PartDanmakuDialogComponent],
      imports: [
        CommonModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzModalModule,
      ],
      providers: [{ provide: RecordingSessionService, useValue: service }],
    }).compileComponents();

    fixture = TestBed.createComponent(PartDanmakuDialogComponent);
    overlayContainer = TestBed.inject(OverlayContainer);
    fixture.componentRef.setInput('session', session);
    fixture.componentRef.setInput('part', part);
    fixture.componentRef.setInput('visible', true);
  });

  it('shows text-only danmaku without creating a video player request', () => {
    fixture.detectChanges();

    expect(service.listDanmaku).toHaveBeenCalledOnceWith(2, 0, 100);
    expect(service.createMediaAccess).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('video')).toBeNull();
    expect(overlayContainer.getContainerElement().textContent).toContain(
      '<script>不会执行</script>'
    );
    expect(
      overlayContainer.getContainerElement().querySelector('.danmaku-content script')
    ).toBeNull();
    expect(
      Array.from(
        overlayContainer.getContainerElement().querySelectorAll('.danmaku-user')
      ).map((element) => element.textContent?.trim())
    ).toEqual(['用户：主播', '用户：未记录']);
    expect(
      Array.from(
        overlayContainer.getContainerElement().querySelectorAll('.danmaku-uid')
      ).map((element) => element.textContent?.trim())
    ).toEqual(['UID：42', 'UID：未记录']);
  });
});
