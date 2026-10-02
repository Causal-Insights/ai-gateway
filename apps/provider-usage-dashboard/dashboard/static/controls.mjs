export function dateRange(preset, today = new Date()) {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  if (preset === '7' || preset === '30') start.setUTCDate(start.getUTCDate() - Number(preset) + 1);
  if (preset === 'month') start.setUTCDate(1);
  if (preset === 'previous') {
    end.setUTCDate(0);
    start.setUTCFullYear(end.getUTCFullYear(), end.getUTCMonth(), 1);
  }
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}

export function sortedRows(rows, key, direction, value) {
  return [...rows].sort((a, b) => {
    const av = value(a, key), bv = value(b, key);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    const compare = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv));
    return direction === 'asc' ? compare : -compare;
  });
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
