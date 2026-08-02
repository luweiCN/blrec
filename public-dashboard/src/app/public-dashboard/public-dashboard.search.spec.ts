import {
  HERO_IDENTITIES,
  heroDisplayName,
  heroSearchSegments,
} from './public-dashboard.hero-names';
import { matchesSearchSegments } from './public-dashboard.search';

describe('public dashboard segmented search', () => {
  it('contains the complete hero alias vocabulary', () => {
    expect(HERO_IDENTITIES.length).toBe(57);
    expect(heroDisplayName('Caine')).toBe('凯恩');
    expect(heroSearchSegments('SAW')).toContain('机枪');
  });

  it('matches Chinese, English, full pinyin and pinyin initials', () => {
    const segments = heroSearchSegments('Celeste');

    expect(matchesSearchSegments(segments, '星妈')).toBeTrue();
    expect(matchesSearchSegments(segments, 'cel')).toBeTrue();
    expect(matchesSearchSegments(segments, 'xingma')).toBeTrue();
    expect(matchesSearchSegments(segments, 'xm')).toBeTrue();
  });

  it('does not match across separate name segments', () => {
    expect(matchesSearchSegments(['Adagio', '鸟人'], 'agio鸟')).toBeFalse();
  });
});
