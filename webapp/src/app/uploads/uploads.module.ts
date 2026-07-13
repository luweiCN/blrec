import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzAvatarModule } from 'ng-zorro-antd/avatar';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { UploadsRoutingModule } from './uploads-routing.module';
import { UploadsComponent } from './uploads.component';

@NgModule({
  declarations: [UploadsComponent],
  imports: [
    CommonModule,
    UploadsRoutingModule,
    NzAlertModule,
    NzAvatarModule,
    NzButtonModule,
    NzCardModule,
    NzEmptyModule,
    NzPageHeaderModule,
    NzSpinModule,
    NzTagModule,
    NzToolTipModule,
  ],
})
export class UploadsModule {}
