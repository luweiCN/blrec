import { TestBed } from '@angular/core/testing';

import { NzMessageService } from 'ng-zorro-antd/message';

import { SettingService } from './setting.service';
import { SettingsSyncService } from './settings-sync.service';

describe('SettingsSyncService', () => {
  let service: SettingsSyncService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'error',
          ]),
        },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'changeSettings',
          ]),
        },
      ],
    });
    service = TestBed.inject(SettingsSyncService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
