import { CommonModule } from '@angular/common';
import {
  FullscreenOverlayContainer,
  OverlayContainer,
  OverlayModule,
} from '@angular/cdk/overlay';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { UploadPolicyDialogModule } from '../../tasks/upload-policy-dialog/upload-policy-dialog.module';
import { HighlightEditorRoutingModule } from './highlight-editor-routing.module';
import { HighlightEditorComponent } from './highlight-editor.component';

@NgModule({
  declarations: [HighlightEditorComponent],
  imports: [
    CommonModule,
    FormsModule,
    OverlayModule,
    UploadPolicyDialogModule,
    HighlightEditorRoutingModule,
    NzToolTipModule,
  ],
  providers: [
    { provide: OverlayContainer, useClass: FullscreenOverlayContainer },
  ],
})
export class HighlightEditorModule {}
