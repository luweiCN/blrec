import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../../settings.module';
import { OutdirEditDialogComponent } from './outdir-edit-dialog.component';

describe('OutdirEditDialogComponent', () => {
  let component: OutdirEditDialogComponent;
  let fixture: ComponentFixture<OutdirEditDialogComponent>;

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
    fixture = TestBed.createComponent(OutdirEditDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
