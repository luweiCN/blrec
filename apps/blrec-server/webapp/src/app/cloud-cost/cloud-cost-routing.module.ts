import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { CloudCostComponent } from './cloud-cost.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: CloudCostComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class CloudCostRoutingModule {}
