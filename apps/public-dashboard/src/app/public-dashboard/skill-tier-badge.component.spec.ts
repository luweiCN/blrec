import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import { skillTierForRatingScore } from './skill-tier';

describe('skillTierForRatingScore', () => {
  it('uses the historical 0–3000 point boundaries after scaling', () => {
    expect(skillTierForRatingScore(0)).toEqual(
      jasmine.objectContaining({
        tier: 1,
        division: 'bronze',
        legacyPoints: 0,
      }),
    );
    expect(skillTierForRatingScore(400)).toEqual(
      jasmine.objectContaining({
        tier: 4,
        division: 'gold',
        legacyPoints: 1200,
      }),
    );
    expect(skillTierForRatingScore(800)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'bronze',
        legacyPoints: 2400,
      }),
    );
    expect(skillTierForRatingScore(867)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'silver',
        legacyPoints: 2601,
      }),
    );
    expect(skillTierForRatingScore(934)).toEqual(
      jasmine.objectContaining({
        tier: 10,
        division: 'gold',
        legacyPoints: 2802,
      }),
    );
  });

  it('handles missing and out-of-range scores safely', () => {
    expect(skillTierForRatingScore(null)).toBeNull();
    expect(skillTierForRatingScore(Number.NaN)).toBeNull();
    expect(skillTierForRatingScore(-20)?.legacyPoints).toBe(0);
    expect(skillTierForRatingScore(1200)?.legacyPoints).toBe(3000);
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
    expect(page.textContent).toContain('榜单分 572');
    expect(page.textContent).toContain('定位中');
    expect(page.querySelector('img')?.getAttribute('src')).toBe(
      'assets/skill-tiers/tier-07-silver.webp',
    );
  });
});
