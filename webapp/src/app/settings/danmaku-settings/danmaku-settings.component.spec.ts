import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../settings.module';
import { DanmakuSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { DanmakuSettingsComponent } from './danmaku-settings.component';

describe('DanmakuSettingsComponent', () => {
  let component: DanmakuSettingsComponent;
  let fixture: ComponentFixture<DanmakuSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
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
    fixture = TestBed.createComponent(DanmakuSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      danmuUname: false,
      recordGiftSend: false,
      recordFreeGifts: false,
      recordGuardBuy: false,
      recordSuperChat: false,
      saveRawDanmaku: false,
    } satisfies DanmakuSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
