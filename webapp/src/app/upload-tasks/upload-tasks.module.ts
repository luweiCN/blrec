import { CommonModule } from '@angular/common';
import { ClipboardModule } from '@angular/cdk/clipboard';
import {
  FullscreenOverlayContainer,
  OverlayContainer,
  OverlayModule,
} from '@angular/cdk/overlay';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzDatePickerModule } from 'ng-zorro-antd/date-picker';
import { NzDrawerModule } from 'ng-zorro-antd/drawer';
import { NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzMenuModule } from 'ng-zorro-antd/menu';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzProgressModule } from 'ng-zorro-antd/progress';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzSwitchModule } from 'ng-zorro-antd/switch';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { UploadPolicyDialogModule } from '../tasks/upload-policy-dialog/upload-policy-dialog.module';
import { RecordingSessionsComponent } from './recording-sessions/recording-sessions.component';
import { HighlightEditorComponent } from './highlight-editor/highlight-editor.component';
import { PartDanmakuDialogComponent } from './part-danmaku-dialog/part-danmaku-dialog.component';
import { PartVideoDialogComponent } from './part-video-dialog/part-video-dialog.component';
import { UploadTasksRoutingModule } from './upload-tasks-routing.module';
import { UploadTasksComponent } from './upload-tasks.component';
import { TaskEditDialogComponent } from './task-edit-dialog/task-edit-dialog.component';

@NgModule({
  declarations: [
    UploadTasksComponent,
    RecordingSessionsComponent,
    PartDanmakuDialogComponent,
    PartVideoDialogComponent,
    TaskEditDialogComponent,
    HighlightEditorComponent,
  ],
  imports: [
    CommonModule,
    ClipboardModule,
    OverlayModule,
    FormsModule,
    UploadPolicyDialogModule,
    UploadTasksRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzCheckboxModule,
    NzDatePickerModule,
    NzDrawerModule,
    NzDropDownModule,
    NzInputModule,
    NzIconModule,
    NzModalModule,
    NzMenuModule,
    NzPageHeaderModule,
    NzPaginationModule,
    NzProgressModule,
    NzSelectModule,
    NzSpinModule,
    NzSwitchModule,
    NzTableModule,
    NzTagModule,
    NzToolTipModule,
  ],
  providers: [
    { provide: OverlayContainer, useClass: FullscreenOverlayContainer },
  ],
})
export class UploadTasksModule {}
