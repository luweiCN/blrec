import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { PublicDashboardComponent } from './public-dashboard.component';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';

const routes: Routes = [
  {
    path: '',
    component: PublicDashboardShellComponent,
    children: [
      {
        path: '',
        pathMatch: 'full',
        component: PublicDashboardComponent,
        title: '虚荣对局榜',
      },
      {
        path: 'players',
        component: PlayerRankingsPageComponent,
        title: '玩家综合榜 · 虚荣对局榜',
      },
      {
        path: 'heroes',
        component: HeroRankingsPageComponent,
        title: '英雄胜率榜 · 虚荣对局榜',
      },
    ],
  },
];

@NgModule({
  imports: [
    RouterModule.forRoot(routes, {
      scrollPositionRestoration: 'enabled',
    }),
  ],
  exports: [RouterModule],
})
export class PublicDashboardRoutingModule {}
