import { describe, it, expect } from 'vitest';
import { parseVCard, generateVCard, type VCard } from './vcard-parser';
import { toVCard, fromVCard, serializeContact, deserializeContact, type Contact } from '../models/contact';

// Yearless and text birthdays across parse -> model -> generate (repair plan v2,
// D1-D4). Synthetic cards only. Assertion messages starting with BEHAVIOR are
// the regression; FIXTURE marks a broken harness, never a reproduction.
// The text flag is optional on both types; the intersections keep this file
// type-correct before and after the field exists.
type FlaggedVCard = VCard & { bdayIsText?: boolean };
type FlaggedContact = Contact & { birthdayIsText?: boolean };

const card = (version: '3.0' | '4.0', ...lines: string[]) =>
  ['BEGIN:VCARD', `VERSION:${version}`, 'UID:synthetic-bday', 'FN:Synthetic Person', 'N:Person;Synthetic;;;', ...lines, 'END:VCARD'].join('\r\n');

const bdayLines = (wire: string) => wire.split('\r\n').filter((line) => /^BDAY[;:]/i.test(line));
const apple = (monthDay: string) => `BDAY;X-APPLE-OMIT-YEAR=1604:1604-${monthDay}`;

function parsed(wire: string): FlaggedVCard {
  const vcard = parseVCard(wire) as FlaggedVCard;
  if (vcard.uid !== 'synthetic-bday') throw new Error('FIXTURE: synthetic card did not parse');
  return vcard;
}

function contactOf(birthday: string | null, birthdayIsText?: boolean): FlaggedContact {
  const at = new Date('2026-10-25T00:00:00Z');
  const contact: FlaggedContact = {
    id: 'synthetic-bday', uid: 'synthetic-bday', displayName: 'Synthetic Person',
    name: { prefix: '', given: 'Synthetic', family: 'Person', suffix: '' },
    phones: [], emails: [], addresses: [], organization: '', title: '', notes: '',
    birthday, photoUrl: null, categories: [], favorite: false, created_at: at, updated_at: at,
  };
  if (birthdayIsText !== undefined) contact.birthdayIsText = birthdayIsText;
  return contact;
}

describe('yearless date birthdays', () => {
  const inputs: Array<[string, '3.0' | '4.0', string]> = [
    ['vCard 4 basic', '4.0', 'BDAY:--0416'],
    ['vCard 4 extended', '4.0', 'BDAY:--04-16'],
    ['legacy vCard 3 basic (old web writes)', '3.0', 'BDAY:--0416'],
    ['legacy vCard 3 extended', '3.0', 'BDAY:--04-16'],
    ['vCard 4 VALUE=date', '4.0', 'BDAY;VALUE=date:--0416'],
    ['vCard 4 VALUE=date-and-or-time', '4.0', 'BDAY;VALUE=date-and-or-time:--04-16'],
  ];

  for (const [label, version, line] of inputs) {
    it(`normalizes ${label} to --MM-DD and writes the marked 1604 form`, () => {
      const vcard = parsed(card(version, line));
      expect(vcard.bday, 'BEHAVIOR: yearless model value').toBe('--04-16');
      expect(vcard.bdayIsText === true, 'BEHAVIOR: a date is not text').toBe(false);
      const out = generateVCard(vcard);
      expect(bdayLines(out), 'BEHAVIOR: vCard 3 output carries the omit-year marker').toEqual([apple('04-16')]);
      expect(out, 'BEHAVIOR: no bare partial date under VERSION:3.0').not.toMatch(/^BDAY:--/m);
    });
  }

  it('accepts an explicit Apple marker in extended, basic and lowercase-parameter forms', () => {
    for (const line of [
      'BDAY;X-APPLE-OMIT-YEAR=1604:1604-04-16',
      'BDAY;X-APPLE-OMIT-YEAR=1604:16040416',
      'BDAY;x-apple-omit-year=1604:1604-04-16',
    ]) {
      const vcard = parsed(card('3.0', line));
      expect(vcard.bday, `BEHAVIOR: ${line} keeps no year`).toBe('--04-16');
      expect(bdayLines(generateVCard(vcard)), `BEHAVIOR: ${line} output`).toEqual([apple('04-16')]);
    }
  });

  it('treats any matching marker year as a sentinel and always writes 1604', () => {
    const vcard = parsed(card('3.0', 'BDAY;X-APPLE-OMIT-YEAR=1900:1900-04-16'));
    expect(vcard.bday, 'BEHAVIOR: matched sentinel is not a birth year').toBe('--04-16');
    expect(bdayLines(generateVCard(vcard)), 'BEHAVIOR: canonical sentinel').toEqual([apple('04-16')]);
  });

  it('keeps a leap-day birthday without a year', () => {
    for (const line of ['BDAY:--0229', 'BDAY:--02-29', 'BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-29']) {
      const vcard = parsed(card(line.includes('APPLE') ? '3.0' : '4.0', line));
      expect(vcard.bday, `BEHAVIOR: ${line} leap day`).toBe('--02-29');
      expect(bdayLines(generateVCard(vcard)), `BEHAVIOR: ${line} leap day output`).toEqual([apple('02-29')]);
    }
  });

  it('is byte-stable across repeated parse and generate', () => {
    const first = generateVCard(parsed(card('4.0', 'BDAY:--0416')));
    const second = generateVCard(parsed(first));
    const third = generateVCard(parsed(second));
    expect(bdayLines(second), 'BEHAVIOR: marked form reparses to the same line').toEqual([apple('04-16')]);
    expect(third, 'BEHAVIOR: repeated serialization is stable').toBe(second);
  });
});

describe('controls that must not be normalized', () => {
  it('keeps a full date when the marker year does not match', () => {
    const vcard = parsed(card('3.0', 'BDAY;X-APPLE-OMIT-YEAR=1604:1990-04-16'));
    expect(vcard.bday, 'BEHAVIOR: mismatched marker keeps the full date').toBe('1990-04-16');
    expect(bdayLines(generateVCard(vcard)), 'BEHAVIOR: no marker invented').toEqual(['BDAY:1990-04-16']);
  });

  it('passes invalid or non month-day values through unchanged (characterization)', () => {
    for (const value of ['--0230', '--0431', '--1301', '--0016', '--04', '---16', '2023-02-30', '--04-1']) {
      const vcard = parsed(card('4.0', `BDAY:${value}`));
      expect(vcard.bday, `BEHAVIOR: ${value} is not rewritten`).toBe(value);
      expect(bdayLines(generateVCard(vcard)), `BEHAVIOR: ${value} output`).toEqual([`BDAY:${value}`]);
    }
  });

  it('does not treat an invalid marked date as yearless', () => {
    const vcard = parsed(card('3.0', 'BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-30'));
    expect(vcard.bday, 'BEHAVIOR: invalid marked date passes through').toBe('1604-02-30');
  });

  it('keeps fully dated vCard 3 and vCard 4 birthdays byte-identical', () => {
    for (const [version, value] of [
      ['3.0', '1990-04-16'], ['4.0', '1990-04-16'], ['3.0', '19900416'], ['4.0', '19900416'], ['4.0', '1990-04-16T10:00:00Z'],
    ] as const) {
      const out = generateVCard(parsed(card(version, `BDAY:${value}`)));
      expect(bdayLines(out), `BEHAVIOR: full date ${value}`).toEqual([`BDAY:${value}`]);
      expect(generateVCard(parsed(out)), `BEHAVIOR: full date ${value} stable`).toBe(out);
    }
  });

  it('writes no BDAY line when there is no birthday, even if a text flag is set', () => {
    expect(bdayLines(generateVCard(parsed(card('3.0')))), 'BEHAVIOR: no BDAY').toEqual([]);
    const flagged: FlaggedVCard = { ...parsed(card('3.0')), bdayIsText: true };
    expect(bdayLines(generateVCard(flagged)), 'BEHAVIOR: flag alone writes nothing').toEqual([]);
    expect(bdayLines(toVCard(contactOf(null, true))), 'BEHAVIOR: null birthday writes nothing').toEqual([]);
  });
});

describe('text birthdays keep their type', () => {
  const textInputs: Array<[string, '3.0' | '4.0', string, string]> = [
    ['date-shaped basic', '4.0', 'BDAY;VALUE=text:--0416', '--0416'],
    ['date-shaped extended', '4.0', 'BDAY;VALUE=text:--04-16', '--04-16'],
    ['uppercase VALUE=TEXT under vCard 3', '3.0', 'BDAY;VALUE=TEXT:--0416', '--0416'],
    ['ordinary text', '4.0', 'BDAY;VALUE=text:circa 1800', 'circa 1800'],
  ];

  for (const [label, version, line, value] of textInputs) {
    it(`keeps ${label} as text through repeated serialization and reload`, () => {
      const vcard = parsed(card(version, line));
      expect(vcard.bday, 'BEHAVIOR: text string unchanged').toBe(value);
      expect(vcard.bdayIsText, 'BEHAVIOR: text type recorded').toBe(true);
      const first = generateVCard(vcard);
      expect(bdayLines(first), 'BEHAVIOR: text is written as text, never as a date').toEqual([`BDAY;VALUE=text:${value}`]);
      expect(first, 'BEHAVIOR: text is never converted to the marked date').not.toMatch(/X-APPLE-OMIT-YEAR/i);
      const second = generateVCard(parsed(first));
      expect(second, 'BEHAVIOR: second serialization identical').toBe(first);
      expect(generateVCard(parsed(second)), 'BEHAVIOR: third serialization identical').toBe(first);

      const contact = fromVCard(first) as FlaggedContact;
      expect([contact.birthday, contact.birthdayIsText], 'BEHAVIOR: model keeps string and type').toEqual([value, true]);
      const reloaded = deserializeContact(serializeContact(contact)) as FlaggedContact;
      expect([reloaded.birthday, reloaded.birthdayIsText], 'BEHAVIOR: cache-equivalent reload keeps type').toEqual([value, true]);
    });
  }

  it('drops other BDAY parameters, which are not preserved (characterization)', () => {
    const out = generateVCard(parsed(card('4.0', 'BDAY;VALUE=text;LANGUAGE=en:circa 1800')));
    expect(bdayLines(out), 'BEHAVIOR: only the text type is kept').toEqual(['BDAY;VALUE=text:circa 1800']);
  });
});

describe('contact model boundary', () => {
  it('maps a yearless date without a text flag and writes the marked form', () => {
    const contact = fromVCard(card('4.0', 'BDAY:--0416')) as FlaggedContact;
    expect(contact.birthday, 'BEHAVIOR: model value').toBe('--04-16');
    expect(contact.birthdayIsText === true, 'BEHAVIOR: no text flag').toBe(false);
    expect(bdayLines(serializeContact(contact)), 'BEHAVIOR: serialized form').toEqual([apple('04-16')]);
  });

  it('interprets existing models without the flag as dates, including older basic values', () => {
    for (const value of ['--0416', '--04-16']) {
      expect(bdayLines(toVCard(contactOf(value))), `BEHAVIOR: unflagged ${value}`).toEqual([apple('04-16')]);
    }
    expect(bdayLines(toVCard(contactOf('--0416', false))), 'BEHAVIOR: explicit false is a date').toEqual([apple('04-16')]);
    expect(bdayLines(toVCard(contactOf('1990-04-16'))), 'BEHAVIOR: unflagged full date').toEqual(['BDAY:1990-04-16']);
  });

  it('writes a text-flagged model as text', () => {
    expect(bdayLines(toVCard(contactOf('--0416', true))), 'BEHAVIOR: flagged text').toEqual(['BDAY;VALUE=text:--0416']);
  });

  it('rewrites legacy bare partial content into the marked form on the next save', () => {
    const legacy = card('3.0', 'BDAY:--0416');
    const reloaded = deserializeContact(legacy) as FlaggedContact;
    const saved = serializeContact(reloaded);
    expect(bdayLines(saved), 'BEHAVIOR: legacy content migrates on save').toEqual([apple('04-16')]);
    expect(serializeContact(deserializeContact(saved)), 'BEHAVIOR: post-fix content is witness-stable').toBe(saved);
  });
});
