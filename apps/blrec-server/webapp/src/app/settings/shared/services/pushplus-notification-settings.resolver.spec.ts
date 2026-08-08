import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { PushplusNotificationSettingsResolver } from './pushplus-notification-settings.resolver';
import { SettingService } from './setting.service';

describe('PushplusNotificationSettingsResolverService', () => {
  let service: PushplusNotificationSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        PushplusNotificationSettingsResolver,
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
    service = TestBed.inject(PushplusNotificationSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
