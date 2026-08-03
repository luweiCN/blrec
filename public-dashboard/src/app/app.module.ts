import { APP_INITIALIZER, NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';
import { RouterModule } from '@angular/router';

import { AppComponent } from './app.component';
import {
  DashboardDataService,
  initializeDashboardData,
} from './public-dashboard/public-dashboard-data.service';
import { PublicDashboardModule } from './public-dashboard/public-dashboard.module';

@NgModule({
  declarations: [AppComponent],
  imports: [BrowserModule, RouterModule, PublicDashboardModule],
  providers: [
    {
      provide: APP_INITIALIZER,
      useFactory: initializeDashboardData,
      deps: [DashboardDataService],
      multi: true,
    },
  ],
  bootstrap: [AppComponent],
})
export class AppModule {}
