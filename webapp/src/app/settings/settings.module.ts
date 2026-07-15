import { NgModule } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ReactiveFormsModule } from '@angular/forms';

import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzSwitchModule } from 'ng-zorro-antd/switch';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzRadioModule } from 'ng-zorro-antd/radio';
import { NzSliderModule } from 'ng-zorro-antd/slider';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzListModule } from 'ng-zorro-antd/list';
import { NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';
import { NzDividerModule } from 'ng-zorro-antd/divider';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';

import { SharedModule } from '../shared/shared.module';
import { SettingsResolver } from './shared/services/settings.resolver';
import { WebhookSettingsResolver } from './shared/services/webhook-settings.resolver';
import { SettingsRoutingModule } from './settings-routing.module';
import { SettingsComponent } from './settings.component';
import { BaseUrlValidatorDirective } from './shared/directives/base-url-validator.directive';
import { SettingsSharedModule } from './shared/settings-shared.module';
import { DiskSpaceSettingsComponent } from './disk-space-settings/disk-space-settings.component';
import { LoggingSettingsComponent } from './logging-settings/logging-settings.component';
import { DanmakuSettingsComponent } from './danmaku-settings/danmaku-settings.component';
import { PostProcessingSettingsComponent } from './post-processing-settings/post-processing-settings.component';
import { RecorderSettingsComponent } from './recorder-settings/recorder-settings.component';
import { HeaderSettingsComponent } from './header-settings/header-settings.component';
import { UserAgentEditDialogComponent } from './header-settings/user-agent-edit-dialog/user-agent-edit-dialog.component';
import { CookieEditDialogComponent } from './header-settings/cookie-edit-dialog/cookie-edit-dialog.component';
import { OutputSettingsComponent } from './output-settings/output-settings.component';
import { WebhookSettingsComponent } from './webhook-settings/webhook-settings.component';
import { WebhookManagerComponent } from './webhook-settings/webhook-manager/webhook-manager.component';
import { WebhookEditDialogComponent } from './webhook-settings/webhook-edit-dialog/webhook-edit-dialog.component';
import { WebhookListComponent } from './webhook-settings/webhook-list/webhook-list.component';
import { OutdirEditDialogComponent } from './output-settings/outdir-edit-dialog/outdir-edit-dialog.component';
import { LogdirEditDialogComponent } from './logging-settings/logdir-edit-dialog/logdir-edit-dialog.component';
import { PathTemplateEditDialogComponent } from './output-settings/path-template-edit-dialog/path-template-edit-dialog.component';
import { BiliApiSettingsComponent } from './bili-api-settings/bili-api-settings.component';
import { BaseApiUrlEditDialogComponent } from './bili-api-settings/base-api-url-edit-dialog/base-api-url-edit-dialog.component';
import { BaseLiveApiUrlEditDialogComponent } from './bili-api-settings/base-live-api-url-edit-dialog/base-live-api-url-edit-dialog.component';
import { BasePlayInfoApiUrlEditDialogComponent } from './bili-api-settings/base-play-info-api-url-edit-dialog/base-play-info-api-url-edit-dialog.component';
import { LiveMonitorSettingsComponent } from './live-monitor-settings/live-monitor-settings.component';

@NgModule({
  declarations: [
    SettingsComponent,
    BaseUrlValidatorDirective,
    DiskSpaceSettingsComponent,
    LoggingSettingsComponent,
    DanmakuSettingsComponent,
    PostProcessingSettingsComponent,
    RecorderSettingsComponent,
    HeaderSettingsComponent,
    UserAgentEditDialogComponent,
    CookieEditDialogComponent,
    OutputSettingsComponent,
    WebhookSettingsComponent,
    WebhookManagerComponent,
    WebhookEditDialogComponent,
    WebhookListComponent,
    OutdirEditDialogComponent,
    LogdirEditDialogComponent,
    PathTemplateEditDialogComponent,
    BiliApiSettingsComponent,
    BaseApiUrlEditDialogComponent,
    BaseLiveApiUrlEditDialogComponent,
    BasePlayInfoApiUrlEditDialogComponent,
    LiveMonitorSettingsComponent,
  ],
  imports: [
    CommonModule,
    SettingsRoutingModule,
    FormsModule,
    ReactiveFormsModule,

    NzSpinModule,
    NzPageHeaderModule,
    NzCardModule,
    NzAlertModule,
    NzFormModule,
    NzInputModule,
    NzSwitchModule,
    NzCheckboxModule,
    NzRadioModule,
    NzSliderModule,
    NzSelectModule,
    NzModalModule,
    NzButtonModule,
    NzIconModule,
    NzListModule,
    NzDropDownModule,
    NzToolTipModule,
    NzDividerModule,
    NzTableModule,
    NzCollapseModule,
    SharedModule,
    SettingsSharedModule,
  ],
  providers: [
    SettingsResolver,
    WebhookSettingsResolver,
  ],
})
export class SettingsModule {}
