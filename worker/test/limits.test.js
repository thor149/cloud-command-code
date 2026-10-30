import test from 'node:test';
import assert from 'node:assert/strict';
import { usageReport, parseLimit, formatUsage, nextMonth } from '../src/limits.js';

const now = Date.UTC(2026, 9, 15, 12) / 1000;
const task = (minutes, finished_at, metered = true) => ({ minutes, finished_at, created_at: finished_at - 60, metered });

test('rolling boundaries, UTC calendar month, unmetered and unfinished exclusions', () => {
  const tasks = [task(1, now - 3599), task(2, now - 3600), task(4, now - 86399), task(8, now - 86400),
    task(16, Date.UTC(2026, 9, 1) / 1000), task(32, Date.UTC(2026, 8, 30, 23, 59, 59) / 1000),
    task(1000, now, false), task(1000, null), task(1000, now + 1)];
  const report = usageReport(tasks, { hourly: 1, daily: 7, monthly: 50 }, now);
  assert.equal(report.windows.hourly.used, 1); assert.equal(report.windows.hourly.blocked, true);
  assert.equal(report.windows.daily.used, 7); assert.equal(report.windows.daily.blocked, true);
  assert.equal(report.windows.monthly.used, 31); assert.equal(report.windows.monthly.percent, 62);
  assert.equal(report.windows.monthly.freesAt, Date.UTC(2026, 10, 1) / 1000);
});

test('free-up time waits until enough minutes expire to fall strictly below a limit', () => {
  const report = usageReport([task(2, now - 3000), task(8, now - 2000), task(5, now - 1000)], { hourly: 6, daily: null, monthly: null }, now);
  assert.equal(report.windows.hourly.freesAt, now + 1600);
  assert.equal(report.windows.daily.blocked, false); assert.equal(report.windows.daily.percent, null);
});

test('projection maths uses a 7 day denominator and the last hour pace', () => {
  const tasks = [task(70, now - 2 * 86400), task(10, now - 1800), task(10, now - 7200)];
  const report = usageReport(tasks, { hourly: 60, daily: 40, monthly: 180 }, now, now - 20 * 86400);
  assert.equal(report.dailyRate, 90 / 7); assert.equal(report.hourlyPace, 10);
  assert.equal(report.lastsDays, 7); assert.equal(report.runsOutAt, now + 7 * 86400);
  assert.equal(report.outlastsMonth, false); assert.equal(report.dailyHoursLeft, 2);
  assert.match(formatUsage(report), /monthly limit lasts until.*7.0 days/);
});

test('short history uses elapsed days with min 1; zero pace and unlimited projections are skipped', () => {
  const tasks = [task(20, now)];
  assert.equal(usageReport(tasks, {}, now, now - 3600).dailyRate, 20);
  assert.equal(usageReport(tasks, {}, now, now - 2 * 86400).dailyRate, 10);
  const report = usageReport([], { hourly: null, daily: null, monthly: 1800 }, now);
  assert.equal(report.dailyRate, 0); assert.equal(report.dailyHoursLeft, null); assert.equal(report.runsOutAt, null);
  assert.match(formatUsage(report), /won't run out this month/);
  assert.doesNotMatch(formatUsage(report), /last hour/);
});

test('UTC reset handles year/leap-month boundaries', () => {
  assert.equal(nextMonth(Date.UTC(2026, 11, 31, 23, 59) / 1000), Date.UTC(2027, 0, 1) / 1000);
  assert.equal(nextMonth(Date.UTC(2028, 1, 29) / 1000), Date.UTC(2028, 2, 1) / 1000);
  assert.deepEqual(parseLimit('daily off'), { window: 'daily', value: null });
});

test('already-exhausted monthly limit is reported even with zero recent pace', () => {
  const report = usageReport([task(50, now - 8 * 86400)], { monthly: 50 }, now);
  assert.equal(report.dailyRate, 0); assert.equal(report.lastsDays, 0);
  assert.equal(report.runsOutAt, now); assert.equal(report.outlastsMonth, false);
});
