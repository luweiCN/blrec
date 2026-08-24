import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { DownloadGuidePageComponent } from './download-guide-page.component';
import { HeroDetailPageComponent } from './hero-detail-page.component';
import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { MatchesPageComponent } from './matches-page.component';
import { PlayGuidePageComponent } from './play-guide-page.component';
import { PlayerDetailPageComponent } from './player-detail-page.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { PublicDashboardComponent } from './public-dashboard.component';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';
import { RankingGuidePageComponent } from './ranking-guide-page.component';

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
        path: 'matches',
        component: MatchesPageComponent,
        title: '对局档案 · 虚荣对局榜',
      },
      {
        path: 'players',
        component: PlayerRankingsPageComponent,
        title: '玩家榜 · 虚荣对局榜',
      },
      {
        path: 'players/:playerId',
        component: PlayerDetailPageComponent,
        title: '玩家详情 · 虚荣对局榜',
      },
      {
        path: 'heroes',
        component: HeroRankingsPageComponent,
        title: '英雄榜 · 虚荣对局榜',
      },
      {
        path: 'heroes/:heroId',
        component: HeroDetailPageComponent,
        title: '英雄详情 · 虚荣对局榜',
      },
      {
        path: 'guide',
        pathMatch: 'full',
        redirectTo: 'guide/rankings',
      },
      {
        path: 'guide/rankings',
        component: RankingGuidePageComponent,
        title: '榜单说明 · 虚荣对局榜',
      },
      {
        path: 'guide/play',
        component: PlayGuidePageComponent,
        title: '如何游玩虚荣 · 虚荣对局榜',
      },
      {
        path: 'guide/download',
        component: DownloadGuidePageComponent,
        title: '虚荣下载指南 · 虚荣对局榜',
      },
    ],
  },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class PublicDashboardRoutingModule {}
