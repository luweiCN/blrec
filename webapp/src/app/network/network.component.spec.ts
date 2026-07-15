import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { SettingService } from '../settings/shared/services/setting.service';
import {
  NetworkSettings,
  Settings,
} from '../settings/shared/setting.model';
import { NetworkComponent } from './network.component';
import { NetworkService } from './network.service';

describe('NetworkComponent', () => {
  let fixture: ComponentFixture<NetworkComponent>;

  beforeEach(async () => {
    const network: NetworkSettings = {
      roomStatus: routeSettings(),
      danmaku: routeSettings(),
      recording: routeSettings(),
      upload: routeSettings(),
      biliApi: routeSettings(),
    };
    const networkService = jasmine.createSpyObj<NetworkService>(
      'NetworkService',
      ['getInterfaces', 'probe']
    );
    networkService.getInterfaces.and.returnValue(of({ interfaces: [] }));
    const settingService = jasmine.createSpyObj<SettingService>(
      'SettingService',
      ['getSettings', 'changeSettings']
    );
    settingService.getSettings.and.returnValue(of({ network } as Settings));

    await TestBed.configureTestingModule({
      declarations: [NetworkComponent],
      providers: [
        { provide: NetworkService, useValue: networkService },
        { provide: SettingService, useValue: settingService },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
            'error',
          ]),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(NetworkComponent);
    fixture.detectChanges();
  });

  it('uses one shared primary-page container', () => {
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1
    );
  });
});

function routeSettings() {
  return {
    primaryInterface: null,
    fallbackInterface: null,
    failoverEnabled: false,
  };
}
