import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../../settings.module';
import { LogdirEditDialogComponent } from './logdir-edit-dialog.component';

describe('LogdirEditDialogComponent', () => {
  let component: LogdirEditDialogComponent;
  let fixture: ComponentFixture<LogdirEditDialogComponent>;

  beforeEach(async () => {
    const validationService = jasmine.createSpyObj<ValidationService>(
      'ValidationService',
      ['validateDir']
    );
    validationService.validateDir.and.returnValue(
      of({ code: 200, message: '' })
    );

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: ValidationService, useValue: validationService },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(LogdirEditDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
