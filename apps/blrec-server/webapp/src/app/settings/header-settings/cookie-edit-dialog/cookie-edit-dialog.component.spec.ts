import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../../settings.module';
import { CookieEditDialogComponent } from './cookie-edit-dialog.component';

describe('CookieEditDialogComponent', () => {
  let component: CookieEditDialogComponent;
  let fixture: ComponentFixture<CookieEditDialogComponent>;

  beforeEach(async () => {
    const validationService = jasmine.createSpyObj<ValidationService>(
      'ValidationService',
      ['validateCookie']
    );
    validationService.validateCookie.and.returnValue(
      of({ code: 200, message: '' })
    );

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: ValidationService, useValue: validationService },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
            'error',
          ]),
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(CookieEditDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
