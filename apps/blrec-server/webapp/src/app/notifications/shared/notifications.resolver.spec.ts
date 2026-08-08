import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  RouterStateSnapshot,
} from '@angular/router';

import { of } from 'rxjs';
import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { SettingService } from '../../settings/shared/services/setting.service';
import { Settings } from '../../settings/shared/setting.model';
import { NotificationsResolver } from './notifications.resolver';

describe('NotificationsResolver', () => {
  let resolver: NotificationsResolver;
  let settingService: jasmine.SpyObj<SettingService>;

  beforeEach(() => {
    settingService = jasmine.createSpyObj<SettingService>('SettingService', [
      'getSettings',
    ]);
    settingService.getSettings.and.returnValue(of({} as Settings));
    TestBed.configureTestingModule({
      providers: [
        NotificationsResolver,
        { provide: SettingService, useValue: settingService },
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
      ],
    });
    resolver = TestBed.inject(NotificationsResolver);
  });

  it('loads only channel and operational notification settings', () => {
    resolver
      .resolve(
        {} as ActivatedRouteSnapshot,
        {} as RouterStateSnapshot
      )
      .subscribe();

    expect(settingService.getSettings).toHaveBeenCalledOnceWith([
      'emailNotification',
      'serverchanNotification',
      'pushdeerNotification',
      'pushplusNotification',
      'telegramNotification',
      'barkNotification',
      'operationalNotifications',
    ]);
  });
});
