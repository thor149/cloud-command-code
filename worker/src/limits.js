export const WINDOWS = ['hourly', 'daily', 'monthly'];
const DAY = 86400;
export const nextMonth = now => {
  const date = new Date(now * 1000);
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) / 1000;
};
export const monthStart = now => {
  const date = new Date(now * 1000);
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 1000;
};

export function parseLimit(args) {
  const parts = args.trim().split(/\s+/);
  if (parts.length !== 2 || !WINDOWS.includes(parts[0])) return null;
  if (parts[1].toLowerCase() === 'off') return { window: parts[0], value: null };
  if (!/^[1-9]\d*$/.test(parts[1]) || !Number.isSafeInteger(Number(parts[1]))) return null;
  return { window: parts[0], value: Number(parts[1]) };
}

// Time is unix seconds throughout. A task is charged at completion, including cancellation.
export function usageReport(tasks, limits, now, firstTaskAt = null) {
  const history = tasks.filter(t => t.metered && t.finished_at != null && t.finished_at <= now && t.minutes > 0);
  const starts = { hourly: now - 3600, daily: now - DAY, monthly: monthStart(now) };
  const windows = {};
  for (const name of WINDOWS) {
    const entries = history.filter(t => name === 'monthly' ? t.finished_at >= starts[name] : t.finished_at > starts[name])
      .sort((a, b) => a.finished_at - b.finished_at);
    const used = entries.reduce((sum, t) => sum + t.minutes, 0);
    const limit = limits[name] ?? null;
    // When blocked, find the expiry that actually brings usage BELOW the limit.
    let remaining = used;
    let freesAt = null;
    for (const task of entries) {
      remaining -= task.minutes;
      if (limit === null || used < limit || remaining < limit) {
        freesAt = task.finished_at + (name === 'hourly' ? 3600 : DAY);
        break;
      }
    }
    windows[name] = { used, limit, percent: limit === null ? null : used / limit * 100,
      freesAt: name === 'monthly' ? nextMonth(now) : freesAt, blocked: limit !== null && used >= limit };
  }
  const earliest = firstTaskAt ?? (history.length ? Math.min(...history.map(t => t.created_at ?? t.finished_at)) : now);
  const days = Math.min(7, Math.max(1, (now - earliest) / DAY));
  const dailyRate = history.filter(t => t.finished_at > now - 7 * DAY).reduce((sum, t) => sum + t.minutes, 0) / days;
  const monthlyRemaining = limits.monthly == null ? null : Math.max(0, limits.monthly - windows.monthly.used);
  const lastsDays = monthlyRemaining === 0 ? 0 : monthlyRemaining === null || dailyRate === 0 ? null : monthlyRemaining / dailyRate;
  const runsOutAt = lastsDays === null ? null : now + lastsDays * DAY;
  const hourlyPace = windows.hourly.used;
  return { windows, dailyRate, hourlyPace, lastsDays, runsOutAt,
    outlastsMonth: runsOutAt === null || runsOutAt >= nextMonth(now), monthResetsAt: nextMonth(now),
    dailyHoursLeft: limits.daily == null || hourlyPace === 0 ? null : Math.max(0, limits.daily - windows.daily.used) / hourlyPace };
}

export function formatTime(time, now) {
  const delta = time - now;
  if (Number.isFinite(delta) && delta >= 3600) return `in ${Math.ceil(delta / 3600)} h`;
  return `${new Date(time * 1000).toISOString().slice(11, 16)} UTC`;
}

export function formatUsage(report, now) {
  const lines = WINDOWS.map(name => {
    const w = report.windows[name];
    return `📊 <b>${name}</b> ${w.used} / ${w.limit ?? 'off'} min${w.percent === null ? '' : ` (${Math.round(w.percent)}%)`}${w.freesAt === null ? '' : `\n<b>${name === 'monthly' ? 'Resets' : 'Frees up'}</b> ${formatTime(w.freesAt, now)}`}`;
  });
  if (report.windows.monthly.limit !== null) {
    lines.push(`\n📈 <b>Monthly projection</b> At ~${report.dailyRate.toFixed(1)} min/day ${report.outlastsMonth ? "won't run out this month" : `the monthly limit lasts until ${formatTime(report.runsOutAt, now)} (${report.lastsDays.toFixed(1)} days)`}.`);
    lines.push(`<b>Month resets</b> ${formatTime(report.monthResetsAt, now)}`);
  }
  if (report.dailyHoursLeft !== null) lines.push(`📈 <b>Daily projection</b> At the last hour's pace (${report.hourlyPace} min/h) the daily limit is reached in ~${report.dailyHoursLeft.toFixed(1)} h.`);
  return lines.join('\n');
}
