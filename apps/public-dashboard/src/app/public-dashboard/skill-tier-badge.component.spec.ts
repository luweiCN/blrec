import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import {
  displayScoreForRatingDelta,
  displayScoreForRatingScore,
  skillTierForRatingScore,
  skillTierProgressForRatingScore,
} from './skill-tier';

describe('skillTierForRatingScore', () => {
  it('maps the rating to the original 0–3,000 ranked score scale', () => {
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
        displayScore: 1_200,
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
        displayScore: 2_400,
      }),
    );
    expect(skillTierForRatingScore(867)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'silver',
        displayScore: 2_601,
      }),
    );
    expect(skillTierForRatingScore(934)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'gold',
        displayScore: 2_802,
      }),
    );
  });

  it('handles missing and out-of-range scores safely', () => {
    expect(skillTierForRatingScore(null)).toBeNull();
    expect(skillTierForRatingScore(Number.NaN)).toBeNull();
    expect(skillTierForRatingScore(-20)?.displayScore).toBe(0);
    expect(skillTierForRatingScore(1200)?.displayScore).toBe(3_000);
    expect(displayScoreForRatingScore(572)).toBe(1_716);
    expect(displayScoreForRatingDelta(-19)).toBe(-57);
  });

  it('positions progress using the historical non-uniform boundaries', () => {
    const tierNine = skillTierProgressForRatingScore(720);
    const tierFour = skillTierProgressForRatingScore(400);

    expect(tierNine).toEqual(
      jasmine.objectContaining({
        tierMinimumScore: 2_000,
        tierMaximumScore: 2_400,
        progressPosition: 40,
      }),
    );
    expect(tierNine?.silverBoundaryPosition).toBeCloseTo(33.5, 2);
    expect(tierNine?.goldBoundaryPosition).toBeCloseTo(66.75, 2);
    expect(tierFour?.silverBoundaryPosition).toBeCloseTo(40.52, 2);
    expect(tierFour?.goldBoundaryPosition).toBeCloseTo(81.41, 2);
    expect(skillTierProgressForRatingScore(null)).toBeNull();
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
    expect(page.textContent).toContain('1,716');
    expect(page.textContent).not.toContain('榜单分');
    expect(page.textContent).toContain('定位中');
    expect(page.querySelector('em')?.textContent).toContain('定位中');
    expect(page.querySelector('img')?.getAttribute('src')).toBe(
      'assets/skill-tiers/tier-07-silver-hd.webp',
    );
  });
});
