import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { PushdeerNotificationSettingsResolver } from './pushdeer-notification-settings.resolver';
import { SettingService } from './setting.service';

describe('PushdeerNotificationSettingsResolver', () => {
  let service: PushdeerNotificationSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        PushdeerNotificationSettingsResolver,
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
    service = TestBed.inject(PushdeerNotificationSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
