import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../settings.module';
import { EmailSettings } from '../../../shared/setting.model';
import { SettingsSyncService } from '../../../shared/services/settings-sync.service';
import { EmailSettingsComponent } from './email-settings.component';

describe('EmailSettingsComponent', () => {
  let component: EmailSettingsComponent;
  let fixture: ComponentFixture<EmailSettingsComponent>;

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
    fixture = TestBed.createComponent(EmailSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      srcAddr: 'sender@example.com',
      dstAddr: 'receiver@example.com',
      authCode: 'auth-code',
      smtpHost: 'smtp.example.com',
      smtpPort: 465,
    } satisfies EmailSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
