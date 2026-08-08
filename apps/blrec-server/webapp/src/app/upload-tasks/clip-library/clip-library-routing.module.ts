import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { ClipLibraryComponent } from './clip-library.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: ClipLibraryComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class ClipLibraryRoutingModule {}
