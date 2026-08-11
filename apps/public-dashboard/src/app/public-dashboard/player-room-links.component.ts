import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { Subscription } from 'rxjs';

import {
  PlayerLiveRoomStatus,
  PlayerLiveStatusService,
} from './player-live-status.service';

@Component({
  selector: 'app-player-room-links',
  templateUrl: './player-room-links.component.html',
  styleUrls: ['./player-room-links.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerRoomLinksComponent implements OnInit, OnDestroy {
  @Input() roomIds: readonly number[] = [];
  @Input() fallbackLabel = '历史录播';
  @Input() showLiveTitle = false;

  roomStatuses: ReadonlyMap<number, PlayerLiveRoomStatus> = new Map();
  private statusSubscription: Subscription | null = null;

  constructor(
    private readonly liveStatus: PlayerLiveStatusService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.statusSubscription = this.liveStatus.roomStatuses$.subscribe(
      (roomStatuses) => {
        this.roomStatuses = roomStatuses;
        this.changeDetector.markForCheck();
      },
    );
  }

  ngOnDestroy(): void {
    this.statusSubscription?.unsubscribe();
  }

  roomUrl(roomId: number): string {
    return `https://live.bilibili.com/${roomId}`;
  }

  liveRoom(roomId: number): PlayerLiveRoomStatus | null {
    return this.roomStatuses.get(roomId) ?? null;
  }

  roomAriaLabel(roomId: number, liveRoom: PlayerLiveRoomStatus | null): string {
    if (liveRoom === null) {
      return `打开 B 站直播间 ${roomId}`;
    }
    const title = liveRoom.title === '' ? '' : `，直播标题：${liveRoom.title}`;
    return `正在直播，打开 B 站直播间 ${roomId}${title}`;
  }

  trackRoom(_index: number, roomId: number): number {
    return roomId;
  }
}
