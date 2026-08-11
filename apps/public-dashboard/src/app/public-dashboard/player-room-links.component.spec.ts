import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BehaviorSubject } from 'rxjs';

import {
  PlayerLiveRoomStatus,
  PlayerLiveStatusService,
} from './player-live-status.service';
import { PlayerRoomLinksComponent } from './player-room-links.component';

describe('PlayerRoomLinksComponent', () => {
  let fixture: ComponentFixture<PlayerRoomLinksComponent>;
  let component: PlayerRoomLinksComponent;
  let roomStatuses: BehaviorSubject<
    ReadonlyMap<number, PlayerLiveRoomStatus>
  >;

  beforeEach(async () => {
    roomStatuses = new BehaviorSubject<
      ReadonlyMap<number, PlayerLiveRoomStatus>
    >(new Map());
    await TestBed.configureTestingModule({
      declarations: [PlayerRoomLinksComponent],
      imports: [CommonModule],
      providers: [
        {
          provide: PlayerLiveStatusService,
          useValue: { roomStatuses$: roomStatuses.asObservable() },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerRoomLinksComponent);
    component = fixture.componentInstance;
  });

  it('links every known room ID to its Bilibili live room', () => {
    component.roomIds = [24767459, 30038570];
    fixture.detectChanges();

    const links = fixture.nativeElement.querySelectorAll(
      'a',
    ) as NodeListOf<HTMLAnchorElement>;
    expect(links.length).toBe(2);
    expect(links[0].href).toBe('https://live.bilibili.com/24767459');
    expect(links[0].target).toBe('_blank');
    expect(links[0].rel).toContain('noopener');
  });

  it('keeps the fallback text when no room ID is public', () => {
    component.fallbackLabel = '历史录播';
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('a')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('历史录播');
  });

  it('highlights a live room and exposes its title on player details', () => {
    component.roomIds = [24767459];
    component.showLiveTitle = true;
    roomStatuses.next(
      new Map([
        [
          24767459,
          {
            roomId: 24767459,
            playerId: 56,
            title: '今晚三排上分',
            startedAt: '2026-08-11T11:30:00Z',
          },
        ],
      ]),
    );
    fixture.detectChanges();

    const link = fixture.nativeElement.querySelector(
      'a.is-live',
    ) as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.textContent).toContain('直播中');
    expect(link.getAttribute('title')).toBe('今晚三排上分');
    expect(fixture.nativeElement.querySelector('.live-title').textContent).toContain(
      '今晚三排上分',
    );
  });
});
