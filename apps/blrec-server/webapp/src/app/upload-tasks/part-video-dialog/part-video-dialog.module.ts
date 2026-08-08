import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzModalModule } from 'ng-zorro-antd/modal';

import { PartVideoDialogComponent } from './part-video-dialog.component';

@NgModule({
  declarations: [PartVideoDialogComponent],
  imports: [CommonModule, NzAlertModule, NzButtonModule, NzModalModule],
  exports: [PartVideoDialogComponent],
})
export class PartVideoDialogModule {}
