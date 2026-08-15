import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';

import { DashboardModeSwitcherComponent } from './dashboard-mode-switcher.component';
import { DownloadGuidePageComponent } from './download-guide-page.component';
import { HeroDetailPageComponent } from './hero-detail-page.component';
import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { MatchDetailModalComponent } from './match-detail-modal.component';
import { MatchExplorerComponent } from './match-explorer.component';
import { MatchesPageComponent } from './matches-page.component';
import { PlayGuidePageComponent } from './play-guide-page.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { PlayerRatingTrendChartComponent } from './player-rating-trend-chart.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { PlayerRoomLinksComponent } from './player-room-links.component';
import { PlayerDetailPageComponent } from './player-detail-page.component';
import { PublicDashboardRoutingModule } from './public-dashboard-routing.module';
import { PublicDashboardComponent } from './public-dashboard.component';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';
import { RankingGuidePageComponent } from './ranking-guide-page.component';
import { SeasonCorrectionNoticeComponent } from './season-correction-notice.component';
import { SiteStatsComponent } from './site-stats.component';
import { SkillTierBadgeComponent } from './skill-tier-badge.component';

@NgModule({
  declarations: [
    PublicDashboardComponent,
    PublicDashboardShellComponent,
    DashboardModeSwitcherComponent,
    LeaderboardFiltersComponent,
    LeaderboardSeasonSelectComponent,
    PlayerAvatarComponent,
    PlayerRatingTrendChartComponent,
    PlayerRoomLinksComponent,
    MatchDetailModalComponent,
    MatchExplorerComponent,
    MatchesPageComponent,
    PlayerRankingsPageComponent,
    HeroRankingsPageComponent,
    PlayerDetailPageComponent,
    HeroDetailPageComponent,
    RankingGuidePageComponent,
    PlayGuidePageComponent,
    DownloadGuidePageComponent,
    SeasonCorrectionNoticeComponent,
    SiteStatsComponent,
    SkillTierBadgeComponent,
  ],
  imports: [CommonModule, PublicDashboardRoutingModule],
})
export class PublicDashboardModule {}
