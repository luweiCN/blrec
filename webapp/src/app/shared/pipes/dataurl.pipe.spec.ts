import { DomSanitizer } from '@angular/platform-browser';

import { DataurlPipe } from './dataurl.pipe';

describe('DataurlPipe', () => {
  it('create an instance', () => {
    const domSanitizer = jasmine.createSpyObj<DomSanitizer>('DomSanitizer', [
      'bypassSecurityTrustUrl',
    ]);
    const pipe = new DataurlPipe(domSanitizer);

    expect(pipe).toBeTruthy();
  });
});
