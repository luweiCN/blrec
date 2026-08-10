import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'app-player-room-links',
  templateUrl: './player-room-links.component.html',
  styleUrls: ['./player-room-links.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerRoomLinksComponent {
  @Input() roomIds: readonly number[] = [];
  @Input() fallbackLabel = '历史录播';

  roomUrl(roomId: number): string {
    return `https://live.bilibili.com/${roomId}`;
  }

  trackRoom(_index: number, roomId: number): number {
    return roomId;
  }
}
