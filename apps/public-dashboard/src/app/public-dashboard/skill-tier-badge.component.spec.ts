import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import {
  displayScoreForRatingDelta,
  displayScoreForRatingScore,
  skillTierForRatingScore,
} from './skill-tier';

describe('skillTierForRatingScore', () => {
  it('maps the rating to 0–30,000 while preserving historical tiers', () => {
    expect(skillTierForRatingScore(0)).toEqual(
      jasmine.objectContaining({
        tier: 1,
        division: 'bronze',
        displayScore: 0,
      }),
    );
    expect(skillTierForRatingScore(400)).toEqual(
      jasmine.objectContaining({
        tier: 4,
        division: 'gold',
        displayScore: 12_000,
      }),
    );
    expect(skillTierForRatingScore(533)).toEqual(
      jasmine.objectContaining({ tier: 6, division: 'gold' }),
    );
    expect(skillTierForRatingScore(534)).toEqual(
      jasmine.objectContaining({ tier: 7, division: 'bronze' }),
    );
    expect(skillTierForRatingScore(800)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'bronze',
        displayScore: 24_000,
      }),
    );
    expect(skillTierForRatingScore(867)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'silver',
        displayScore: 26_010,
      }),
    );
    expect(skillTierForRatingScore(934)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'gold',
        displayScore: 28_020,
      }),
    );
  });

  it('handles missing and out-of-range scores safely', () => {
    expect(skillTierForRatingScore(null)).toBeNull();
    expect(skillTierForRatingScore(Number.NaN)).toBeNull();
    expect(skillTierForRatingScore(-20)?.displayScore).toBe(0);
    expect(skillTierForRatingScore(1200)?.displayScore).toBe(30_000);
    expect(displayScoreForRatingScore(572)).toBe(17_160);
    expect(displayScoreForRatingDelta(-19)).toBe(-570);
  });
});

describe('SkillTierBadgeComponent', () => {
  let fixture: ComponentFixture<SkillTierBadgeComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [SkillTierBadgeComponent],
      imports: [CommonModule],
    }).compileComponents();

    fixture = TestBed.createComponent(SkillTierBadgeComponent);
  });

  it('shows the localized tier, original badge and underlying score', () => {
    fixture.componentInstance.ratingScore = 572;
    fixture.componentInstance.provisional = true;
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    expect(page.textContent).toContain('炉火纯青 · 银');
    expect(page.textContent).toContain('17,160');
    expect(page.textContent).not.toContain('榜单分');
    expect(page.textContent).not.toContain('段位分');
    expect(page.textContent).toContain('定位中');
    expect(page.querySelector('img')?.getAttribute('src')).toBe(
      'assets/skill-tiers/tier-07-silver-hd.webp',
    );
  });
});
