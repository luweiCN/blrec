import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../../settings.module';
import { NotifierSettings } from '../../../../shared/setting.model';
import { SettingsSyncService } from '../../../../shared/services/settings-sync.service';
import { NotifierSettingsComponent } from './notifier-settings.component';

describe('NotifierSettingsComponent', () => {
  let component: NotifierSettingsComponent;
  let fixture: ComponentFixture<NotifierSettingsComponent>;

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
    fixture = TestBed.createComponent(NotifierSettingsComponent);
    component = fixture.componentInstance;
    component.settings = { enabled: false } satisfies NotifierSettings;
    component.keyOfSettings = 'emailNotification';
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
