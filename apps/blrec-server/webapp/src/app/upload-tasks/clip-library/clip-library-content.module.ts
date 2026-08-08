import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzProgressModule } from 'ng-zorro-antd/progress';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { UploadPolicyDialogModule } from '../../tasks/upload-policy-dialog/upload-policy-dialog.module';
import { ClipLibraryComponent } from './clip-library.component';

@NgModule({
  declarations: [ClipLibraryComponent],
  imports: [
    CommonModule,
    FormsModule,
    RouterModule,
    UploadPolicyDialogModule,
    NzAlertModule,
    NzButtonModule,
    NzIconModule,
    NzInputModule,
    NzModalModule,
    NzPageHeaderModule,
    NzPaginationModule,
    NzProgressModule,
    NzTableModule,
    NzTagModule,
  ],
  exports: [ClipLibraryComponent],
})
export class ClipLibraryContentModule {}
