import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { HighlightEditorComponent } from './highlight-editor/highlight-editor.component';
import { UploadTasksComponent } from './upload-tasks.component';

const routes: Routes = [
  { path: 'highlights/:sessionId', component: HighlightEditorComponent },
  { path: '', component: UploadTasksComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class UploadTasksRoutingModule {}
