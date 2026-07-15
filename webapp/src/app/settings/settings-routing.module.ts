import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { SettingsResolver } from './shared/services/settings.resolver';
import { WebhookSettingsResolver } from './shared/services/webhook-settings.resolver';
import { SettingsComponent } from './settings.component';
import { WebhookManagerComponent } from './webhook-settings/webhook-manager/webhook-manager.component';

const routes: Routes = [
  {
    path: 'email-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/email-notification',
  },
  {
    path: 'serverchan-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/serverchan-notification',
  },
  {
    path: 'pushdeer-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/pushdeer-notification',
  },
  {
    path: 'pushplus-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/pushplus-notification',
  },
  {
    path: 'telegram-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/telegram-notification',
  },
  {
    path: 'bark-notification',
    pathMatch: 'full',
    redirectTo: '/notifications/bark-notification',
  },
  {
    path: 'webhooks',
    component: WebhookManagerComponent,
    resolve: {
      settings: WebhookSettingsResolver,
    },
  },
  {
    path: '',
    component: SettingsComponent,
    resolve: {
      settings: SettingsResolver,
    },
  },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class SettingsRoutingModule { }
