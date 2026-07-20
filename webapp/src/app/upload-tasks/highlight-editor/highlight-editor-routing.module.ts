import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { HighlightEditorComponent } from './highlight-editor.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: HighlightEditorComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class HighlightEditorRoutingModule {}
