// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest';

import { parseRoomId } from '../src/shared/room';

function locationAt(pathname: string): Location {
  return { pathname } as Location;
}

describe('Bilibili room detection', () => {
  beforeEach(() => {
    document.head.innerHTML = '';
    document.body.innerHTML = '';
  });

  it('uses a numeric room path when no canonical page data exists', () => {
    expect(parseRoomId(locationAt('/22907214'), document)).toBe(22907214);
  });

  it('prefers the canonical room ID embedded for a short room number', () => {
    const script = document.createElement('script');
    script.textContent = `window.__NEPTUNE_IS_MY_WAIFU__ = ${JSON.stringify({
      roomInfoRes: { data: { room_info: { room_id: 3582149 } } },
    })};`;
    document.head.append(script);

    expect(parseRoomId(locationAt('/6'), document)).toBe(3582149);
  });

  it('returns null instead of guessing from an invalid path', () => {
    expect(parseRoomId(locationAt('/blackboard/activity-test'), document)).toBeNull();
  });
});
