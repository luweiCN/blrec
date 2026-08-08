import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import {
  findHero,
  getHeroRankings,
  heroForSeason,
  heroImage,
  modeLabel,
  playersForSeason,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import {
  HeroIdentity,
  heroIdentity,
} from './public-dashboard.hero-names';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  COMPETITIVE_MODE_OPTIONS,
  CompetitiveMode,
  HeroPerformance,
  HeroStanding,
  HeroUsage,
  ModeFilter,
  PlayerStanding,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

interface HeroModeRecord {
  readonly key: CompetitiveMode;
  readonly label: string;
  readonly performance: HeroPerformance;
}

interface HeroPlayerRecord {
  readonly player: PlayerStanding;
  readonly usage: HeroUsage;
}

interface HeroSeasonRecord {
  readonly season: SeasonOption;
  readonly performance: HeroPerformance;
  readonly rank: number | null;
}

const EMPTY_PERFORMANCE: HeroPerformance = {
  matches: 0,
  wins: 0,
  players: 0,
};

@Component({
  selector: 'app-hero-detail-page',
  templateUrl: './hero-detail-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-profile-page.scss',
    './leaderboard-profile-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HeroDetailPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  private readonly modeSubscription: Subscription;

  constructor(
    private readonly data: DashboardDataService,
    private readonly route: ActivatedRoute,
    dashboardMode: DashboardModeService,
    changeDetector: ChangeDetectorRef,
  ) {
    this.activeSeason = data.snapshot.currentSeasonKey;
    this.activeMode = dashboardMode.mode;
    this.modeSubscription = dashboardMode.mode$.subscribe((mode) => {
      if (mode === this.activeMode) {
        return;
      }
      this.activeMode = mode;
      changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.modeSubscription.unsubscribe();
  }

  get heroId(): string {
    return this.route.snapshot.paramMap.get('heroId') ?? '';
  }

  get hero(): HeroStanding | undefined {
    return findHero(this.data.snapshot, this.heroId);
  }

  get seasonHero(): HeroStanding | undefined {
    return heroForSeason(this.data.snapshot, this.activeSeason, this.heroId);
  }

  get identity(): HeroIdentity | undefined {
    return this.hero === undefined ? undefined : heroIdentity(this.hero.name);
  }

  get seasonOptions(): readonly SeasonOption[] {
    return this.data.snapshot.seasons;
  }

  get selectedSeason(): SeasonOption {
    return seasonOption(this.data.snapshot, this.activeSeason);
  }

  get performance(): HeroPerformance {
    return this.seasonHero?.modes[this.activeMode] ?? EMPTY_PERFORMANCE;
  }

  get rank(): number | null {
    const hero = this.hero;
    if (hero === undefined) {
      return null;
    }
    const index = getHeroRankings(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
    ).findIndex(
      (standing) =>
        standing.name.toLocaleLowerCase() === hero.name.toLocaleLowerCase(),
    );
    return index < 0 ? null : index + 1;
  }

  get modeRecords(): readonly HeroModeRecord[] {
    const hero = this.seasonHero;
    if (hero === undefined) {
      return [];
    }
    return COMPETITIVE_MODE_OPTIONS.map((mode) => ({
      ...mode,
      performance: hero.modes[mode.key],
    }));
  }

  get playerRecords(): readonly HeroPlayerRecord[] {
    const hero = this.hero;
    if (hero === undefined) {
      return [];
    }
    const normalizedName = hero.name.toLocaleLowerCase();
    const records: HeroPlayerRecord[] = [];
    for (const player of playersForSeason(
      this.data.snapshot,
      this.activeSeason,
    )) {
      const usage = player.heroPool.find(
        (candidate) =>
          candidate.name.toLocaleLowerCase() === normalizedName,
      );
      if (usage !== undefined) {
        records.push({ player, usage });
      }
    }
    return records.sort(
      (left, right) =>
        right.usage.matches - left.usage.matches ||
        right.usage.wins - left.usage.wins ||
        left.player.id - right.player.id,
    );
  }

  get seasonHistory(): readonly HeroSeasonRecord[] {
    const hero = this.hero;
    if (hero === undefined) {
      return [];
    }
    const records: HeroSeasonRecord[] = [];
    for (const season of this.data.snapshot.seasons) {
      const seasonHero = heroForSeason(
        this.data.snapshot,
        season.key,
        hero.name,
      );
      if (
        seasonHero === undefined ||
        seasonHero.modes[this.activeMode].matches === 0
      ) {
        continue;
      }
      const rankIndex = getHeroRankings(
        this.data.snapshot,
        season.key,
        this.activeMode,
      ).findIndex(
        (standing) =>
          standing.name.toLocaleLowerCase() === hero.name.toLocaleLowerCase(),
      );
      records.push({
        season,
        performance: seasonHero.modes[this.activeMode],
        rank: rankIndex < 0 ? null : rankIndex + 1,
      });
    }
    return records;
  }

  selectSeason(season: SeasonKey): void {
    this.activeSeason = season;
  }

  heroImage(heroName: string): string {
    return heroImage(heroName);
  }

  winRate(value: { readonly matches: number; readonly wins: number }): number {
    return winRate(value);
  }

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  trackMode(_index: number, record: HeroModeRecord): CompetitiveMode {
    return record.key;
  }

  trackPlayer(_index: number, record: HeroPlayerRecord): number {
    return record.player.id;
  }

  trackSeason(_index: number, record: HeroSeasonRecord): SeasonKey {
    return record.season.key;
  }
}
