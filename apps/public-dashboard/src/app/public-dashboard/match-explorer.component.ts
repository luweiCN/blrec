import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import {
  DashboardMatchApiService,
  DashboardMatchPage,
} from './dashboard-match-api.service';
import {
  formatEconomy,
  heroImage,
  modeLabel,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  currentMatchStreak,
  filterDashboardMatches,
} from './public-dashboard.matches';
import {
  DashboardMatch,
  DashboardMatchPlayer,
  DashboardMatchRating,
  ModeFilter,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';
import { DashboardDataService } from './public-dashboard-data.service';

const PAGE_SIZE = 20;

type MatchPageState =
  | { readonly kind: 'local' }
  | { readonly kind: 'loading'; readonly page: DashboardMatchPage | null }
  | { readonly kind: 'ready'; readonly page: DashboardMatchPage }
  | {
      readonly kind: 'error';
      readonly page: DashboardMatchPage | null;
      readonly message: string;
    };

@Component({
  selector: 'app-match-explorer',
  templateUrl: './match-explorer.component.html',
  styleUrls: [
    './match-explorer.component.scss',
    './match-explorer-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MatchExplorerComponent implements OnChanges, OnDestroy {
  @Input() matches: readonly DashboardMatch[] = [];
  @Input() players: readonly PlayerStanding[] = [];
  @Input() seasonKey: SeasonKey = 'all-time';
  @Input() mode: ModeFilter = 'all';
  @Input() fixedPlayerId?: number;
  @Input() title = '最近对局';
  @Input() contextLabel = '';

  playerQuery = '';
  selectedHeroes: readonly string[] = [];
  page = 1;
  selectedMatch: DashboardMatch | null = null;
  expandSelectedResultImage = false;
  requestState: MatchPageState = { kind: 'local' };
  readonly loadingRows = Array.from({ length: 6 }, (_, index) => index);
  private requestSequence = 0;
  private searchTimer?: ReturnType<typeof setTimeout>;
  private readonly matchRevisionSubscription: Subscription;

  constructor(
    private readonly matchApi: DashboardMatchApiService,
    dashboardData: DashboardDataService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {
    this.matchRevisionSubscription = dashboardData.matchRevision$.subscribe(() => {
      if (this.matchApi.enabled) {
        void this.loadApiPage();
      }
    });
  }

  ngOnChanges(): void {
    this.page = 1;
    this.selectedMatch = null;
    this.expandSelectedResultImage = false;
    this.trimHeroSelection();
    void this.loadApiPage();
  }

  ngOnDestroy(): void {
    this.requestSequence += 1;
    if (this.searchTimer !== undefined) {
      clearTimeout(this.searchTimer);
    }
    this.matchRevisionSubscription.unsubscribe();
  }

  get filteredMatches(): readonly DashboardMatch[] {
    const apiPage = this.apiPage;
    if (apiPage !== null) {
      return apiPage.items;
    }
    return filterDashboardMatches(this.matches, this.players, {
      seasonKey: this.seasonKey,
      mode: this.mode,
      fixedPlayerId: this.fixedPlayerId,
      playerQuery: this.playerQuery,
      selectedHeroes: this.selectedHeroes,
    });
  }

  get pageMatches(): readonly DashboardMatch[] {
    const apiPage = this.apiPage;
    if (apiPage !== null) {
      return apiPage.items;
    }
    const start = (this.page - 1) * PAGE_SIZE;
    return this.filteredMatches.slice(start, start + PAGE_SIZE);
  }

  get pageCount(): number {
    const total = this.apiPage?.total ?? this.filteredMatches.length;
    return Math.max(1, Math.ceil(total / PAGE_SIZE));
  }

  get totalMatches(): number {
    return this.apiPage?.total ?? this.filteredMatches.length;
  }

  get isInitialLoading(): boolean {
    return this.requestState.kind === 'loading' && this.requestState.page === null;
  }

  get isRefreshing(): boolean {
    return this.requestState.kind === 'loading';
  }

  get loadError(): string | null {
    return this.requestState.kind === 'error'
      ? this.requestState.message
      : null;
  }

  get heroOptions(): readonly string[] {
    const baseMatches = filterDashboardMatches(this.matches, this.players, {
      seasonKey: this.seasonKey,
      mode: this.mode,
      fixedPlayerId: this.fixedPlayerId,
      playerQuery: '',
      selectedHeroes: [],
    });
    const names = new Set<string>();
    for (const match of baseMatches) {
      for (const player of [...match.ally.players, ...match.enemy.players]) {
        if (player.heroName !== '') {
          names.add(player.heroName);
        }
      }
    }
    for (const player of this.players) {
      for (const usage of player.heroPool) {
        if (usage.name !== '') {
          names.add(usage.name);
        }
      }
    }
    return Array.from(names).sort((left, right) =>
      this.heroName(left).localeCompare(this.heroName(right), 'zh-CN'),
    );
  }

  get heroSelectionLimit(): number {
    return this.mode === '5v5' || this.mode === 'all' ? 10 : 6;
  }

  get hasLocalFilters(): boolean {
    return this.playerQuery.trim() !== '' || this.selectedHeroes.length > 0;
  }

  get summaryText(): string {
    if (this.hasLocalFilters) {
      return `按直播时间倒序 · 筛选结果 ${this.totalMatches} 场`;
    }
    const recent = this.filteredMatches.slice(0, PAGE_SIZE);
    if (recent.length === 0) {
      return '按直播时间倒序 · 暂无已识别对局';
    }
    const wins = recent.filter((match) => match.result === 'W').length;
    const streak = currentMatchStreak(this.filteredMatches);
    const streakText =
      streak === null
        ? ''
        : ` · 连${streak.result === 'W' ? '胜' : '败'} ${streak.matches} 场`;
    return `按直播时间倒序 · 最近 ${recent.length} 场 ${wins} 胜 ${recent.length - wins} 负${streakText}`;
  }

  get playerSearchLabel(): string {
    return this.fixedPlayerId === undefined ? '搜索玩家' : '搜索队友或对手';
  }

  get playerSearchPlaceholder(): string {
    return this.fixedPlayerId === undefined
      ? '主播名、玩家名、直播标题或拼音'
      : '玩家名、直播标题、拼音或首字母';
  }

  setPlayerQuery(event: Event): void {
    this.playerQuery = (event.target as HTMLInputElement).value;
    this.page = 1;
    this.scheduleApiSearch();
  }

  toggleHero(heroName: string): void {
    if (this.selectedHeroes.includes(heroName)) {
      this.selectedHeroes = this.selectedHeroes.filter(
        (selected) => selected !== heroName,
      );
    } else if (this.selectedHeroes.length < this.heroSelectionLimit) {
      this.selectedHeroes = [...this.selectedHeroes, heroName];
    }
    this.page = 1;
    void this.loadApiPage();
  }

  clearFilters(): void {
    this.playerQuery = '';
    this.selectedHeroes = [];
    this.page = 1;
    void this.loadApiPage();
  }

  clearHeroFilters(): void {
    this.selectedHeroes = [];
    this.page = 1;
    void this.loadApiPage();
  }

  previousPage(): void {
    this.page = Math.max(1, this.page - 1);
    void this.loadApiPage();
  }

  nextPage(): void {
    this.page = Math.min(this.pageCount, this.page + 1);
    void this.loadApiPage();
  }

  openMatch(match: DashboardMatch): void {
    this.selectedMatch = match;
    this.expandSelectedResultImage = false;
  }

  openResultImage(match: DashboardMatch): void {
    this.selectedMatch = match;
    this.expandSelectedResultImage = true;
  }

  closeMatch(): void {
    this.selectedMatch = null;
    this.expandSelectedResultImage = false;
  }

  retryApiPage(): void {
    void this.loadApiPage();
  }

  playerName(playerId: number): string {
    return this.players.find((player) => player.id === playerId)?.name ?? '主播';
  }

  heroImage(heroName: string): string {
    return heroImage(heroName);
  }

  heroName(heroName: string): string {
    return heroName === '' ? '未识别英雄' : heroDisplayName(heroName);
  }

  modeName(mode: ModeFilter): string {
    return modeLabel(mode);
  }

  formatEconomy(value: number | null): string {
    return formatEconomy(value);
  }

  formatDate(value: string): string {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(value));
  }

  formatDuration(seconds: number): string {
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }

  matchRatingAriaLabel(rating: DashboardMatchRating): string {
    const change =
      rating.scoreDelta > 0
        ? `增加 ${rating.scoreDelta}`
        : rating.scoreDelta < 0
          ? `减少 ${Math.abs(rating.scoreDelta)}`
          : '不变';
    return `排位分从 ${rating.scoreBefore} 变为 ${rating.scoreAfter}，本局${change}`;
  }

  matchAriaLabel(match: DashboardMatch): string {
    return `${this.formatDate(match.playedAt)}，${this.modeName(match.mode)}，${
      match.result === 'W' ? '胜利' : '失败'
    }，查看对局详情`;
  }

  trackMatch(_index: number, match: DashboardMatch): number {
    return match.id;
  }

  trackHero(_index: number, heroName: string): string {
    return heroName;
  }

  trackPlayer(_index: number, player: DashboardMatchPlayer): string {
    return `${player.name}:${player.heroName}`;
  }

  private trimHeroSelection(): void {
    const options = new Set(this.heroOptions);
    this.selectedHeroes = this.selectedHeroes
      .filter((heroName) => options.has(heroName))
      .slice(0, this.heroSelectionLimit);
  }

  private scheduleApiSearch(): void {
    if (this.searchTimer !== undefined) {
      clearTimeout(this.searchTimer);
    }
    this.searchTimer = setTimeout(() => {
      this.searchTimer = undefined;
      void this.loadApiPage();
    }, 250);
  }

  private async loadApiPage(): Promise<void> {
    if (!this.matchApi.enabled) {
      this.requestState = { kind: 'local' };
      return;
    }
    const sequence = ++this.requestSequence;
    const stalePage = this.apiPage;
    this.requestState = { kind: 'loading', page: stalePage };
    this.changeDetector.markForCheck();
    let response: DashboardMatchPage;
    try {
      response = await this.matchApi.list({
        page: this.page,
        pageSize: PAGE_SIZE,
        seasonKey: this.seasonKey,
        mode: this.mode,
        playerId: this.fixedPlayerId,
        query: this.playerQuery,
        heroes: this.selectedHeroes,
      });
    } catch (error: unknown) {
      if (sequence !== this.requestSequence) {
        return;
      }
      console.warn('Unable to load matches from dashboard API', error);
      this.requestState = {
        kind: 'error',
        page: stalePage,
        message: '实时对局暂时没有加载成功，请稍后重试。',
      };
      this.changeDetector.markForCheck();
      return;
    }
    if (sequence !== this.requestSequence) {
      return;
    }
    this.requestState = { kind: 'ready', page: response };
    if (this.page > this.pageCount) {
      this.page = this.pageCount;
      void this.loadApiPage();
      return;
    }
    this.changeDetector.markForCheck();
  }

  private get apiPage(): DashboardMatchPage | null {
    return this.requestState.kind === 'local'
      ? null
      : this.requestState.page;
  }
}
