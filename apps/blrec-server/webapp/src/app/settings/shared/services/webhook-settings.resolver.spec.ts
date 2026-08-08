import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { SettingService } from './setting.service';
import { WebhookSettingsResolver } from './webhook-settings.resolver';

describe('WebhookSettingsResolverService', () => {
  let service: WebhookSettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        WebhookSettingsResolver,
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
    service = TestBed.inject(WebhookSettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
