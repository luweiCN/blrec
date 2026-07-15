import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { SettingService } from './setting.service';
import { TelegramNotificationSettingsResolver } from './telegram-notification-settings.resolver';

describe('TelegramNotificationSettingsResolverService', () => {
  let service: TelegramNotificationSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        TelegramNotificationSettingsResolver,
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
    service = TestBed.inject(TelegramNotificationSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
