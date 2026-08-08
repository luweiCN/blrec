import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../settings.module';
import { CoverSaveStrategy, RecorderSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { RecorderSettingsComponent } from './recorder-settings.component';

describe('RecorderSettingsComponent', () => {
  let component: RecorderSettingsComponent;
  let fixture: ComponentFixture<RecorderSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings'],
    );
    settingsSyncService.syncSettings.and.returnValue(of());

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: SettingsSyncService, useValue: settingsSyncService },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(RecorderSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      streamFormat: 'flv',
      recordingMode: 'standard',
      qualityNumber: 10000,
      fmp4StreamTimeout: 60,
      readTimeout: 60,
      disconnectionTimeout: 60,
      bufferSize: 8192,
      saveCover: false,
      coverSaveStrategy: CoverSaveStrategy.DEFAULT,
      titleKeywords: [],
    } satisfies RecorderSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('edits multiple recording title keywords as tags', () => {
    component.titleKeywordsControl.setValue(['比赛', '高光']);

    expect(component.settingsForm.value.titleKeywords).toEqual([
      '比赛',
      '高光',
    ]);
    expect(fixture.nativeElement.textContent).toContain('录制标题关键词');
  });
});
