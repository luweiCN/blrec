import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSwitchModule } from 'ng-zorro-antd/switch';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { SettingService } from '../shared/services/setting.service';
import { Settings } from '../shared/setting.model';
import { NotificationSettingsComponent } from './notification-settings.component';

describe('NotificationSettingsComponent', () => {
  let component: NotificationSettingsComponent;
  let fixture: ComponentFixture<NotificationSettingsComponent>;
  let settingService: jasmine.SpyObj<SettingService>;

  beforeEach(async () => {
    settingService = jasmine.createSpyObj<SettingService>('SettingService', [
      'changeSettings',
    ]);
    settingService.changeSettings.and.returnValue(
      of({ operationalNotifications: { routes: [] } })
    );
    await TestBed.configureTestingModule({
      imports: [
        FormsModule,
        NoopAnimationsModule,
        NzButtonModule,
        NzSelectModule,
        NzSwitchModule,
        NzTagModule,
      ],
      declarations: [NotificationSettingsComponent],
      providers: [
        { provide: SettingService, useValue: settingService },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
            'error',
          ]),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(NotificationSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      emailNotification: { enabled: true, srcAddr: 'a@b.com' },
      serverchanNotification: { enabled: false, sendkey: '' },
      pushdeerNotification: { enabled: false, pushkey: '' },
      pushplusNotification: { enabled: false, token: '' },
      telegramNotification: { enabled: false, token: '', chatid: '' },
      barkNotification: { enabled: false, pushkey: '' },
      operationalNotifications: {
        routes: [
          {
            event: 'account_unavailable',
            targets: [],
            notifyRecovery: true,
          },
        ],
      },
    } as unknown as Settings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('shows channel management and operational event routes', () => {
    const text = fixture.nativeElement.textContent;

    expect(text).toContain('通知渠道');
    expect(text).toContain('投稿账号不可用');
  });

  it('keeps the selected channel value stable between change detection passes', () => {
    const route = component.routes[0];

    expect(component.selectedChannels(route)).toBe(
      component.selectedChannels(route)
    );
  });

  it('saves selected channel and message format', () => {
    const route = component.routes[0];
    component.changeChannels(route, ['email']);
    route.targets[0].messageType = 'html';

    component.save();

    expect(settingService.changeSettings).toHaveBeenCalledWith({
      operationalNotifications: { routes: component.routes },
    });
  });
});
