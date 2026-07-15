import { Injectable } from '@angular/core';

import * as QRCode from 'qrcode';

@Injectable({ providedIn: 'root' })
export class QrCodeRenderer {
  toDataUrl(value: string): Promise<string> {
    return QRCode.toDataURL(value, {
      errorCorrectionLevel: 'M',
      margin: 2,
      width: 256,
    });
  }
}
