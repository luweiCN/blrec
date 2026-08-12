import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { forkJoin, Subscription } from 'rxjs';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  NetworkRouteSettings,
  NetworkSettings,
} from 'src/app/settings/shared/setting.model';
import { SettingService } from 'src/app/settings/shared/services/setting.service';
import { NetworkInterface, NetworkPurpose } from './network.model';
import { NetworkService } from './network.service';
import { RealtimeService } from '../core/services/realtime.service';

interface PurposeRow {
  key: NetworkPurpose;
  name: string;
  help: string;
}

@Component({
  selector: 'app-network',
  templateUrl: './network.component.html',
  styleUrls: ['./network.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class NetworkComponent implements OnInit, OnDestroy {
  readonly purposes: PurposeRow[] = [
    {
      key: 'roomStatus',
      name: '房间状态轮询',
      help: '批量查询直播间是否开播。',
    },
    {
      key: 'danmaku',
      name: '弹幕 WebSocket',
      help: '建立弹幕连接时选线路；同一连接和普通重连保持不变。',
    },
    {
      key: 'recording',
      name: '直播流录制',
      help: '下载正在直播的直播流；每场直播只选一次线路，整场及分段保持不变。',
    },
    {
      key: 'upload',
      name: '视频上传',
      help: '固定使用所选线路；线路故障时暂停，不会自动换出口。',
    },
    {
      key: 'biliApi',
      name: '其他 B 站请求',
      help: '账号、投稿、审核、评论和回灌固定使用所选线路。',
    },
    {
      key: 'archiveDownload',
      name: '历史稿件下载',
      help: '固定线路使用 3 路下载；多线路并行时，每条开启“历史下载”的线路固定运行 3 路，可单独暂停。',
    },
    {
      key: 'dashboardPublish',
      name: '排行榜数据发布',
      help: '每天生成公开排行榜 JSON 并上传到 OSS；整次发布固定使用同一线路，线路故障时停止并稍后重试。',
    },
    {
      key: 'cloudCost',
      name: '云成本查询',
      help: '使用只读凭据查询阿里云账单与 CDN、OSS 用量；固定使用所选线路，线路故障时停止查询。',
    },
    {
      key: 'visitorAnalytics',
      name: '访客日志查询',
      help: '使用只读凭据查询阿里云日志服务中的站点访问数据；固定使用所选线路，线路故障时停止查询。',
    },
  ];

  interfaces: NetworkInterface[] = [];
  settings: NetworkSettings | null = null;
  loading = false;
  saving = false;
  probingAll = false;
  probingInterface: string | null = null;
  readonly savingInterfaces = new Set<string>();
  readonly uploadLimitDraft: Record<string, number> = {};
  private realtimeSubscription?: Subscription;

  constructor(
    private networkService: NetworkService,
    private settingService: SettingService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef,
    private realtime: RealtimeService,
  ) {}

  ngOnInit(): void {
    this.load();
    this.realtimeSubscription = this.realtime.events$.subscribe((event) => {
      if (event.type === 'resync') {
        this.load();
        return;
      }
      if (event.type !== 'network') {
        return;
      }
      const response = this.interfaceResponse(event.data);
      if (response !== null) {
        this.applyInterfaces(response.interfaces, false);
      }
    });
  }

  ngOnDestroy(): void {
    this.realtimeSubscription?.unsubscribe();
  }

  load(): void {
    this.loading = true;
    forkJoin({
      interfaces: this.networkService.getInterfaces(),
      settings: this.settingService.getSettings(['network']),
    })
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: ({ interfaces, settings }) => {
          this.applyInterfaces(interfaces.interfaces);
          this.settings = this.copySettings(settings.network);
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.message.error('网络信息加载失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  probe(interfaceName: string | null = null): void {
    if (interfaceName === null) {
      this.probingAll = true;
    } else {
      this.probingInterface = interfaceName;
    }
    this.networkService
      .probe(interfaceName)
      .pipe(
        finalize(() => {
          if (interfaceName === null) {
            this.probingAll = false;
          } else if (this.probingInterface === interfaceName) {
            this.probingInterface = null;
          }
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (response) => {
          this.applyInterfaces(response.interfaces);
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.message.error('网络检测失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  save(): void {
    if (this.settings === null) {
      return;
    }
    this.saving = true;
    this.settingService
      .changeSettings({ network: this.settings })
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => this.message.success('网络分工已保存'),
        error: () => this.message.error('网络设置保存失败'),
      });
  }

  route(key: NetworkPurpose): NetworkRouteSettings {
    if (this.settings === null) {
      throw new Error('network settings are not loaded');
    }
    return this.settings[key];
  }

  interfaceLabel(name: string | null): string {
    if (name === null) {
      return '系统默认';
    }
    const item = this.interfaces.find((candidate) => candidate.name === name);
    return item ? `${item.name} · ${item.address}` : `${name}（不可用）`;
  }

  setInterfaceEnabled(item: NetworkInterface, enabled: boolean): void {
    this.updateInterface(item, { enabled });
  }

  setArchiveDownloadEnabled(
    item: NetworkInterface,
    enabled: boolean,
  ): void {
    this.updateInterface(item, { archiveDownloadEnabled: enabled });
  }

  saveUploadLimit(item: NetworkInterface, megabytesPerSecond: number): void {
    const normalized = Number.isFinite(megabytesPerSecond)
      ? Math.max(0, megabytesPerSecond)
      : 0;
    this.updateInterface(item, {
      uploadLimitBps: Math.round(normalized * 1024 * 1024),
    });
  }

  isInterfaceSaving(name: string): boolean {
    return this.savingInterfaces.has(name);
  }

  supportsRoundRobin(purpose: NetworkPurpose): boolean {
    return ![
      'upload',
      'biliApi',
      'archiveDownload',
      'dashboardPublish',
      'cloudCost',
      'visitorAnalytics',
    ].includes(purpose);
  }

  supportsParallel(purpose: NetworkPurpose): boolean {
    return purpose === 'archiveDownload';
  }

  formatRate(bytesPerSecond: number): string {
    return `${this.formatBytes(bytesPerSecond)}/s`;
  }

  formatBytes(bytes: number): string {
    if (bytes < 1024) {
      return `${Math.round(bytes)} B`;
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[index]}`;
  }

  private updateInterface(
    item: NetworkInterface,
    update: {
      enabled?: boolean;
      archiveDownloadEnabled?: boolean;
      uploadLimitBps?: number;
    },
  ): void {
    if (this.savingInterfaces.has(item.name)) {
      return;
    }
    this.savingInterfaces.add(item.name);
    this.networkService
      .updateInterface(item.name, update)
      .pipe(
        finalize(() => {
          this.savingInterfaces.delete(item.name);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (response) => {
          this.applyInterfaces(response.interfaces);
          this.message.success('网卡设置已生效');
        },
        error: () => this.message.error('网卡设置保存失败'),
      });
  }

  private applyInterfaces(
    interfaces: NetworkInterface[],
    updateDraft = true,
  ): void {
    this.interfaces = interfaces;
    if (updateDraft) {
      for (const item of interfaces) {
        this.uploadLimitDraft[item.name] =
          item.uploadLimitBps / (1024 * 1024);
      }
    }
    if (this.settings !== null) {
      for (const item of interfaces) {
        this.settings.interfaces[item.name] = {
          enabled: item.enabled,
          archiveDownloadEnabled: item.archiveDownloadEnabled,
          uploadLimitBps: item.uploadLimitBps,
        };
      }
    }
    this.changeDetector.markForCheck();
  }

  private interfaceResponse(data: unknown): { interfaces: NetworkInterface[] } | null {
    if (typeof data !== 'object' || data === null || !('interfaces' in data)) {
      return null;
    }
    const interfaces = (data as { interfaces: unknown }).interfaces;
    return Array.isArray(interfaces)
      ? { interfaces: interfaces as NetworkInterface[] }
      : null;
  }

  private copySettings(settings: NetworkSettings): NetworkSettings {
    const copied = JSON.parse(JSON.stringify(settings)) as NetworkSettings;
    copied.upload.failoverEnabled = false;
    copied.archiveDownload.failoverEnabled = false;
    copied.dashboardPublish.failoverEnabled = false;
    copied.cloudCost.failoverEnabled = false;
    copied.visitorAnalytics.failoverEnabled = false;
    return copied;
  }
}
