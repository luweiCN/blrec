import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';

import { InputFilesizeComponent } from './input-filesize.component';

describe('InputFilesizeComponent', () => {
  let component: InputFilesizeComponent;
  let fixture: ComponentFixture<InputFilesizeComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [InputFilesizeComponent],
      imports: [ReactiveFormsModule, NzFormModule, NzInputModule],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(InputFilesizeComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
