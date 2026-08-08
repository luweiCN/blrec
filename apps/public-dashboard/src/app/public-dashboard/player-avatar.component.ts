import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
} from '@angular/core';

import { environment } from '../../environments/environment';
import { heroImage } from './public-dashboard.data';

type AvatarSource = 'player' | 'hero' | 'brand' | 'none';

@Component({
  selector: 'app-player-avatar',
  templateUrl: './player-avatar.component.html',
  styleUrls: ['./player-avatar.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerAvatarComponent implements OnChanges {
  @Input() playerId = 0;
  @Input() heroName = '';

  imageSource = '';
  source: AvatarSource = 'none';

  ngOnChanges(): void {
    if (Number.isSafeInteger(this.playerId) && this.playerId > 0) {
      const dataBaseUrl = environment.dataBaseUrl.replace(/\/+$/u, '');
      this.imageSource = `${dataBaseUrl}/avatars/${this.playerId}.jpg`;
      this.source = 'player';
      return;
    }
    this.useHeroOrBrand();
  }

  handleImageError(): void {
    if (this.source === 'player') {
      this.useHeroOrBrand();
      return;
    }
    if (this.source === 'hero') {
      this.imageSource = 'favicon.ico';
      this.source = 'brand';
      return;
    }
    this.imageSource = '';
    this.source = 'none';
  }

  private useHeroOrBrand(): void {
    if (this.heroName) {
      this.imageSource = heroImage(this.heroName);
      this.source = 'hero';
      return;
    }
    this.imageSource = 'favicon.ico';
    this.source = 'brand';
  }
}
