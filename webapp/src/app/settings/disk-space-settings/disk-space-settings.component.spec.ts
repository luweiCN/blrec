import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../settings.module';
import { SpaceSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { DiskSpaceSettingsComponent } from './disk-space-settings.component';
import { RecordingRetentionService } from './recording-retention.service';

describe('DiskSpaceSettingsComponent', () => {
  let component: DiskSpaceSettingsComponent;
  let fixture: ComponentFixture<DiskSpaceSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSyncService.syncSettings.and.returnValue(of());
    const recordingRetentionService = jasmine.createSpyObj<RecordingRetentionService>(
      'RecordingRetentionService',
      ['status']
    );
    recordingRetentionService.status.and.returnValue(
      of({
        managedVideoBytes: 480,
        capacityBytes: 500,
        remainingBytes: 20,
        warningThresholdBytes: 20,
        warning: true,
      })
    );

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: SettingsSyncService, useValue: settingsSyncService },
        {
          provide: RecordingRetentionService,
          useValue: recordingRetentionService,
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(DiskSpaceSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      checkInterval: 60,
      spaceThreshold: 1024 ** 3,
      recycleRecords: false,
      recordingCapacity: 0,
      capacityWarningThreshold: 1024 ** 3 * 20,
    } satisfies SpaceSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
