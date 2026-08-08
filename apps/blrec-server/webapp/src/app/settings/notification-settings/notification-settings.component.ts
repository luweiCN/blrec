import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
} from '@angular/core';

import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  MessageType,
  OperationalNotificationChannel,
  OperationalNotificationEvent,
  OperationalNotificationRoute,
  Settings,
} from '../shared/setting.model';
import { SettingService } from '../shared/services/setting.service';

interface ChannelOption {
  readonly value: OperationalNotificationChannel;
  readonly label: string;
  readonly route: string;
}

@Component({
  selector: 'app-notification-settings',
  templateUrl: './notification-settings.component.html',
  styleUrls: ['./notification-settings.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class NotificationSettingsComponent implements OnChanges {
  @Input() settings!: Settings;

  routes: OperationalNotificationRoute[] = [];
  saving = false;

  readonly channelOptions: readonly ChannelOption[] = [
    { value: 'email', label: '邮箱', route: 'email-notification' },
    {
      value: 'serverchan',
      label: 'ServerChan',
      route: 'serverchan-notification',
    },
    { value: 'pushdeer', label: 'PushDeer', route: 'pushdeer-notification' },
    { value: 'pushplus', label: 'PushPlus', route: 'pushplus-notification' },
    { value: 'telegram', label: 'Telegram', route: 'telegram-notification' },
    { value: 'bark', label: 'Bark', route: 'bark-notification' },
  ];

  private readonly eventLabels: Record<OperationalNotificationEvent, string> = {
    account_unavailable: '投稿账号不可用',
    network_unavailable: '网络不可用',
    network_failover: '网络切换备用线路',
    recording_failed: '录制异常',
    upload_failed: '上传失败',
    review_rejected: '审核未通过',
    collection_failed: '加入合集失败',
    comment_failed: '自动评论失败',
    danmaku_failed: '弹幕回灌失败',
    transcode_repair_failed: '自动转码修复失败',
    capacity_warning: '录像容量预警',
  };
  private readonly selectedChannelsByRoute = new WeakMap<
    OperationalNotificationRoute,
    OperationalNotificationChannel[]
  >();

  constructor(
    private settingService: SettingService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef
  ) {}

  ngOnChanges(): void {
    this.routes = (this.settings?.operationalNotifications?.routes ?? []).map(
      (route) => ({
        ...route,
        targets: route.targets.map((target) => ({ ...target })),
      })
    );
    this.changeDetector.markForCheck();
  }

  eventLabel(event: OperationalNotificationEvent): string {
    return this.eventLabels[event];
  }

  selectedChannels(
    route: OperationalNotificationRoute
  ): OperationalNotificationChannel[] {
    const cached = this.selectedChannelsByRoute.get(route);
    if (cached) {
      return cached;
    }
    const channels = route.targets.map((target) => target.channel);
    this.selectedChannelsByRoute.set(route, channels);
    return channels;
  }

  changeChannels(
    route: OperationalNotificationRoute,
    channels: OperationalNotificationChannel[]
  ): void {
    this.selectedChannelsByRoute.set(route, channels);
    route.targets = channels.map((channel) => {
      const existing = route.targets.find(
        (target) => target.channel === channel
      );
      return (
        existing ?? {
          channel,
          messageType: this.messageTypes(channel)[0],
        }
      );
    });
    this.changeDetector.markForCheck();
  }

  channelLabel(channel: OperationalNotificationChannel): string {
    return (
      this.channelOptions.find((option) => option.value === channel)?.label ??
      channel
    );
  }

  channelReady(channel: OperationalNotificationChannel): boolean {
    const settings = this.settings;
    if (!settings) {
      return false;
    }
    switch (channel) {
      case 'email':
        return Boolean(
          settings.emailNotification.enabled &&
            settings.emailNotification.srcAddr &&
            settings.emailNotification.dstAddr &&
            settings.emailNotification.authCode
        );
      case 'serverchan':
        return Boolean(
          settings.serverchanNotification.enabled &&
            settings.serverchanNotification.sendkey
        );
      case 'pushdeer':
        return Boolean(
          settings.pushdeerNotification.enabled &&
            settings.pushdeerNotification.pushkey
        );
      case 'pushplus':
        return Boolean(
          settings.pushplusNotification.enabled &&
            settings.pushplusNotification.token
        );
      case 'telegram':
        return Boolean(
          settings.telegramNotification.enabled &&
            settings.telegramNotification.token &&
            settings.telegramNotification.chatid
        );
      case 'bark':
        return Boolean(
          settings.barkNotification.enabled && settings.barkNotification.pushkey
        );
    }
  }

  messageTypes(channel: OperationalNotificationChannel): MessageType[] {
    return {
      email: ['text', 'html'],
      serverchan: ['markdown'],
      pushdeer: ['text', 'markdown'],
      pushplus: ['text', 'markdown', 'html'],
      telegram: ['markdown', 'html'],
      bark: ['text', 'markdown'],
    }[channel] as MessageType[];
  }

  messageTypeLabel(messageType: MessageType): string {
    return { text: '纯文本', markdown: 'Markdown', html: 'HTML' }[messageType];
  }

  save(): void {
    if (this.saving) {
      return;
    }
    this.saving = true;
    this.settingService
      .changeSettings({
        operationalNotifications: { routes: this.routes },
      })
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: () => this.message.success('通知规则已保存'),
        error: (error: Error) =>
          this.message.error(`保存通知规则失败：${error.message}`),
      });
  }

  trackRoute(
    _index: number,
    route: OperationalNotificationRoute
  ): OperationalNotificationEvent {
    return route.event;
  }
}
