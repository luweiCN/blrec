import { NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';
import { RouterModule } from '@angular/router';

import { AppComponent } from './app.component';
import { PublicDashboardModule } from './public-dashboard/public-dashboard.module';

@NgModule({
  declarations: [AppComponent],
  imports: [BrowserModule, RouterModule, PublicDashboardModule],
  bootstrap: [AppComponent],
})
export class AppModule {}
