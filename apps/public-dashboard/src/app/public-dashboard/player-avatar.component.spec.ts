import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { PlayerAvatarComponent } from './player-avatar.component';

describe('PlayerAvatarComponent', () => {
  let fixture: ComponentFixture<PlayerAvatarComponent>;
  let component: PlayerAvatarComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [PlayerAvatarComponent],
      imports: [CommonModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerAvatarComponent);
    component = fixture.componentInstance;
  });

  it('loads the static avatar generated for the stable player id', () => {
    component.playerId = 56;
    component.heroName = 'Ylva';
    component.ngOnChanges();

    expect(component.imageSource).toBe('data/avatars/56.jpg');
    expect(component.source).toBe('player');
  });

  it('falls back to the hero portrait instead of a name initial', () => {
    component.playerId = 56;
    component.heroName = 'Ylva';
    component.ngOnChanges();
    component.handleImageError();

    expect(component.imageSource).toBe(
      'assets/vainglory/heroes/ylva.jpg',
    );
    expect(component.source).toBe('hero');
  });
});
