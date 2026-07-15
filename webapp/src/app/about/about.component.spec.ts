import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of } from 'rxjs';

import { AppInfo } from '../core/models/app.models';
import { AboutComponent } from './about.component';

const appInfo: AppInfo = {
  name: '',
  version: '',
  pid: 1,
  ppid: 1,
  create_time: 0,
  cwd: '',
  exe: '',
  cmdline: [],
};

describe('AboutComponent', () => {
  let component: AboutComponent;
  let fixture: ComponentFixture<AboutComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [AboutComponent],
      providers: [
        {
          provide: ActivatedRoute,
          useValue: jasmine.createSpyObj<ActivatedRoute>('ActivatedRoute', [], {
            data: of({ appInfo }),
          }),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(AboutComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('uses one shared primary-page container', () => {
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1
    );
  });
});
