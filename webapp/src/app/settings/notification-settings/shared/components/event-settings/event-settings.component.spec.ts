import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../../settings.module';
import { NotificationSettings } from '../../../../shared/setting.model';
import { SettingsSyncService } from '../../../../shared/services/settings-sync.service';
import { EventSettingsComponent } from './event-settings.component';

describe('EventSettingsComponent', () => {
  let component: EventSettingsComponent;
  let fixture: ComponentFixture<EventSettingsComponent>;

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
    fixture = TestBed.createComponent(EventSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      notifyBegan: false,
      notifyEnded: false,
      notifyError: false,
      notifySpace: false,
    } satisfies NotificationSettings;
    component.keyOfSettings = 'emailNotification';
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
