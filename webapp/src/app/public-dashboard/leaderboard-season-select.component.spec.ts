import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';

describe('LeaderboardSeasonSelectComponent', () => {
  let fixture: ComponentFixture<LeaderboardSeasonSelectComponent>;
  let component: LeaderboardSeasonSelectComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [LeaderboardSeasonSelectComponent],
      imports: [CommonModule],
    }).compileComponents();

    fixture = TestBed.createComponent(LeaderboardSeasonSelectComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('opens a styled listbox and emits the selected season', () => {
    const selected: string[] = [];
    component.valueChange.subscribe((value) => selected.push(value));

    const trigger = fixture.nativeElement.querySelector(
      '.season-trigger',
    ) as HTMLButtonElement;
    trigger.click();
    fixture.detectChanges();

    const options = fixture.nativeElement.querySelectorAll(
      '[role="option"]',
    ) as NodeListOf<HTMLButtonElement>;
    expect(options.length).toBe(4);

    options[1].click();
    fixture.detectChanges();

    expect(selected).toEqual(['2026-spring']);
    expect(component.isOpen).toBeFalse();
  });

  it('supports arrow keys and Escape', () => {
    const trigger = fixture.nativeElement.querySelector(
      '.season-trigger',
    ) as HTMLButtonElement;
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'End', bubbles: true }),
    );
    fixture.detectChanges();

    expect(component.isOpen).toBeTrue();
    expect(component.activeIndex).toBe(3);

    const lastOption = fixture.nativeElement.querySelectorAll(
      '[role="option"]',
    )[3] as HTMLButtonElement;
    lastOption.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    fixture.detectChanges();

    expect(component.isOpen).toBeFalse();
  });
});
