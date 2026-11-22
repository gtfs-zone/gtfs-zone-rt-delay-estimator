/**
 * Scores replayed packages against the recorded ones with rt-accuracy's own
 * scorer. Run from a gtfs-zone-rt-accuracy checkout, so its node_modules and
 * src/metrics are used without adding files to it:
 *
 *   SCORE_OUT=out SCORE_FEED=columbia-county SCORE_PRODUCER=estimator-gps \
 *     node_modules/.bin/vitest run --globals \
 *     --root ../gtfs-zone-rt-delay-estimator/scripts score_replay
 *
 * SCORE_OUT is the replay_tides.py --out directory: it reads
 * source/<feed>/<date>/, <producer>/<feed>/<date>/ and gtfs/<feed>/*.zip.
 * SCORE_DATES (optional) is a comma list of dates to keep. SCORE_ACCURACY
 * points at the rt-accuracy checkout (default: the working directory).
 * Prints MAE, p90 and bias in minutes by horizon and route, and writes the
 * report to <out>/score-<feed>-<producer>.json.
 */
import { readdirSync, readFileSync, existsSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

type Row = Record<string, string | undefined>;

const accuracy = resolve(process.env.SCORE_ACCURACY ?? process.cwd());
const out = resolve(accuracy, process.env.SCORE_OUT ?? 'out');
const feed = process.env.SCORE_FEED ?? '';
const producer = process.env.SCORE_PRODUCER ?? 'estimator-gps';
const keep = process.env.SCORE_DATES?.split(',').filter(Boolean);

const req = createRequire(join(accuracy, 'package.json'));
const Papa = req('papaparse') as {
  parse<T>(s: string, o: object): { data: T[] };
};
const { unzipSync, strFromU8 } = req('fflate') as {
  unzipSync(d: Uint8Array, o?: object): Record<string, Uint8Array>;
  strFromU8(d: Uint8Array): string;
};

function parse(text: string): Row[] {
  return Papa.parse<Row>(text.replace(/^﻿/, ''), {
    header: true,
    skipEmptyLines: true,
  }).data;
}

function table(dir: string, name: string): Row[] {
  const path = join(dir, `${name}.csv`);
  return existsSync(path) ? parse(readFileSync(path, 'utf8')) : [];
}

function dates(dir: string): string[] {
  if (!existsSync(dir)) {
    return [];
  }
  return readdirSync(dir)
    .filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .filter((d) => !keep || keep.includes(d))
    .sort();
}

/** stop_id to [lat, lon] and the agency zone, over every snapshot. */
function gtfsStops(): { stops: Map<string, [number, number]>; tz: string } {
  const stops = new Map<string, [number, number]>();
  let tz = 'UTC';
  const dir = join(out, 'gtfs', feed);
  for (const name of existsSync(dir) ? readdirSync(dir) : []) {
    const files = unzipSync(readFileSync(join(dir, name)), {
      filter: (f: { name: string }) =>
        f.name === 'stops.txt' || f.name === 'agency.txt',
    });
    for (const r of parse(strFromU8(files['stops.txt']))) {
      if (r.stop_id && r.stop_lat && r.stop_lon) {
        stops.set(r.stop_id, [Number(r.stop_lat), Number(r.stop_lon)]);
      }
    }
    tz = parse(strFromU8(files['agency.txt']))[0]?.agency_timezone ?? tz;
  }
  return { stops, tz };
}

const min = (s: number | null) => (s === null ? '-' : (s / 60).toFixed(2));

test('score replay', async () => {
  const metrics = pathToFileURL(join(accuracy, 'src/metrics')).href;
  const { DatasetBuilder, TABLE_ORDER } = await import(`${metrics}/dataset.ts`);
  const { score } = await import(`${metrics}/score.ts`);

  const recorded = dates(join(out, 'source', feed));
  const replayed = new Set(dates(join(out, producer, feed)));
  const both = recorded.filter((d) => replayed.has(d));
  expect(both.length).toBeGreaterThan(0);

  const builder = new DatasetBuilder(false);
  for (const d of both) {
    for (const [dir, fixes] of [
      [join(out, 'source', feed, d), true],
      [join(out, producer, feed, d), false],
    ] as const) {
      builder.beginPackage('predictions');
      for (const name of TABLE_ORDER) {
        // Fixes come from the recorded package only, so the gps baseline
        // does not see each one twice.
        const rows =
          name === 'vehicle_locations' && !fixes ? [] : table(dir, name);
        builder.rows(name, rows);
      }
      builder.endPackage();
    }
  }
  const data = builder.finish();
  const { stops, tz } = gtfsStops();
  const report = score(data, { stops, timeZone: tz });

  const names: string[] = report.producers;
  const lines = [
    `${feed}: ${both[0]}..${both[both.length - 1]} (${both.length} days, ` +
      `${report.counts.samples} samples)`,
    '',
    `| Group | ${names.join(' | ')} |`,
    `| --- |${names.map(() => ' --- |').join('')}`,
  ];
  type Group = {
    key: string;
    producers: Record<
      string,
      { mae: number | null; p90: number | null; bias: number | null }
    >;
  };
  const row = (label: string, g: Group, f: 'mae' | 'p90' | 'bias') =>
    `| ${label} | ${names.map((n) => min(g.producers[n][f])).join(' | ')} |`;
  for (const f of ['mae', 'p90', 'bias'] as const) {
    lines.push(row(`${f} overall`, report.overall, f));
    for (const g of report.byHorizon) {
      lines.push(row(`${f} ${g.key}`, g, f));
    }
  }
  for (const g of report.byRoute) {
    lines.push(row(`mae route ${g.key}`, g, 'mae'));
  }
  console.log(lines.join('\n'));
  writeFileSync(
    join(out, `score-${feed}-${producer}.json`),
    JSON.stringify(report, null, 1)
  );
}, 600_000);
