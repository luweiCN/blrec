import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { DownloadGuidePageComponent } from './download-guide-page.component';
import { PlayGuidePageComponent } from './play-guide-page.component';
import { RankingGuidePageComponent } from './ranking-guide-page.component';
import { SkillTierBadgeComponent } from './skill-tier-badge.component';

describe('public guide pages', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        RankingGuidePageComponent,
        PlayGuidePageComponent,
        DownloadGuidePageComponent,
        SkillTierBadgeComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
    }).compileComponents();
  });

  it('explains the leaderboard sample boundary and rating model', () => {
    const fixture: ComponentFixture<RankingGuidePageComponent> =
      TestBed.createComponent(RankingGuidePageComponent);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.textContent).toContain('直播样本榜');
    expect(page.textContent).toContain('不会因此自动进入玩家榜');
    expect(page.textContent).toContain('虚拟平均对局');
    expect(page.textContent).toContain('胜利上涨、失败下降');
    expect(page.textContent).not.toContain('下一局胜率约为 77.3%');
    expect(page.textContent).not.toContain('经验量 · 35%');
    expect(page.textContent).not.toContain('稳定胜率 · 40%');
    expect(page.textContent).toContain('RANKING / 07');
    expect(page.textContent).toContain('更新于 2026-08-15');
    expect(page.textContent).toContain('冬季赛');
    expect(page.textContent).toContain('不是从《虚荣》服务器读取的官方 MMR');
    expect(page.textContent).toContain('新玩家先从平均水平开始');
    expect(page.textContent).not.toContain('30 场 19 胜 11 负');
    expect(page.textContent).toContain('为什么同段位、相近胜率');
    expect(page.textContent).toContain('它不能单独用来判断谁更强');
    expect(page.textContent).not.toContain('内部评分 × 3 = 排位分');
    expect(page.textContent).toContain('0–3000');
    expect(page.textContent).toContain('不是玩家当年的官方游戏段位');
    expect(page.textContent).toContain('历史数据正在持续同步中');
    expect(page.textContent).toContain('当前公开数据的主体');
    expect(page.textContent).toContain('5V5 战绩匹配尚未完成');
    expect(page.querySelector('.guide-switcher')).toBeNull();
  });

  it('documents acceleration, regional queues, and the full party-code flow', () => {
    const fixture: ComponentFixture<PlayGuidePageComponent> =
      TestBed.createComponent(PlayGuidePageComponent);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.textContent).toContain('先开加速器，再打开《虚荣》');
    expect(page.textContent).toContain('工作日白天和凌晨');
    expect(page.textContent).toContain('排位通常全天都有机会匹配到人');
    expect(page.textContent).toContain('5V5 内战');
    expect(page.textContent).toContain('需要路人补位');
    expect(page.textContent).toContain('低于 1600');
    expect(page.textContent).toContain('高于 1600');
    expect(page.textContent).toContain('征召 BP');
    expect(page.textContent).toContain('两条线各管一件事');
    expect(page.textContent).toContain('使用低于 3000 的数字');
    expect(page.textContent).toContain('使用高于 3000 的数字');
    expect(page.textContent).toContain('点首页右上角的玩家名或 Guest');
    expect(page.textContent).toContain('1200-1_各自昵称');
    expect(page.textContent).toContain('2000-1_各自昵称');
    expect(page.textContent).toContain('6666-1_小明');
    expect(page.textContent).toContain('6666-2_小王');
    expect(page.textContent).not.toContain('APKPure 不是官方商店');
    expect(page.querySelectorAll('.guide-switcher a').length).toBe(2);
    expect(page.querySelector('.guide-switcher')?.textContent).not.toContain(
      '榜单说明',
    );
  });

  it('keeps official, archived, and VGNA download paths separate', () => {
    const fixture: ComponentFixture<DownloadGuidePageComponent> =
      TestBed.createComponent(DownloadGuidePageComponent);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.textContent).toContain('美区直接搜索，国区只看历史已购');
    expect(page.textContent).toContain('在 App Store 搜索后直接下载');
    expect(page.textContent).toContain('从“已购项目”重新下载');
    expect(page.textContent).not.toContain('香港');
    expect(page.textContent).not.toContain('港区');
    expect(page.textContent).toContain('Android 要安装完整 XAPK');
    expect(page.textContent).toContain('VGNA 是社区增强版，不是官方续作');
    expect(page.textContent).toContain('玩家不需要自己购买 Apple Developer 账号');
    expect(page.textContent).toContain('会绑定 Discord');
    expect(page.querySelectorAll('.guide-switcher a').length).toBe(2);
    expect(page.querySelector('.guide-switcher')?.textContent).not.toContain(
      '榜单说明',
    );
    expect(
      page.querySelector<HTMLAnchorElement>(
        'a[href^="https://apps.apple.com/us/"]',
      )?.target,
    ).toBe('_blank');
    expect(
      page.querySelector<HTMLAnchorElement>(
        'a[href$="VGNA-Client-Android-1.04r3-OEM.apk"]',
      )?.target,
    ).toBe('_blank');
  });
});
