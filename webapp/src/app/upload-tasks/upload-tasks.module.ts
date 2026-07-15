import { CommonModule } from '@angular/common';
import { ClipboardModule } from '@angular/cdk/clipboard';
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
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { RecordingSessionsComponent } from './recording-sessions/recording-sessions.component';
import { PartContentDialogComponent } from './part-content-dialog/part-content-dialog.component';
import { UploadTasksRoutingModule } from './upload-tasks-routing.module';
import { UploadTasksComponent } from './upload-tasks.component';

@NgModule({
  declarations: [
    UploadTasksComponent,
    RecordingSessionsComponent,
    PartContentDialogComponent,
  ],
  imports: [
    CommonModule,
    ClipboardModule,
    FormsModule,
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
    NzSelectModule,
    NzTableModule,
    NzTagModule,
    NzToolTipModule,
  ],
})
export class UploadTasksModule {}
