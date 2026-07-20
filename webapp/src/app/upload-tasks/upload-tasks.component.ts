import { Component, Optional } from '@angular/core';
import { ActivatedRoute } from '@angular/router';

import { RecordingSessionScope } from './shared/recording-session.model';

@Component({
  selector: 'app-upload-tasks',
  templateUrl: './upload-tasks.component.html',
  styleUrls: ['./upload-tasks.component.scss'],
})
export class UploadTasksComponent {
  readonly scope: RecordingSessionScope;

  constructor(@Optional() route: ActivatedRoute | null) {
    const configuredScope =
      route?.snapshot.data.sessionScope ??
      route?.parent?.snapshot.data.sessionScope;
    this.scope = configuredScope === 'recordings' ? 'recordings' : 'uploads';
  }
}
