import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { BarkNotificationSettingsComponent } from '../settings/notification-settings/bark-notification-settings/bark-notification-settings.component';
import { EmailNotificationSettingsComponent } from '../settings/notification-settings/email-notification-settings/email-notification-settings.component';
import { PushdeerNotificationSettingsComponent } from '../settings/notification-settings/pushdeer-notification-settings/pushdeer-notification-settings.component';
import { PushplusNotificationSettingsComponent } from '../settings/notification-settings/pushplus-notification-settings/pushplus-notification-settings.component';
import { ServerchanNotificationSettingsComponent } from '../settings/notification-settings/serverchan-notification-settings/serverchan-notification-settings.component';
import { TelegramNotificationSettingsComponent } from '../settings/notification-settings/telegram-notification-settings/telegram-notification-settings.component';
import { BarkNotificationSettingsResolver } from '../settings/shared/services/bark-notification-settings.resolver';
import { EmailNotificationSettingsResolver } from '../settings/shared/services/email-notification-settings.resolver';
import { PushdeerNotificationSettingsResolver } from '../settings/shared/services/pushdeer-notification-settings.resolver';
import { PushplusNotificationSettingsResolver } from '../settings/shared/services/pushplus-notification-settings.resolver';
import { ServerchanNotificationSettingsResolver } from '../settings/shared/services/serverchan-notification-settings.resolver';
import { TelegramNotificationSettingsResolver } from '../settings/shared/services/telegram-notification-settings.resolver';
import { NotificationsComponent } from './notifications.component';
import { NotificationsResolver } from './shared/notifications.resolver';

const routes: Routes = [
  {
    path: 'email-notification',
    component: EmailNotificationSettingsComponent,
    resolve: { settings: EmailNotificationSettingsResolver },
  },
  {
    path: 'serverchan-notification',
    component: ServerchanNotificationSettingsComponent,
    resolve: { settings: ServerchanNotificationSettingsResolver },
  },
  {
    path: 'pushdeer-notification',
    component: PushdeerNotificationSettingsComponent,
    resolve: { settings: PushdeerNotificationSettingsResolver },
  },
  {
    path: 'pushplus-notification',
    component: PushplusNotificationSettingsComponent,
    resolve: { settings: PushplusNotificationSettingsResolver },
  },
  {
    path: 'telegram-notification',
    component: TelegramNotificationSettingsComponent,
    resolve: { settings: TelegramNotificationSettingsResolver },
  },
  {
    path: 'bark-notification',
    component: BarkNotificationSettingsComponent,
    resolve: { settings: BarkNotificationSettingsResolver },
  },
  {
    path: '',
    pathMatch: 'full',
    component: NotificationsComponent,
    resolve: { settings: NotificationsResolver },
  },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class NotificationsRoutingModule {}
