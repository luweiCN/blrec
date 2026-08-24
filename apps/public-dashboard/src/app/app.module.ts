import { NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';
import { RouterModule } from '@angular/router';

import { AppComponent } from './app.component';

@NgModule({
  declarations: [AppComponent],
  imports: [
    BrowserModule,
    RouterModule.forRoot(
      [
        {
          path: '',
          loadChildren: () =>
            import('./public-dashboard/public-dashboard.module').then(
              (module) => module.PublicDashboardModule,
            ),
        },
      ],
      {
        anchorScrolling: 'enabled',
        scrollPositionRestoration: 'enabled',
      },
    ),
  ],
  bootstrap: [AppComponent],
})
export class AppModule {}
