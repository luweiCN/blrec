import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { BaseApiUrlEditDialogComponent } from './base-api-url-edit-dialog.component';

describe('BaseApiUrlEditDialogComponent', () => {
  let component: BaseApiUrlEditDialogComponent;
  let fixture: ComponentFixture<BaseApiUrlEditDialogComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [BaseApiUrlEditDialogComponent],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(BaseApiUrlEditDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
