import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { VisitorAnalyticsComponent } from './visitor-analytics.component';

const routes: Routes = [
  { path: '', pathMatch: 'full', component: VisitorAnalyticsComponent },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class VisitorAnalyticsRoutingModule {}
