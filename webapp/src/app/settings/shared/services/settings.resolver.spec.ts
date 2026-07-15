import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  RouterStateSnapshot,
} from '@angular/router';

import { of } from 'rxjs';
import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { SettingService } from './setting.service';
import { SettingsResolver } from './settings.resolver';
import { Settings } from '../setting.model';

describe('SettingsResolverService', () => {
  let service: SettingsResolver;
  let settingService: jasmine.SpyObj<SettingService>;

  beforeEach(() => {
    settingService = jasmine.createSpyObj<SettingService>('SettingService', [
      'getSettings',
    ]);
    settingService.getSettings.and.returnValue(of({} as Settings));
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
          useValue: settingService,
        },
      ],
    });
    service = TestBed.inject(SettingsResolver);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('loads only system settings', () => {
    service
      .resolve(
        {} as ActivatedRouteSnapshot,
        {} as RouterStateSnapshot
      )
      .subscribe();

    expect(settingService.getSettings).toHaveBeenCalledOnceWith([
      'output',
      'logging',
      'biliApi',
      'header',
      'danmaku',
      'recorder',
      'postprocessing',
      'space',
    ]);
  });
});
