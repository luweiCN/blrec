import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { EmailNotificationSettingsResolver } from './email-notification-settings.resolver';
import { SettingService } from './setting.service';

describe('EmailNotificationSettingsResolverService', () => {
  let service: EmailNotificationSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        EmailNotificationSettingsResolver,
        {
          provide: NGXLogger,
          useValue: jasmine.createSpyObj<NGXLogger>('NGXLogger', ['error']),
        },
        {
          provide: NzNotificationService,
          useValue: jasmine.createSpyObj<NzNotificationService>(
            'NzNotificationService',
            ['error']
          ),
        },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'getSettings',
          ]),
        },
      ],
    });
    service = TestBed.inject(EmailNotificationSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
