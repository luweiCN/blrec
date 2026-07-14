import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';

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
    NzCardModule,
    NzCollapseModule,
    NzEmptyModule,
    NzInputModule,
    NzModalModule,
    NzPageHeaderModule,
    NzSpinModule,
    NzTagModule,
  ],
})
export class UploadTasksModule {}
