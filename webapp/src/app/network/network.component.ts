import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { forkJoin } from 'rxjs';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  NetworkRouteSettings,
  NetworkSettings,
} from 'src/app/settings/shared/setting.model';
import { SettingService } from 'src/app/settings/shared/services/setting.service';
import { NetworkInterface, NetworkPurpose } from './network.model';
import { NetworkService } from './network.service';

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
export class NetworkComponent implements OnInit {
  readonly purposes: PurposeRow[] = [
    {
      key: 'roomStatus',
      name: '房间状态轮询',
      help: '批量查询直播间是否开播。',
    },
    {
      key: 'danmaku',
      name: '弹幕 WebSocket',
      help: '连接直播间弹幕服务器。已有连接会在下次重连时切换。',
    },
    {
      key: 'recording',
      name: '录像下载',
      help: '下载直播视频流。正在录制的连接不会被强制中断。',
    },
    {
      key: 'upload',
      name: '视频上传',
      help: '上传录像文件和分 P 数据。',
    },
    {
      key: 'biliApi',
      name: '其他 B 站请求',
      help: '账号、投稿、审核、评论和房间详情等 API 请求。',
    },
  ];

  interfaces: NetworkInterface[] = [];
  settings: NetworkSettings | null = null;
  loading = false;
  saving = false;
  probing = false;

  constructor(
    private networkService: NetworkService,
    private settingService: SettingService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.load();
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
          this.interfaces = interfaces.interfaces;
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
    this.probing = true;
    this.networkService
      .probe(interfaceName)
      .pipe(
        finalize(() => {
          this.probing = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (response) => {
          this.interfaces = response.interfaces;
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

  private copySettings(settings: NetworkSettings): NetworkSettings {
    return JSON.parse(JSON.stringify(settings)) as NetworkSettings;
  }
}
