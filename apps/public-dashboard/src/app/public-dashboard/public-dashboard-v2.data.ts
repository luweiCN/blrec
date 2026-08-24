import { dashboardRequestInit } from './dashboard-owner-access.service';
import type { DashboardResourceRealtimeUpdate } from './dashboard-realtime.service';
import {
  DashboardSnapshot,
  DashboardTrendPublication,
  DashboardTrendStanding,
  DashboardTrends,
  HeroStanding,
  isSeasonKey,
  ModeFilter,
  PlayerStanding,
  RatingModel,
  SeasonOption,
  SeasonStandings,
} from './public-dashboard.models';

export interface DashboardResourceManifest {
  readonly standings: Readonly<Record<string, string>>;
  readonly environment: Readonly<Record<string, string>>;
  readonly trends: string;
  readonly matches: string;
  readonly liveRooms: string;
}

export interface DashboardV2Summary {
  readonly snapshotId: string;
  readonly contentRevision?: string;
  readonly publicationDate: string;
  readonly generatedAt: string;
  readonly sourceLastMatchId: number;
  readonly sourceMatchCount: number;
  readonly ratingModel: RatingModel;
  readonly currentSeasonKey: SeasonOption['key'];
  readonly seasons: readonly SeasonOption[];
  readonly resources: DashboardResourceManifest;
}

export interface DashboardV2Data {
  readonly snapshot: DashboardSnapshot;
  readonly trends: DashboardTrends | null;
}

export class DashboardV2Client {
  private manifest: DashboardResourceManifest | null = null;
  private readonly standings = new Map<string, string>();
  private readonly environments = new Map<string, string>();
  private trendQuery: TrendQuery | null = null;
  private trendRevision: string | null = null;
  private summaryRevision: string | null = null;

  constructor(private readonly apiBaseUrl: string) {}

  async load(): Promise<DashboardV2Data> {
    const summary = await fetchSummary(this.apiBaseUrl);
    const current = await fetchStandings(
      this.apiBaseUrl,
      summary.currentSeasonKey,
    );
    const standings: Record<string, SeasonStandings> = {};
    for (const season of summary.seasons) {
      standings[season.key] = { players: [], heroes: [] };
    }
    standings[summary.currentSeasonKey] = current;
    this.manifest = summary.resources;
    this.standings.set(
      summary.currentSeasonKey,
      summary.resources.standings[summary.currentSeasonKey] ?? '',
    );
    return { snapshot: snapshotFromSummary(summary, standings), trends: null };
  }

  async ensureStandings(
    data: DashboardV2Data,
    seasonId: SeasonOption['key'],
  ): Promise<DashboardV2Data | null> {
    if (this.standings.has(seasonId)) {
      return null;
    }
    return this.loadStandings(data, seasonId);
  }

  async ensureAllStandings(data: DashboardV2Data): Promise<DashboardV2Data | null> {
    let current = data;
    let changed = false;
    for (const season of data.snapshot.seasons) {
      const next = await this.ensureStandings(current, season.key);
      if (next !== null) {
        current = next;
        changed = true;
      }
    }
    return changed ? current : null;
  }

  async ensureEnvironment(
    data: DashboardV2Data,
    seasonId: SeasonOption['key'],
  ): Promise<DashboardV2Data | null> {
    if (this.environments.has(seasonId)) {
      return null;
    }
    return this.loadEnvironment(data, seasonId);
  }

  async ensureAllEnvironments(data: DashboardV2Data): Promise<DashboardV2Data | null> {
    let current = data;
    let changed = false;
    for (const season of data.snapshot.seasons) {
      const next = await this.ensureEnvironment(current, season.key);
      if (next !== null) {
        current = next;
        changed = true;
      }
    }
    return changed ? current : null;
  }

  async ensureTrends(
    data: DashboardV2Data,
    seasonId: SeasonOption['key'],
    mode: ModeFilter,
    playerIds: readonly number[],
  ): Promise<DashboardV2Data | null> {
    const query = normalizeQuery(seasonId, mode, playerIds);
    if (sameQuery(query, this.trendQuery)) {
      return null;
    }
    return this.loadTrends(data, query);
  }

  async refreshResource(
    data: DashboardV2Data,
    update: DashboardResourceRealtimeUpdate,
  ): Promise<DashboardV2Data | null> {
    switch (update.resource) {
      case 'summary':
        if (this.summaryRevision === update.revision) {
          return null;
        }
        return this.refreshSummary(data, update.revision);
      case 'standings':
        return update.seasonId !== undefined &&
          this.standings.has(update.seasonId) &&
          this.standings.get(update.seasonId) !== update.revision
          ? this.loadStandings(data, update.seasonId, update.revision)
          : null;
      case 'environment':
        return update.seasonId !== undefined &&
          this.environments.has(update.seasonId) &&
          this.environments.get(update.seasonId) !== update.revision
          ? this.loadEnvironment(data, update.seasonId, update.revision)
          : null;
      case 'trends':
        return this.trendQuery !== null && this.trendRevision !== update.revision
          ? this.loadTrends(data, this.trendQuery, update.revision)
          : null;
    }
  }

  async resync(data: DashboardV2Data): Promise<DashboardV2Data | null> {
    const previous = this.manifest;
    let current = await this.refreshSummary(data);
    let changed = current !== data;
    const manifest = this.manifest;
    if (manifest === null) {
      return changed ? current : null;
    }
    for (const [seasonId, revision] of [...this.standings]) {
      const nextRevision = manifest.standings[seasonId];
      if (nextRevision !== undefined && nextRevision !== revision) {
        current = await this.loadStandings(current, seasonId, nextRevision);
        changed = true;
      }
    }
    for (const [seasonId, revision] of [...this.environments]) {
      const nextRevision = manifest.environment[seasonId];
      if (nextRevision !== undefined && nextRevision !== revision) {
        current = await this.loadEnvironment(current, seasonId, nextRevision);
        changed = true;
      }
    }
    if (this.trendQuery !== null && previous?.trends !== manifest.trends) {
      current = await this.loadTrends(
        current,
        this.trendQuery,
        manifest.trends,
      );
      changed = true;
    }
    return changed ? current : null;
  }

  private async refreshSummary(
    data: DashboardV2Data,
    revision?: string,
  ): Promise<DashboardV2Data> {
    const summary = await fetchSummary(this.apiBaseUrl);
    this.manifest = summary.resources;
    this.summaryRevision = revision ?? null;
    let next: DashboardV2Data = {
      ...data,
      snapshot: {
        ...data.snapshot,
        ...snapshotMetadata(summary),
        seasons: summary.seasons,
      },
    };
    if (!this.standings.has(summary.currentSeasonKey)) {
      next = await this.loadStandings(next, summary.currentSeasonKey);
    }
    return next;
  }

  private async loadStandings(
    data: DashboardV2Data,
    seasonId: string,
    revision?: string,
  ): Promise<DashboardV2Data> {
    const resource = await fetchStandings(this.apiBaseUrl, seasonId);
    this.standings.set(
      seasonId,
      revision ?? this.manifest?.standings[seasonId] ?? '',
    );
    return {
      ...data,
      snapshot: {
        ...data.snapshot,
        standings: { ...data.snapshot.standings, [seasonId]: resource },
      },
    };
  }

  private async loadEnvironment(
    data: DashboardV2Data,
    seasonId: string,
    revision?: string,
  ): Promise<DashboardV2Data> {
    const environmentHeroes = await fetchEnvironment(this.apiBaseUrl, seasonId);
    const standings = data.snapshot.standings[seasonId] ?? {
      players: [],
      heroes: [],
    };
    this.environments.set(
      seasonId,
      revision ?? this.manifest?.environment[seasonId] ?? '',
    );
    return {
      ...data,
      snapshot: {
        ...data.snapshot,
        standings: {
          ...data.snapshot.standings,
          [seasonId]: { ...standings, environmentHeroes },
        },
      },
    };
  }

  private async loadTrends(
    data: DashboardV2Data,
    query: TrendQuery,
    revision?: string,
  ): Promise<DashboardV2Data> {
    const trends = await fetchTrends(
      this.apiBaseUrl,
      query.seasonId,
      query.mode,
      query.playerIds,
    );
    this.trendQuery = query;
    this.trendRevision = revision ?? this.manifest?.trends ?? null;
    return { ...data, trends };
  }
}

interface TrendQuery {
  readonly seasonId: SeasonOption['key'];
  readonly mode: ModeFilter;
  readonly playerIds: readonly number[];
}

export function baseUrl(apiBaseUrl: string): string {
  return apiBaseUrl.replace(/\/v1$/u, '/v2');
}

async function fetchSummary(apiBaseUrl: string): Promise<DashboardV2Summary> {
  return parseSummary(await fetchJson(`${baseUrl(apiBaseUrl)}/dashboard/summary`));
}

function snapshotMetadata(
  summary: DashboardV2Summary,
): Pick<
  DashboardSnapshot,
  | 'snapshotId'
  | 'contentRevision'
  | 'publicationDate'
  | 'generatedAt'
  | 'sourceLastMatchId'
  | 'sourceMatchCount'
  | 'ratingModel'
  | 'currentSeasonKey'
> {
  return {
    snapshotId: summary.snapshotId,
    contentRevision: summary.contentRevision,
    publicationDate: summary.publicationDate,
    generatedAt: summary.generatedAt,
    sourceLastMatchId: summary.sourceLastMatchId,
    sourceMatchCount: summary.sourceMatchCount,
    ratingModel: summary.ratingModel,
    currentSeasonKey: summary.currentSeasonKey,
  };
}

function snapshotFromSummary(
  summary: DashboardV2Summary,
  standings: Readonly<Record<string, SeasonStandings>>,
): DashboardSnapshot {
  return {
    schemaVersion: 3,
    ...snapshotMetadata(summary),
    seasons: summary.seasons,
    standings,
    matches: [],
  };
}

function normalizeQuery(
  seasonId: TrendQuery['seasonId'],
  mode: TrendQuery['mode'],
  playerIds: readonly number[],
): TrendQuery {
  return {
    seasonId,
    mode,
    playerIds: [...new Set(playerIds)].sort((left, right) => left - right),
  };
}

function sameQuery(left: TrendQuery, right: TrendQuery | null): boolean {
  return (
    right !== null &&
    left.seasonId === right.seasonId &&
    left.mode === right.mode &&
    left.playerIds.length === right.playerIds.length &&
    left.playerIds.every((playerId, index) => playerId === right.playerIds[index])
  );
}

export function parseSummary(value: unknown): DashboardV2Summary {
  if (!isRecord(value) || value['schemaVersion'] !== 1) {
    throw new Error('dashboard v2 summary has an unsupported format');
  }
  const seasons = value['seasons'];
  const resources = value['resources'];
  if (
    typeof value['snapshotId'] !== 'string' ||
    typeof value['publicationDate'] !== 'string' ||
    typeof value['generatedAt'] !== 'string' ||
    !isInteger(value['sourceLastMatchId']) ||
    !isInteger(value['sourceMatchCount']) ||
    !isRecord(value['ratingModel']) ||
    typeof value['currentSeasonKey'] !== 'string' ||
    !isSeasonKey(value['currentSeasonKey']) ||
    !Array.isArray(seasons) ||
    !isManifest(resources)
  ) {
    throw new Error('dashboard v2 summary has an unsupported format');
  }
  return {
    snapshotId: value['snapshotId'],
    ...(typeof value['contentRevision'] === 'string'
      ? { contentRevision: value['contentRevision'] }
      : {}),
    publicationDate: value['publicationDate'],
    generatedAt: value['generatedAt'],
    sourceLastMatchId: value['sourceLastMatchId'],
    sourceMatchCount: value['sourceMatchCount'],
    ratingModel: value['ratingModel'] as unknown as RatingModel,
    currentSeasonKey: value['currentSeasonKey'],
    seasons: seasons as readonly SeasonOption[],
    resources,
  };
}

export async function fetchStandings(
  apiBaseUrl: string,
  seasonId: string,
): Promise<{
  readonly players: readonly PlayerStanding[];
  readonly heroes: readonly HeroStanding[];
}> {
  const value = await fetchResource(apiBaseUrl, 'standings', seasonId);
  if (!Array.isArray(value['players']) || !Array.isArray(value['heroes'])) {
    throw new Error('dashboard v2 standings have an unsupported format');
  }
  const players = value['players'].map((player) => {
    if (!isRecord(player) || !isRecord(player['heroPools'])) {
      throw new Error('dashboard v2 player standing is invalid');
    }
    return { ...player, heroPool: player['heroPools']['all'] } as unknown as PlayerStanding;
  });
  return { players, heroes: value['heroes'] as readonly HeroStanding[] };
}

export async function fetchEnvironment(
  apiBaseUrl: string,
  seasonId: string,
): Promise<readonly HeroStanding[]> {
  const value = await fetchResource(apiBaseUrl, 'environment', seasonId);
  if (!Array.isArray(value['environmentHeroes'])) {
    throw new Error('dashboard v2 environment has an unsupported format');
  }
  return value['environmentHeroes'] as readonly HeroStanding[];
}

export async function fetchTrends(
  apiBaseUrl: string,
  seasonId: SeasonOption['key'],
  mode: ModeFilter,
  playerIds: readonly number[],
): Promise<DashboardTrends> {
  const parameters = new URLSearchParams({ seasonId, mode });
  if (playerIds.length > 0) {
    parameters.set('playerIds', playerIds.join(','));
  }
  const value = await fetchJson(`${baseUrl(apiBaseUrl)}/trends?${parameters}`);
  if (
    !isRecord(value) ||
    value['schemaVersion'] !== 1 ||
    typeof value['updatedAt'] !== 'string' ||
    !Array.isArray(value['publications'])
  ) {
    throw new Error('dashboard v2 trends have an unsupported format');
  }
  const publications = value['publications'].map((publication) =>
    normalizePublication(publication, seasonId, mode),
  );
  return { schemaVersion: 1, updatedAt: value['updatedAt'], publications };
}

async function fetchResource(
  apiBaseUrl: string,
  resource: 'standings' | 'environment',
  seasonId: string,
): Promise<Record<string, unknown>> {
  const parameters = new URLSearchParams({ seasonId });
  const value = await fetchJson(`${baseUrl(apiBaseUrl)}/${resource}?${parameters}`);
  if (
    !isRecord(value) ||
    value['schemaVersion'] !== 1 ||
    value['seasonId'] !== seasonId
  ) {
    throw new Error(`dashboard v2 ${resource} has an unsupported format`);
  }
  return value;
}

async function fetchJson(url: string): Promise<unknown> {
  const response = await fetch(url, dashboardRequestInit('no-cache'));
  if (!response.ok) {
    throw new Error(`fetch ${url} failed with ${response.status}`);
  }
  return response.json() as Promise<unknown>;
}

function normalizePublication(
  value: unknown,
  seasonId: SeasonOption['key'],
  mode: ModeFilter,
): DashboardTrendPublication {
  if (!isRecord(value) || !isRecord(value['standings'])) {
    throw new Error('dashboard v2 trend publication is invalid');
  }
  const season = value['standings'][seasonId];
  const rows = isRecord(season) ? season[mode] : undefined;
  if (
    typeof value['snapshotId'] !== 'string' ||
    typeof value['publicationDate'] !== 'string' ||
    !Array.isArray(rows) ||
    !rows.every(isTrendStanding)
  ) {
    throw new Error('dashboard v2 trend publication is invalid');
  }
  const modes: Record<ModeFilter, readonly DashboardTrendStanding[]> = {
    all: [],
    '3v3': [],
    brawl: [],
    '5v5': [],
  };
  modes[mode] = rows;
  return {
    snapshotId: value['snapshotId'],
    publicationDate: value['publicationDate'],
    sourceLastMatchId: 0,
    standings: { [seasonId]: modes },
  };
}

function isManifest(value: unknown): value is DashboardResourceManifest {
  return (
    isRecord(value) &&
    isStringRecord(value['standings']) &&
    isStringRecord(value['environment']) &&
    typeof value['trends'] === 'string' &&
    typeof value['matches'] === 'string' &&
    typeof value['liveRooms'] === 'string'
  );
}

function isStringRecord(value: unknown): value is Readonly<Record<string, string>> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === 'string');
}

function isTrendStanding(value: unknown): value is DashboardTrendStanding {
  return (
    isRecord(value) &&
    isInteger(value['playerId']) &&
    value['playerId'] > 0 &&
    isInteger(value['rank']) &&
    value['rank'] > 0 &&
    typeof value['ratingScore'] === 'number'
  );
}

function isInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
