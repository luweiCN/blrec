import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute } from '@angular/router';

import { of } from 'rxjs';
import { ArrowLeftOutline } from '@ant-design/icons-angular/icons';
import { NZ_ICONS } from 'ng-zorro-antd/icon';
import { NzMessageService } from 'ng-zorro-antd/message';

import { SettingsModule } from '../../settings.module';
import { ServerchanNotificationSettings } from '../../shared/setting.model';
import { SettingService } from '../../shared/services/setting.service';
import { SettingsSyncService } from '../../shared/services/settings-sync.service';
import { ServerchanNotificationSettingsComponent } from './serverchan-notification-settings.component';

describe('ServerchanNotificationSettingsComponent', () => {
  let component: ServerchanNotificationSettingsComponent;
  let fixture: ComponentFixture<ServerchanNotificationSettingsComponent>;

  beforeEach(async () => {
    const settings = {
      sendkey: 'send-key',
      enabled: false,
      notifyBegan: false,
      notifyEnded: false,
      notifyError: false,
      notifySpace: false,
      beganMessageType: 'markdown',
      beganMessageTitle: '',
      beganMessageContent: '',
      endedMessageType: 'markdown',
      endedMessageTitle: '',
      endedMessageContent: '',
      spaceMessageType: 'markdown',
      spaceMessageTitle: '',
      spaceMessageContent: '',
      errorMessageType: 'markdown',
      errorMessageTitle: '',
      errorMessageContent: '',
    } satisfies ServerchanNotificationSettings;
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
    fixture = TestBed.createComponent(ServerchanNotificationSettingsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
