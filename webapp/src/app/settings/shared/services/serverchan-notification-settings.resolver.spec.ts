import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { ServerchanNotificationSettingsResolver } from './serverchan-notification-settings.resolver';
import { SettingService } from './setting.service';

describe('ServerchanNotificationSettingsResolverService', () => {
  let service: ServerchanNotificationSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ServerchanNotificationSettingsResolver,
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
    service = TestBed.inject(ServerchanNotificationSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
