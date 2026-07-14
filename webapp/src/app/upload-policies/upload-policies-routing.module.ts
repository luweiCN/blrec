import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { UploadPoliciesComponent } from './upload-policies.component';

const routes: Routes = [{ path: '', component: UploadPoliciesComponent }];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class UploadPoliciesRoutingModule {}
