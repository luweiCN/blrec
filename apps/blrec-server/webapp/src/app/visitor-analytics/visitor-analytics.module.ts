import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzDatePickerModule } from 'ng-zorro-antd/date-picker';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzIconModule } from 'ng-zorro-antd/icon';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTabsModule } from 'ng-zorro-antd/tabs';
import { NgxEchartsModule } from 'ngx-echarts';

import { VisitorAnalyticsRoutingModule } from './visitor-analytics-routing.module';
import { VisitorAnalyticsComponent } from './visitor-analytics.component';

@NgModule({
  declarations: [VisitorAnalyticsComponent],
  imports: [
    CommonModule,
    FormsModule,
    VisitorAnalyticsRoutingModule,
    NzAlertModule,
    NzButtonModule,
    NzDatePickerModule,
    NzEmptyModule,
    NzIconModule,
    NzInputModule,
    NzSelectModule,
    NzSpinModule,
    NzTabsModule,
    NgxEchartsModule.forRoot({ echarts: () => import('echarts') }),
  ],
})
export class VisitorAnalyticsModule {}
