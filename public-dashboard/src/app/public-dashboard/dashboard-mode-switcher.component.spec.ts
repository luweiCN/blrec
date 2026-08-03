import { ComponentFixture, TestBed } from '@angular/core/testing';

import { DashboardModeSwitcherComponent } from './dashboard-mode-switcher.component';

describe('DashboardModeSwitcherComponent', () => {
  let fixture: ComponentFixture<DashboardModeSwitcherComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [DashboardModeSwitcherComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(DashboardModeSwitcherComponent);
    fixture.detectChanges();
  });

  it('shows 3V3 as the default and emits a mode selection', () => {
    const component = fixture.componentInstance;
    const emittedModes: string[] = [];
    component.valueChange.subscribe((mode) => emittedModes.push(mode));
    const page = fixture.nativeElement as HTMLElement;
    const buttons = Array.from(page.querySelectorAll<HTMLButtonElement>('button'));
    const selected = buttons.find(
      (button) => button.getAttribute('aria-pressed') === 'true',
    );
    const brawl = buttons.find((button) =>
      button.textContent?.includes('乱斗'),
    );

    expect(selected?.textContent).toContain('3V3');
    brawl?.click();
    expect(emittedModes).toEqual(['brawl']);
  });
});
