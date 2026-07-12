import { TestBed } from '@angular/core/testing';

import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { SettingService } from './setting.service';
import { SettingsResolver } from './settings.resolver';

describe('SettingsResolverService', () => {
  let service: SettingsResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        SettingsResolver,
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
    service = TestBed.inject(SettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
