import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzDrawerModule } from 'ng-zorro-antd/drawer';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzImageModule } from 'ng-zorro-antd/image';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzProgressModule } from 'ng-zorro-antd/progress';
import { NzPopconfirmModule } from 'ng-zorro-antd/popconfirm';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzTabsModule } from 'ng-zorro-antd/tabs';

import { PartVideoDialogModule } from '../upload-tasks/part-video-dialog/part-video-dialog.module';
import { VaingloryRoutingModule } from './vainglory-routing.module';
import { VaingloryComponent } from './vainglory.component';

@NgModule({
  declarations: [VaingloryComponent],
  imports: [
    CommonModule,
    FormsModule,
    PartVideoDialogModule,
    VaingloryRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzCheckboxModule,
    NzDrawerModule,
    NzEmptyModule,
    NzIconModule,
    NzImageModule,
    NzInputModule,
    NzModalModule,
    NzPageHeaderModule,
    NzPaginationModule,
    NzProgressModule,
    NzPopconfirmModule,
    NzSelectModule,
    NzSpinModule,
    NzTagModule,
    NzTabsModule,
  ],
})
export class VaingloryModule {}
