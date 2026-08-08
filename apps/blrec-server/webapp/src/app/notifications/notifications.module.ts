import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule, ReactiveFormsModule } from '@angular/forms';

import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSwitchModule } from 'ng-zorro-antd/switch';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { SharedModule } from '../shared/shared.module';
import { BarkNotificationSettingsComponent } from '../settings/notification-settings/bark-notification-settings/bark-notification-settings.component';
import { BarkSettingsComponent } from '../settings/notification-settings/bark-notification-settings/bark-settings/bark-settings.component';
import { EmailNotificationSettingsComponent } from '../settings/notification-settings/email-notification-settings/email-notification-settings.component';
import { EmailSettingsComponent } from '../settings/notification-settings/email-notification-settings/email-settings/email-settings.component';
import { NotificationSettingsComponent } from '../settings/notification-settings/notification-settings.component';
import { PushdeerNotificationSettingsComponent } from '../settings/notification-settings/pushdeer-notification-settings/pushdeer-notification-settings.component';
import { PushdeerSettingsComponent } from '../settings/notification-settings/pushdeer-notification-settings/pushdeer-settings/pushdeer-settings.component';
import { PushplusNotificationSettingsComponent } from '../settings/notification-settings/pushplus-notification-settings/pushplus-notification-settings.component';
import { PushplusSettingsComponent } from '../settings/notification-settings/pushplus-notification-settings/pushplus-settings/pushplus-settings.component';
import { ServerchanNotificationSettingsComponent } from '../settings/notification-settings/serverchan-notification-settings/serverchan-notification-settings.component';
import { ServerchanSettingsComponent } from '../settings/notification-settings/serverchan-notification-settings/serverchan-settings/serverchan-settings.component';
import { EventSettingsComponent } from '../settings/notification-settings/shared/components/event-settings/event-settings.component';
import { MessageTemplateEditDialogComponent } from '../settings/notification-settings/shared/components/message-template-settings/message-template-edit-dialog/message-template-edit-dialog.component';
import { MessageTemplateSettingsComponent } from '../settings/notification-settings/shared/components/message-template-settings/message-template-settings.component';
import { NotifierSettingsComponent } from '../settings/notification-settings/shared/components/notifier-settings/notifier-settings.component';
import { TelegramNotificationSettingsComponent } from '../settings/notification-settings/telegram-notification-settings/telegram-notification-settings.component';
import { TelegramSettingsComponent } from '../settings/notification-settings/telegram-notification-settings/telegram-settings/telegram-settings.component';
import { BarkNotificationSettingsResolver } from '../settings/shared/services/bark-notification-settings.resolver';
import { EmailNotificationSettingsResolver } from '../settings/shared/services/email-notification-settings.resolver';
import { PushdeerNotificationSettingsResolver } from '../settings/shared/services/pushdeer-notification-settings.resolver';
import { PushplusNotificationSettingsResolver } from '../settings/shared/services/pushplus-notification-settings.resolver';
import { ServerchanNotificationSettingsResolver } from '../settings/shared/services/serverchan-notification-settings.resolver';
import { TelegramNotificationSettingsResolver } from '../settings/shared/services/telegram-notification-settings.resolver';
import { SettingsSharedModule } from '../settings/shared/settings-shared.module';
import { NotificationsRoutingModule } from './notifications-routing.module';
import { NotificationsComponent } from './notifications.component';
import { NotificationsResolver } from './shared/notifications.resolver';

@NgModule({
  declarations: [
    NotificationsComponent,
    NotificationSettingsComponent,
    EventSettingsComponent,
    EmailNotificationSettingsComponent,
    EmailSettingsComponent,
    ServerchanNotificationSettingsComponent,
    ServerchanSettingsComponent,
    PushdeerNotificationSettingsComponent,
    PushdeerSettingsComponent,
    PushplusNotificationSettingsComponent,
    PushplusSettingsComponent,
    TelegramNotificationSettingsComponent,
    TelegramSettingsComponent,
    BarkNotificationSettingsComponent,
    BarkSettingsComponent,
    NotifierSettingsComponent,
    MessageTemplateSettingsComponent,
    MessageTemplateEditDialogComponent,
  ],
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    NotificationsRoutingModule,
    SharedModule,
    SettingsSharedModule,
    NzButtonModule,
    NzFormModule,
    NzIconModule,
    NzInputModule,
    NzModalModule,
    NzPageHeaderModule,
    NzSelectModule,
    NzSwitchModule,
    NzTagModule,
    NzToolTipModule,
  ],
  providers: [
    NotificationsResolver,
    EmailNotificationSettingsResolver,
    ServerchanNotificationSettingsResolver,
    PushdeerNotificationSettingsResolver,
    PushplusNotificationSettingsResolver,
    TelegramNotificationSettingsResolver,
    BarkNotificationSettingsResolver,
  ],
})
export class NotificationsModule {}
