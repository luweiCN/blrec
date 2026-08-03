import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { PlayGuidePageComponent } from './play-guide-page.component';
import { RankingGuidePageComponent } from './ranking-guide-page.component';

describe('public guide pages', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [RankingGuidePageComponent, PlayGuidePageComponent],
      imports: [RouterTestingModule],
    }).compileComponents();
  });

  it('explains the leaderboard sample boundary and rating model', () => {
    const fixture: ComponentFixture<RankingGuidePageComponent> =
      TestBed.createComponent(RankingGuidePageComponent);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.textContent).toContain('直播样本榜');
    expect(page.textContent).toContain('不会因此自动进入玩家排行榜');
    expect(page.textContent).toContain('90% 可信下界');
    expect(page.textContent).toContain('历史数据正在持续同步中');
  });

  it('documents the current client, party code, and download risks', () => {
    const fixture: ComponentFixture<PlayGuidePageComponent> =
      TestBed.createComponent(PlayGuidePageComponent);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.textContent).toContain('4.13.4');
    expect(page.textContent).toContain('组队码可以正常用于约局和组队');
    expect(page.textContent).toContain('6666-1_名字 / 6666-2_名字');
    expect(page.textContent).toContain('APKPure 不是官方商店');
    expect(
      page.querySelector<HTMLAnchorElement>(
        'a[href*="apps.apple.com"]',
      )?.target,
    ).toBe('_blank');
  });
});
