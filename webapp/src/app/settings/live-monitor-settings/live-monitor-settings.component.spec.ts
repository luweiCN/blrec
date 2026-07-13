import { CommonModule } from '@angular/common';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import {
  ComponentFixture,
  TestBed,
  fakeAsync,
  tick,
} from '@angular/core/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { Observable, Subject, of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzFormLabelComponent, NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';

import { UrlService } from 'src/app/core/services/url.service';
import {
  LiveMonitorSettings,
  LiveStatusMetrics,
  Settings,
} from '../shared/setting.model';
import { LiveStatusService } from '../shared/services/live-status.service';
import { SettingService } from '../shared/services/setting.service';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { LiveMonitorSettingsComponent } from './live-monitor-settings.component';

describe('LiveMonitorSettingsComponent', () => {
  let component: LiveMonitorSettingsComponent;
  let fixture: ComponentFixture<LiveMonitorSettingsComponent>;
  let liveStatusService: jasmine.SpyObj<LiveStatusService>;
  let settingService: jasmine.SpyObj<SettingService>;
  let settingsSyncService: jasmine.SpyObj<SettingsSyncService>;

  const settingsFixture: LiveMonitorSettings = {
    mode: 'legacy',
    intervalSeconds: 45,
    batchSize: 11,
    fallbackCooldownSeconds: 1200,
  };

  const metricsFixture: LiveStatusMetrics = {
    mode: 'batch',
    intervalSeconds: 30,
    batchSize: 29,
    registeredRooms: 58,
    activeWebsockets: 0,
    lastSuccessAt: 100,
    snapshotMaxAgeSeconds: 12,
    missingResults: 0,
    fallbackRequests: 0,
    breakerState: 'closed',
    breakerReason: null,
  };

  beforeEach(async () => {
    liveStatusService = jasmine.createSpyObj<LiveStatusService>(
      'LiveStatusService',
      ['getMetrics', 'resume']
    );
    settingService = jasmine.createSpyObj<SettingService>('SettingService', [
      'getSettings',
    ]);
    settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    liveStatusService.getMetrics.and.returnValue(of(metricsFixture));
    liveStatusService.resume.and.returnValue(of(void 0));
    settingService.getSettings.and.returnValue(
      of({ liveMonitor: settingsFixture }) as ReturnType<
        SettingService['getSettings']
      >
    );
    settingsSyncService.syncSettings.and.returnValue(of());

    await TestBed.configureTestingModule({
      declarations: [LiveMonitorSettingsComponent],
      imports: [
        CommonModule,
        ReactiveFormsModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzCardModule,
        NzFormModule,
        NzInputModule,
        NzSelectModule,
        NzSpinModule,
      ],
      providers: [
        { provide: LiveStatusService, useValue: liveStatusService },
        { provide: SettingService, useValue: settingService },
        { provide: SettingsSyncService, useValue: settingsSyncService },
      ],
    }).compileComponents();
  });

  function createComponent(): void {
    fixture = TestBed.createComponent(LiveMonitorSettingsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  it('loads settings before enabling the form and wires their values to sync', () => {
    const settingsResponse = new Subject<Settings>();
    settingService.getSettings.and.returnValue(settingsResponse);

    createComponent();

    expect(component.settingsLoad.state).toBe('loading');
    expect(component.settingsForm.disabled).toBeTrue();
    expect(settingsSyncService.syncSettings).not.toHaveBeenCalled();

    settingsResponse.next({ liveMonitor: settingsFixture } as Settings);
    fixture.detectChanges();

    expect(component.settingsLoad.state).toBe('ready');
    expect(component.settingsForm.enabled).toBeTrue();
    expect(component.settingsForm.getRawValue()).toEqual(settingsFixture);
    expect(settingsSyncService.syncSettings).toHaveBeenCalledTimes(1);

    const [key, initialValue, valueChanges] =
      settingsSyncService.syncSettings.calls.mostRecent().args;
    expect(key).toBe('liveMonitor');
    expect(initialValue).toEqual(settingsFixture);

    let emittedValue: LiveMonitorSettings | undefined;
    const valueChangesSubscription = (
      valueChanges as Observable<LiveMonitorSettings>
    ).subscribe((value) => (emittedValue = value));
    component.settingsForm.controls['batchSize'].setValue(12);

    expect(emittedValue).toEqual({ ...settingsFixture, batchSize: 12 });
    valueChangesSubscription.unsubscribe();
  });

  it('keeps the form disabled after a settings error and recovers on retry', () => {
    settingService.getSettings.and.returnValues(
      throwError(() => new Error('设置加载失败')),
      of({ liveMonitor: settingsFixture }) as ReturnType<
        SettingService['getSettings']
      >
    );

    createComponent();

    expect(component.settingsLoad).toEqual({
      state: 'error',
      message: '设置加载失败',
    });
    expect(component.settingsForm.disabled).toBeTrue();
    expect(fixture.nativeElement.textContent).toContain('设置加载失败');

    const retryButton: HTMLButtonElement = fixture.nativeElement.querySelector(
      '[data-testid="retry-settings"]'
    );
    retryButton.click();
    fixture.detectChanges();

    expect(settingService.getSettings).toHaveBeenCalledTimes(2);
    expect(component.settingsLoad.state).toBe('ready');
    expect(component.settingsForm.enabled).toBeTrue();
    expect(component.settingsForm.getRawValue()).toEqual(settingsFixture);
  });

  it('shows that offline rooms use no websocket', () => {
    createComponent();
    component.status = { state: 'ready', data: metricsFixture };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('活跃 WSS：0');
  });

  it('exposes only the bounded live monitor settings', () => {
    createComponent();
    expect(Object.keys(component.settingsForm.controls)).toEqual([
      'mode',
      'intervalSeconds',
      'batchSize',
      'fallbackCooldownSeconds',
    ]);

    component.settingsForm.controls['intervalSeconds'].setValue(29);
    component.settingsForm.controls['batchSize'].setValue(30);
    component.settingsForm.controls['fallbackCooldownSeconds'].setValue(599);

    expect(component.settingsForm.invalid).toBeTrue();
    expect(fixture.nativeElement.querySelector('[formControlName="cookie"]')).toBeNull();
  });

  it('explains every live monitor option with the standard form tooltip', () => {
    createComponent();

    const tooltips = fixture.debugElement
      .queryAll(By.directive(NzFormLabelComponent))
      .map(
        (label) => label.injector.get(NzFormLabelComponent).nzTooltipTitle
      );

    expect(tooltips).toEqual([
      '批量模式会合并查询多个房间的直播状态，只为确认开播的房间建立弹幕连接，可减少请求次数和空闲连接。旧模式会分别监控每个房间，仅用于兼容或紧急回退，通常产生更多请求和连接。切换模式会重启应用，录制期间不能切换。',
      '系统每隔指定秒数查询一次所有已登记房间的直播状态。数值越小，发现开播越快，但请求更频繁；数值越大，请求更少，但发现开播可能更慢。允许设置为 30–60 秒。',
      '一次批量状态请求包含的最大房间数。房间总数超过该值时会拆成多次请求；数值越小，每轮产生的请求越多。允许设置为 1–29；例如 58 个房间设置为 29 时，正常每轮需要 2 次批量请求。',
      '当批量结果缺少原本已确认开播的房间时，系统会使用单房间匿名请求再次确认，避免误判下播。该值表示同一房间两次兜底确认的最短间隔；数值越大，请求越少，但异常状态恢复可能更慢。允许设置为 600–3600 秒。',
    ]);
  });

  it('shows legacy risk and the read-only health details', () => {
    liveStatusService.getMetrics.and.returnValue(
      of({
        ...metricsFixture,
        mode: 'legacy',
        breakerReason: 'manual pause',
      })
    );
    createComponent();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('旧模式会为离线房间维持连接');
    expect(text).toContain('上次成功：100');
    expect(text).toContain('快照最长年龄：12 秒');
    expect(text).toContain('熔断原因：manual pause');
  });

  it('refreshes metrics after resuming a paused coordinator', () => {
    const pausedMetrics: LiveStatusMetrics = {
      ...metricsFixture,
      breakerState: 'paused',
    };
    const resumedMetrics: LiveStatusMetrics = {
      ...metricsFixture,
      activeWebsockets: 1,
      breakerState: 'closed',
    };
    liveStatusService.getMetrics.and.returnValues(
      of(pausedMetrics),
      of(resumedMetrics)
    );

    createComponent();
    fixture.detectChanges();

    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');
    button.click();

    expect(liveStatusService.resume).toHaveBeenCalledTimes(1);
    expect(liveStatusService.getMetrics).toHaveBeenCalledTimes(2);
    expect(component.status).toEqual({ state: 'ready', data: resumedMetrics });
  });

  it('stops settings, status, resume, sync, and form subscriptions on destroy', () => {
    let settingsStopped = false;
    let statusStopped = false;
    let resumeStopped = false;
    let syncStopped = false;
    let valueChangesCompleted = false;

    settingService.getSettings.and.returnValue(
      new Observable<Settings>((observer) => {
        observer.next({ liveMonitor: settingsFixture } as Settings);
        return () => (settingsStopped = true);
      })
    );
    liveStatusService.getMetrics.and.returnValue(
      new Observable<LiveStatusMetrics>(() => () => (statusStopped = true))
    );
    liveStatusService.resume.and.returnValue(
      new Observable<void>(() => () => (resumeStopped = true))
    );
    settingsSyncService.syncSettings.and.returnValue(
      new Observable<never>(() => () => (syncStopped = true))
    );

    createComponent();
    const valueChanges = settingsSyncService.syncSettings.calls.mostRecent()
      .args[2] as Observable<LiveMonitorSettings>;
    valueChanges.subscribe({
      complete: () => (valueChangesCompleted = true),
    });
    component.status = {
      state: 'ready',
      data: { ...metricsFixture, breakerState: 'paused' },
    };
    component.resume();

    fixture.destroy();

    expect(settingsStopped).toBeTrue();
    expect(statusStopped).toBeTrue();
    expect(resumeStopped).toBeTrue();
    expect(syncStopped).toBeTrue();
    expect(valueChangesCompleted).toBeTrue();
  });
});

describe('LiveMonitorSettingsComponent persistence', () => {
  let component: LiveMonitorSettingsComponent;
  let fixture: ComponentFixture<LiveMonitorSettingsComponent>;
  let http: HttpTestingController;

  const settingsFixture: LiveMonitorSettings = {
    mode: 'legacy',
    intervalSeconds: 45,
    batchSize: 11,
    fallbackCooldownSeconds: 1200,
  };

  const metricsFixture: LiveStatusMetrics = {
    mode: 'legacy',
    intervalSeconds: 45,
    batchSize: 11,
    registeredRooms: 58,
    activeWebsockets: 0,
    lastSuccessAt: 100,
    snapshotMaxAgeSeconds: 12,
    missingResults: 0,
    fallbackRequests: 0,
    breakerState: 'closed',
    breakerReason: null,
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [LiveMonitorSettingsComponent],
      imports: [
        CommonModule,
        HttpClientTestingModule,
        ReactiveFormsModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzCardModule,
        NzFormModule,
        NzInputModule,
        NzSelectModule,
        NzSpinModule,
      ],
      providers: [
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
        {
          provide: NzMessageService,
          useValue: { error: jasmine.createSpy('error') },
        },
      ],
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(LiveMonitorSettingsComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => http.verify());

  it('persists a non-mode field as the exact nested PATCH diff', fakeAsync(() => {
    fixture.detectChanges();

    const settingsRequest = http.expectOne(
      (request) => request.url === '/api/v1/settings'
    );
    expect(settingsRequest.request.method).toBe('GET');
    expect(settingsRequest.request.params.getAll('include')).toEqual([
      'liveMonitor',
    ]);
    settingsRequest.flush({ liveMonitor: settingsFixture });

    const statusRequest = http.expectOne('/api/v1/live-status');
    expect(statusRequest.request.method).toBe('GET');
    statusRequest.flush(metricsFixture);
    tick();
    fixture.detectChanges();

    expect(component.settingsLoad.state).toBe('ready');
    expect(component.settingsForm.getRawValue()).toEqual(settingsFixture);

    component.settingsForm.controls['batchSize'].setValue(12);
    tick();

    const patchRequest = http.expectOne('/api/v1/settings');
    expect(patchRequest.request.method).toBe('PATCH');
    expect(patchRequest.request.body).toEqual({
      liveMonitor: { batchSize: 12 },
    });
    patchRequest.flush({
      liveMonitor: { ...settingsFixture, batchSize: 12 },
    });
    tick();

    expect(component.settingsForm.getRawValue()).toEqual({
      ...settingsFixture,
      batchSize: 12,
    });
    http.expectNone((request) => request.method === 'PATCH');
  }));
});
