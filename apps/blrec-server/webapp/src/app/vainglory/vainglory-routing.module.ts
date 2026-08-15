import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { VaingloryComponent } from './vainglory.component';
import { OperationsComponent } from './operations/operations.component';

const routes: Routes = [
  { path: 'operations', component: OperationsComponent },
  { path: '', pathMatch: 'full', component: VaingloryComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class VaingloryRoutingModule {}
