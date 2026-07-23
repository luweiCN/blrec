import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzInputNumberModule } from 'ng-zorro-antd/input-number';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzProgressModule } from 'ng-zorro-antd/progress';
import { NzRadioModule } from 'ng-zorro-antd/radio';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { UploadPolicyDialogModule } from '../tasks/upload-policy-dialog/upload-policy-dialog.module';
import { MediaLibraryRoutingModule } from './media-library-routing.module';
import { MediaLibraryComponent } from './media-library.component';

@NgModule({
  declarations: [MediaLibraryComponent],
  imports: [
    CommonModule,
    FormsModule,
    UploadPolicyDialogModule,
    MediaLibraryRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzIconModule,
    NzInputModule,
    NzInputNumberModule,
    NzModalModule,
    NzPageHeaderModule,
    NzPaginationModule,
    NzProgressModule,
    NzRadioModule,
    NzTableModule,
    NzTagModule,
  ],
})
export class MediaLibraryModule {}
