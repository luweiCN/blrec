import { match } from 'pinyin-pro';

const HAN_CHARACTER = /[\u3400-\u9fff]/u;
const LATIN_QUERY = /^[a-z0-9]+$/;

export function normalizeSearchValue(value: string): string {
  return value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/ü/g, 'v')
    .replace(/[\s·._\-/—]+/g, '');
}

export function matchesSearchSegments(
  segments: readonly string[],
  query: string,
): boolean {
  const normalizedQuery = normalizeSearchValue(query.trim());
  if (normalizedQuery === '') {
    return true;
  }

  return segments.some((segment) => {
    if (normalizeSearchValue(segment).includes(normalizedQuery)) {
      return true;
    }
    if (!HAN_CHARACTER.test(segment) || !LATIN_QUERY.test(normalizedQuery)) {
      return false;
    }
    return (
      match(segment, normalizedQuery, {
        continuous: true,
        precision: 'any',
        v: true,
      }) !== null
    );
  });
}
