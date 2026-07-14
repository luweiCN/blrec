import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzDrawerModule } from 'ng-zorro-antd/drawer';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { RecordingSessionsComponent } from './recording-sessions/recording-sessions.component';
import { UploadTasksRoutingModule } from './upload-tasks-routing.module';
import { UploadTasksComponent } from './upload-tasks.component';

@NgModule({
  declarations: [UploadTasksComponent, RecordingSessionsComponent],
  imports: [
    CommonModule,
    FormsModule,
    UploadTasksRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzDrawerModule,
    NzInputModule,
    NzModalModule,
    NzPageHeaderModule,
    NzPaginationModule,
    NzTableModule,
    NzTagModule,
    NzToolTipModule,
  ],
})
export class UploadTasksModule {}
