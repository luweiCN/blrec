import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';

import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { PublicDashboardRoutingModule } from './public-dashboard-routing.module';
import { PublicDashboardComponent } from './public-dashboard.component';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';

@NgModule({
  declarations: [
    PublicDashboardComponent,
    PublicDashboardShellComponent,
    LeaderboardFiltersComponent,
    LeaderboardSeasonSelectComponent,
    PlayerRankingsPageComponent,
    HeroRankingsPageComponent,
  ],
  imports: [CommonModule, PublicDashboardRoutingModule],
})
export class PublicDashboardModule {}
