import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NgxEchartsModule } from 'ngx-echarts';

import { CloudCostRoutingModule } from './cloud-cost-routing.module';
import { CloudCostComponent } from './cloud-cost.component';

@NgModule({
  declarations: [CloudCostComponent],
  imports: [
    CommonModule,
    CloudCostRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzEmptyModule,
    NzIconModule,
    NzSpinModule,
    NgxEchartsModule.forRoot({ echarts: () => import('echarts') }),
  ],
})
export class CloudCostModule {}
