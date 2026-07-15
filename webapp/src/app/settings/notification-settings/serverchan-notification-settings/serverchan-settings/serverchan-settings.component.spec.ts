import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../settings.module';
import { ServerchanSettings } from '../../../shared/setting.model';
import { SettingsSyncService } from '../../../shared/services/settings-sync.service';
import { ServerchanSettingsComponent } from './serverchan-settings.component';

describe('ServerchanSettingsComponent', () => {
  let component: ServerchanSettingsComponent;
  let fixture: ComponentFixture<ServerchanSettingsComponent>;

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
    fixture = TestBed.createComponent(ServerchanSettingsComponent);
    component = fixture.componentInstance;
    component.settings = { sendkey: 'send-key' } satisfies ServerchanSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
