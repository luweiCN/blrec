import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzAvatarModule } from 'ng-zorro-antd/avatar';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzRadioModule } from 'ng-zorro-antd/radio';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { UploadsRoutingModule } from './uploads-routing.module';
import { RecordingSessionsComponent } from './recording-sessions/recording-sessions.component';
import { UploadsComponent } from './uploads.component';

@NgModule({
  declarations: [UploadsComponent, RecordingSessionsComponent],
  imports: [
    CommonModule,
    FormsModule,
    UploadsRoutingModule,
    NzAlertModule,
    NzAvatarModule,
    NzButtonModule,
    NzCardModule,
    NzCollapseModule,
    NzEmptyModule,
    NzModalModule,
    NzPageHeaderModule,
    NzRadioModule,
    NzSelectModule,
    NzSpinModule,
    NzTagModule,
    NzToolTipModule,
  ],
})
export class UploadsModule {}
