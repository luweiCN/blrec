import { Injectable } from '@angular/core';

import { environment } from '../../environments/environment';

export interface DashboardAdminHero {
  readonly id: number;
  readonly label: string;
  readonly thumbnailUrl: string;
}

export interface DashboardAdminMatchPlayer {
  readonly side: 'left' | 'right';
  readonly slot: number;
  name: string;
  heroId: number | null;
  readonly heroLabel: string;
  readonly heroSource: 'automatic' | 'manual';
  readonly heroProbability: number | null;
  kills: number | null;
  deaths: number | null;
  assists: number | null;
  economy: number | null;
  lastHits: number | null;
  readonly confidence: number;
  readonly isRecordedPlayer: boolean;
  readonly afkPredictionStatus: 'unknown' | 'active' | 'afk';
  readonly afkProbability: number | null;
  readonly afkModelVersion: string;
  readonly afkGateReason: string;
  afkManualOverride: boolean | null;
}

export interface DashboardAdminMatch {
  readonly id: number;
  title: string;
  gameMode: '3v3' | '5v5' | 'aram' | 'other' | 'unknown';
  durationSeconds: number | null;
  resultText: string;
  endReason: 'normal' | 'surrender' | 'unknown';
  matchKind: 'pvp' | 'bot' | 'practice' | 'unknown';
  viewContext: 'played' | 'observed' | 'unknown';
  statsEligible: boolean;
  winnerColor: 'teal' | 'orange' | 'unknown';
  readonly leftColor: 'teal' | 'orange' | 'unknown';
  readonly rightColor: 'teal' | 'orange' | 'unknown';
  leftKills: number | null;
  rightKills: number | null;
  leftEconomy: number | null;
  rightEconomy: number | null;
  readonly confidence: number;
  readonly recordedPlayerConfidence: number | null;
  readonly recordedPlayerSource: 'automatic' | 'manual';
  readonly players: DashboardAdminMatchPlayer[];
}

export interface DashboardAdminMatchUpdate {
  readonly title: string;
  readonly gameMode: DashboardAdminMatch['gameMode'];
  readonly durationSeconds: number | null;
  readonly resultText: string;
  readonly endReason: DashboardAdminMatch['endReason'];
  readonly matchKind: DashboardAdminMatch['matchKind'];
  readonly viewContext: DashboardAdminMatch['viewContext'];
  readonly statsEligible: boolean;
  readonly winnerColor: DashboardAdminMatch['winnerColor'];
  readonly leftKills: number | null;
  readonly rightKills: number | null;
  readonly leftEconomy: number | null;
  readonly rightEconomy: number | null;
  readonly recordedPlayer?: Readonly<{ side: 'left' | 'right'; slot: number }>;
  readonly players: readonly {
    readonly side: 'left' | 'right';
    readonly slot: number;
    readonly name: string;
    readonly heroId: number | null;
    readonly kills: number | null;
    readonly deaths: number | null;
    readonly assists: number | null;
    readonly economy: number | null;
    readonly lastHits: number | null;
    readonly afkManualOverride: boolean | null;
  }[];
}

@Injectable({ providedIn: 'root' })
export class DashboardAdminApiService {
  get enabled(): boolean {
    return environment.adminMode && environment.adminApiBaseUrl !== '';
  }

  async getMatch(matchId: number): Promise<DashboardAdminMatch> {
    return this.request<DashboardAdminMatch>(`/matches/${matchId}`);
  }

  async listHeroes(): Promise<readonly DashboardAdminHero[]> {
    return this.request<readonly DashboardAdminHero[]>('/heroes');
  }

  async updateMatch(
    matchId: number,
    update: DashboardAdminMatchUpdate,
  ): Promise<DashboardAdminMatch> {
    return this.request<DashboardAdminMatch>(`/matches/${matchId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
    });
  }

  heroThumbnail(hero: DashboardAdminHero): string {
    return `${this.baseUrl}${hero.thumbnailUrl.replace(
      /^\/api\/v1\/vainglory/u,
      '',
    )}`;
  }

  private get baseUrl(): string {
    return environment.adminApiBaseUrl.replace(/\/+$/u, '');
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!this.enabled) {
      throw new Error('internal dashboard admin API is disabled');
    }
    const response = await fetch(`${this.baseUrl}${path}`, {
      cache: 'no-store',
      credentials: 'same-origin',
      ...init,
    });
    if (!response.ok) {
      throw new Error(`internal dashboard admin API returned ${response.status}`);
    }
    return (await response.json()) as T;
  }
}
