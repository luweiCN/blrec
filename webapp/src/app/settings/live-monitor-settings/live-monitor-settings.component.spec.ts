import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';

import { LiveMonitorSettings, LiveStatusMetrics } from '../shared/setting.model';
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
    mode: 'batch',
    intervalSeconds: 30,
    batchSize: 29,
    fallbackCooldownSeconds: 600,
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

    fixture = TestBed.createComponent(LiveMonitorSettingsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('shows that offline rooms use no websocket', () => {
    component.status = { state: 'ready', data: metricsFixture };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('活跃 WSS：0');
  });

  it('exposes only the bounded live monitor settings', () => {
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
    component.status = {
      state: 'ready',
      data: {
        ...metricsFixture,
        mode: 'legacy',
        breakerReason: 'manual pause',
      },
    };
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('旧模式会为离线房间维持连接');
    expect(text).toContain('上次成功：100');
    expect(text).toContain('快照最长年龄：12 秒');
    expect(text).toContain('熔断原因：manual pause');
  });

  it('resumes a paused coordinator', () => {
    component.status = {
      state: 'ready',
      data: { ...metricsFixture, breakerState: 'paused' },
    };
    fixture.detectChanges();

    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');
    button.click();

    expect(liveStatusService.resume).toHaveBeenCalledTimes(1);
  });
});
