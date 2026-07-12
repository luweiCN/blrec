import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { Observable, Subject, of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';

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
