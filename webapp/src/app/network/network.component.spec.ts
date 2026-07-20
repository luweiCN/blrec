import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';

import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  EVENT_SOURCE_FACTORY,
  EventSourceLike,
  RealtimeEvent,
  RealtimeService,
} from '../core/services/realtime.service';
import { UrlService } from '../core/services/url.service';
import { SettingService } from '../settings/shared/services/setting.service';
import {
  NetworkSettings,
  Settings,
} from '../settings/shared/setting.model';
import { NetworkComponent } from './network.component';
import { NetworkService } from './network.service';

class FakeRealtimeSource implements EventSourceLike {
  private readonly listeners = new Map<string, EventListener[]>();

  addEventListener(type: string, listener: EventListener): void {
    const values = this.listeners.get(type) ?? [];
    values.push(listener);
    this.listeners.set(type, values);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((value) => value !== listener)
    );
  }

  close(): void {}

  next(event: RealtimeEvent): void {
    const message = new MessageEvent(event.type, {
      data: JSON.stringify(event.data),
    });
    for (const listener of this.listeners.get(event.type) ?? []) {
      listener(message);
    }
  }
}

describe('NetworkComponent', () => {
  let fixture: ComponentFixture<NetworkComponent>;
  let networkService: jasmine.SpyObj<NetworkService>;
  let settingService: jasmine.SpyObj<SettingService>;
  let realtimeEvents: FakeRealtimeSource;

  const networkInterface = {
    name: 'eth0',
    address: '192.168.1.20',
    netmask: '255.255.255.0',
    gateway: '192.168.1.1',
    dnsServers: ['192.168.1.1'],
    kind: 'physical' as const,
    isUp: true,
    speedMbps: 1000,
    isDefault: true,
    enabled: true,
    uploadLimitBps: 0,
    uploadBps: 1024,
    downloadBps: 2048,
    uploadTotal: 4096,
    downloadTotal: 8192,
    probe: null,
  };

  beforeEach(async () => {
    const network: NetworkSettings = {
      interfaces: {},
      roomStatus: routeSettings(),
      danmaku: routeSettings(),
      recording: routeSettings(),
      upload: routeSettings(),
      biliApi: routeSettings(),
    };
    networkService = jasmine.createSpyObj<NetworkService>(
      'NetworkService',
      ['getInterfaces', 'probe', 'updateInterface']
    );
    networkService.getInterfaces.and.returnValue(
      of({ interfaces: [networkInterface] })
    );
    networkService.probe.and.returnValue(of({ interfaces: [networkInterface] }));
    networkService.updateInterface.and.returnValue(
      of({ interfaces: [networkInterface] })
    );
    settingService = jasmine.createSpyObj<SettingService>(
      'SettingService',
      ['getSettings', 'changeSettings']
    );
    settingService.getSettings.and.returnValue(of({ network } as Settings));
    settingService.changeSettings.and.returnValue(of({ network } as Settings));
    realtimeEvents = new FakeRealtimeSource();

    await TestBed.configureTestingModule({
      declarations: [NetworkComponent],
      providers: [
        { provide: NetworkService, useValue: networkService },
        { provide: SettingService, useValue: settingService },
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: () => realtimeEvents },
        { provide: Router, useValue: { url: '/network' } },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
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

  it('keeps horizontal scrolling on the table instead of the whole panel', () => {
    const panel = fixture.nativeElement.querySelector(
      '.network-panel'
    ) as HTMLElement;
    const table = fixture.nativeElement.querySelector('nz-table') as HTMLElement & {
      nzScroll?: { x?: string };
    };

    expect(getComputedStyle(panel).overflowX).toBe('visible');
    expect(table.nzScroll?.x).toBe('1040px');
  });

  it('saves interface enable state immediately without saving all routes', () => {
    fixture.componentInstance.setInterfaceEnabled(networkInterface, false);

    expect(networkService.updateInterface).toHaveBeenCalledOnceWith('eth0', {
      enabled: false,
    });
    expect(settingService.changeSettings).not.toHaveBeenCalled();
  });

  it('converts the row upload limit from MB/s before saving', () => {
    fixture.componentInstance.saveUploadLimit(networkInterface, 2);

    expect(networkService.updateInterface).toHaveBeenCalledOnceWith('eth0', {
      uploadLimitBps: 2 * 1024 * 1024,
    });
  });

  it('probes only the selected row', () => {
    fixture.componentInstance.probe('eth0');

    expect(networkService.probe).toHaveBeenCalledOnceWith('eth0');
  });

  it('applies realtime network metrics without another HTTP request', () => {
    realtimeEvents.next({
      type: 'network',
      data: {
        interfaces: [
          { ...networkInterface, uploadBps: 4096, downloadBps: 8192 },
        ],
      },
    });

    expect(fixture.componentInstance.interfaces[0].uploadBps).toBe(4096);
    expect(networkService.getInterfaces).toHaveBeenCalledTimes(1);
  });

  it('skips the bootstrap resync and reloads for the next resync', () => {
    expect(networkService.getInterfaces).toHaveBeenCalledTimes(1);
    expect(settingService.getSettings).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(networkService.getInterfaces).toHaveBeenCalledTimes(1);
    expect(settingService.getSettings).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(networkService.getInterfaces).toHaveBeenCalledTimes(2);
    expect(settingService.getSettings).toHaveBeenCalledTimes(2);
  });
});

function routeSettings() {
  return {
    mode: 'fixed' as const,
    interface: null,
    failoverEnabled: false,
  };
}
