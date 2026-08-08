import { TestBed } from '@angular/core/testing';
import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { AppService } from 'src/app/core/services/app.service';
import { AppInfoResolver } from './app-info.resolver';

describe('AppInfoResolver', () => {
  let resolver: AppInfoResolver;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        AppInfoResolver,
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
          provide: AppService,
          useValue: jasmine.createSpyObj<AppService>('AppService', [
            'getAppInfo',
          ]),
        },
      ],
    });
    resolver = TestBed.inject(AppInfoResolver);
  });

  it('should be created', () => {
    expect(resolver).toBeTruthy();
  });
});
