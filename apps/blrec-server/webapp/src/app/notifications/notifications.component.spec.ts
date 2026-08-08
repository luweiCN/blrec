import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';

import { NotificationsComponent } from './notifications.component';

describe('NotificationsComponent', () => {
  let fixture: ComponentFixture<NotificationsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [NotificationsComponent],
      imports: [NoopAnimationsModule, NzPageHeaderModule],
      providers: [
        {
          provide: ActivatedRoute,
          useValue: { data: of({ settings: {} }) },
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(NotificationsComponent);
    fixture.detectChanges();
  });

  it('renders notification settings as a primary page', () => {
    expect(fixture.nativeElement.textContent).toContain('通知设置');
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1
    );
    expect(
      fixture.nativeElement.querySelector('app-notification-settings')
    ).not.toBeNull();
  });
});
