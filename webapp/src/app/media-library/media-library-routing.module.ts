import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { MediaLibraryComponent } from './media-library.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: MediaLibraryComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class MediaLibraryRoutingModule {}
