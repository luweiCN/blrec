import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute } from '@angular/router';

import { of } from 'rxjs';
import { ArrowLeftOutline } from '@ant-design/icons-angular/icons';
import { NZ_ICONS } from 'ng-zorro-antd/icon';
import { NzMessageService } from 'ng-zorro-antd/message';

import { SettingsModule } from '../../settings.module';
import { BarkNotificationSettings } from '../../shared/setting.model';
import { SettingService } from '../../shared/services/setting.service';
import { SettingsSyncService } from '../../shared/services/settings-sync.service';
import { BarkNotificationSettingsComponent } from './bark-notification-settings.component';

describe('BarkNotificationSettingsComponent', () => {
  let component: BarkNotificationSettingsComponent;
  let fixture: ComponentFixture<BarkNotificationSettingsComponent>;

  beforeEach(async () => {
    const settings = {
      server: 'https://example.com',
      pushkey: 'push-key',
      enabled: false,
      notifyBegan: false,
      notifyEnded: false,
      notifyError: false,
      notifySpace: false,
      beganMessageType: 'text',
      beganMessageTitle: '',
      beganMessageContent: '',
      endedMessageType: 'text',
      endedMessageTitle: '',
      endedMessageContent: '',
      spaceMessageType: 'text',
      spaceMessageTitle: '',
      spaceMessageContent: '',
      errorMessageType: 'text',
      errorMessageTitle: '',
      errorMessageContent: '',
    } satisfies BarkNotificationSettings;
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSyncService.syncSettings.and.returnValue(of());

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: NZ_ICONS, useValue: [ArrowLeftOutline] },
        { provide: ActivatedRoute, useValue: { data: of({ settings }) } },
        { provide: SettingsSyncService, useValue: settingsSyncService },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'changeSettings',
          ]),
        },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
            'error',
          ]),
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(BarkNotificationSettingsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
