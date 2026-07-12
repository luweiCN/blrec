import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { BasePlayInfoApiUrlEditDialogComponent } from './base-play-info-api-url-edit-dialog.component';

describe('BasePlayInfoApiUrlEditDialogComponent', () => {
  let component: BasePlayInfoApiUrlEditDialogComponent;
  let fixture: ComponentFixture<BasePlayInfoApiUrlEditDialogComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [BasePlayInfoApiUrlEditDialogComponent],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(BasePlayInfoApiUrlEditDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
