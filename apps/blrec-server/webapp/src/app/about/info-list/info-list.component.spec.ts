import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';

import { AppInfo } from 'src/app/core/models/app.models';
import { UpdateService } from 'src/app/core/services/update.service';
import { InfoListComponent } from './info-list.component';

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

describe('InfoListComponent', () => {
  let component: InfoListComponent;
  let fixture: ComponentFixture<InfoListComponent>;

  beforeEach(async () => {
    const updateService = jasmine.createSpyObj<UpdateService>('UpdateService', [
      'getLatestVerisonString',
    ]);
    updateService.getLatestVerisonString.and.returnValue(of(''));

    await TestBed.configureTestingModule({
      declarations: [InfoListComponent],
      imports: [CommonModule],
      providers: [{ provide: UpdateService, useValue: updateService }],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(InfoListComponent);
    component = fixture.componentInstance;
    component.appInfo = appInfo;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
