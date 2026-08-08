import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../settings.module';
import { BiliApiSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { BiliApiSettingsComponent } from './bili-api-settings.component';

describe('BiliApiSettingsComponent', () => {
  let component: BiliApiSettingsComponent;
  let fixture: ComponentFixture<BiliApiSettingsComponent>;

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
    fixture = TestBed.createComponent(BiliApiSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      baseApiUrls: [],
      baseLiveApiUrls: [],
      basePlayInfoApiUrls: [],
    } satisfies BiliApiSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
