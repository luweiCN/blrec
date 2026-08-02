import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { VaingloryComponent } from './vainglory.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: VaingloryComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class VaingloryRoutingModule {}
