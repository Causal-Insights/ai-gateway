import test from 'node:test';
import assert from 'node:assert/strict';
import {dateRange, sortedRows, escapeHtml} from '../dashboard/static/controls.mjs';

test('presets use UTC and include the last day', () => {
  const today = new Date('2026-03-01T04:00:00Z');
  assert.deepEqual(dateRange('today',today),['2026-03-01','2026-03-01']);
  assert.deepEqual(dateRange('7',today),['2026-02-23','2026-03-01']);
  assert.deepEqual(dateRange('30',today),['2026-01-31','2026-03-01']);
  assert.deepEqual(dateRange('month',today),['2026-03-01','2026-03-01']);
  assert.deepEqual(dateRange('previous',today),['2026-02-01','2026-02-28']);
  assert.deepEqual(dateRange('previous',new Date('2026-01-31Z')),['2025-12-01','2025-12-31']);
});
test('missing values stay last in either sort direction and zero is real', () => {
  const rows = [{n:20},{n:null},{n:0},{n:3}];
  const value = r => r.n;
  assert.deepEqual(sortedRows(rows,'n','desc',value).map(r=>r.n),[20,3,0,null]);
  assert.deepEqual(sortedRows(rows,'n','asc',value).map(r=>r.n),[0,3,20,null]);
});
test('provider/model sorting is alphabetical', () => {
  assert.deepEqual(sortedRows([{n:'xAI'},{n:'OpenAI'}],'n','asc',r=>r.n).map(r=>r.n),['OpenAI','xAI']);
});
test('provider-controlled text cannot inject markup', () => {
  assert.equal(escapeHtml('<img src=x onerror="attack()">'), '&lt;img src=x onerror=&quot;attack()&quot;&gt;');
});
