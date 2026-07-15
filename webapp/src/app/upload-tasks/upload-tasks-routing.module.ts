import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { UploadTasksComponent } from './upload-tasks.component';

const routes: Routes = [{ path: '', component: UploadTasksComponent }];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class UploadTasksRoutingModule {}
