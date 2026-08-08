export function parseRoomId(
  location: Pick<Location, 'pathname'>,
  document: Document
): number | null {
  const embedded = embeddedRoomId(document);
  if (embedded !== null) {
    return embedded;
  }
  const dataRoomId = positiveInteger(
    document.querySelector<HTMLElement>('[data-room-id]')?.dataset['roomId']
  );
  if (dataRoomId !== null) {
    return dataRoomId;
  }
  const match = /^\/(\d+)(?:\/|$)/.exec(location.pathname);
  return positiveInteger(match?.[1]);
}

function embeddedRoomId(document: Document): number | null {
  for (const script of Array.from(document.scripts)) {
    const text = script.textContent ?? '';
    const documents: unknown[] = [];
    if (script.type === 'application/json') {
      try {
        documents.push(JSON.parse(text) as unknown);
      } catch (_error) {
        // Ignore unrelated JSON scripts that are still being written.
      }
    }
    for (const marker of [
      '__NEPTUNE_IS_MY_WAIFU__',
      '__INITIAL_STATE__',
      '__NEXT_DATA__',
    ]) {
      const markerIndex = text.indexOf(marker);
      if (markerIndex < 0) {
        continue;
      }
      const json = objectLiteral(text, markerIndex + marker.length);
      if (json) {
        try {
          documents.push(JSON.parse(json) as unknown);
        } catch (_error) {
          // Ignore a state script until Bilibili finishes replacing it.
        }
      }
    }
    for (const value of documents) {
      const roomId = roomIdFromState(value);
      if (roomId !== null) {
        return roomId;
      }
    }
  }
  return null;
}

function roomIdFromState(value: unknown): number | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const state = value as Record<string, unknown>;
  const candidates = [
    path(state, ['roomInfoRes', 'data', 'room_info', 'room_id']),
    path(state, ['roomInitRes', 'data', 'room_id']),
    path(state, ['roomInfo', 'room_id']),
    path(state, ['roomInfo', 'roomId']),
    state['room_id'],
    state['roomId'],
  ];
  for (const candidate of candidates) {
    const roomId = positiveInteger(candidate);
    if (roomId !== null) {
      return roomId;
    }
  }
  return null;
}

function path(value: Record<string, unknown>, keys: readonly string[]): unknown {
  let current: unknown = value;
  for (const key of keys) {
    if (typeof current !== 'object' || current === null) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function positiveInteger(value: unknown): number | null {
  const parsed =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && /^\d+$/.test(value)
      ? Number(value)
      : Number.NaN;
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function objectLiteral(text: string, startAt: number): string | null {
  const start = text.indexOf('{', startAt);
  if (start < 0) {
    return null;
  }
  let depth = 0;
  let quoted = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (escaped) {
        escaped = false;
      } else if (character === '\\') {
        escaped = true;
      } else if (character === '"') {
        quoted = false;
      }
      continue;
    }
    if (character === '"') {
      quoted = true;
    } else if (character === '{') {
      depth += 1;
    } else if (character === '}') {
      depth -= 1;
      if (depth === 0) {
        return text.slice(start, index + 1);
      }
    }
  }
  return null;
}
