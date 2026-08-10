import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { PlayerRoomLinksComponent } from './player-room-links.component';

describe('PlayerRoomLinksComponent', () => {
  let fixture: ComponentFixture<PlayerRoomLinksComponent>;
  let component: PlayerRoomLinksComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [PlayerRoomLinksComponent],
      imports: [CommonModule],
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
});
