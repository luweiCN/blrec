import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnDestroy,
} from '@angular/core';

import {
  DashboardMatchApiService,
  DashboardMatchPage,
} from './dashboard-match-api.service';
import { heroImage, modeLabel } from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  currentMatchStreak,
  filterDashboardMatches,
} from './public-dashboard.matches';
import {
  DashboardMatch,
  DashboardMatchPlayer,
  ModeFilter,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';

const PAGE_SIZE = 10;

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
  private apiPage: DashboardMatchPage | null = null;
  private requestSequence = 0;
  private searchTimer?: ReturnType<typeof setTimeout>;

  constructor(
    private readonly matchApi: DashboardMatchApiService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnChanges(): void {
    this.page = 1;
    this.selectedMatch = null;
    this.trimHeroSelection();
    void this.loadApiPage();
  }

  ngOnDestroy(): void {
    this.requestSequence += 1;
    if (this.searchTimer !== undefined) {
      clearTimeout(this.searchTimer);
    }
  }

  get filteredMatches(): readonly DashboardMatch[] {
    if (this.apiPage !== null) {
      return this.apiPage.items;
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
    if (this.apiPage !== null) {
      return this.apiPage.items;
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
  }

  closeMatch(): void {
    this.selectedMatch = null;
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
      return;
    }
    const sequence = ++this.requestSequence;
    const response = await this.matchApi.list({
      page: this.page,
      pageSize: PAGE_SIZE,
      seasonKey: this.seasonKey,
      mode: this.mode,
      playerId: this.fixedPlayerId,
      query: this.playerQuery,
      heroes: this.selectedHeroes,
    });
    if (sequence !== this.requestSequence) {
      return;
    }
    this.apiPage = response;
    if (response !== null && this.page > this.pageCount) {
      this.page = this.pageCount;
      void this.loadApiPage();
      return;
    }
    this.changeDetector.markForCheck();
  }
}
