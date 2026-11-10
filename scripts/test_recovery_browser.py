"""Real Chromium checks against a local HTTP server and temporary synthetic DBs.

Only responses are deliberately dropped. Original writes still use real HTTP
and SQLite. This is not production, PostgreSQL, cross-browser or load testing.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hotel_demo.server import DemoServer, DemoWorkspace
from playwright.sync_api import sync_playwright, expect


def run():
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        workspace = DemoWorkspace(Path(temporary)/'state')
        server = DemoServer(('127.0.0.1', 0), workspace)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'), headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage'])
                for case in ('committed_reserve', 'committed_cancel', 'rolled_back_reserve'):
                    context = browser.new_context(viewport={'width': 1440, 'height': 1080})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    expect(page.locator('#reserve-button')).to_be_enabled()
                    page.locator('#reset-button').click()
                    page.locator('#reset-dialog button[value=confirm]').click()
                    expect(page.locator('#metric-bookings')).to_have_text('0')
                    old = workspace.generation
                    page.locator('#baby-example').click()
                    if case == 'committed_cancel':
                        page.locator('#reserve-button').click()
                        expect(page.locator('#metric-bookings')).to_have_text('1')
                    if case == 'rolled_back_reserve':
                        page.locator('#simulate-failure').check()
                    operation = 'cancel' if case == 'committed_cancel' else 'reserve'
                    captured = {}
                    def drop_after_server_commit(route):
                        captured['payload'] = route.request.post_data_json
                        response = route.fetch()
                        captured['status'] = response.status
                        captured['receipt'] = response.json()
                        route.abort('failed')
                    page.route('**/api/' + operation, drop_after_server_commit)
                    if operation == 'cancel':
                        page.locator('.cancel-booking').click()
                    else:
                        page.locator('#reserve-button').click()
                    expect(page.locator('#pending-box')).to_be_visible()
                    expect(page.locator('#pending-title')).to_have_text('上次操作的结果尚未确认')
                    page.unroute('**/api/' + operation, drop_after_server_commit)
                    assert captured['status'] == 200
                    other = context.new_page()
                    other.goto(origin)
                    expect(other.locator('#reset-button')).to_be_enabled()
                    other.locator('#reset-button').click()
                    other.locator('#reset-dialog button[value=confirm]').click()
                    expect(other.locator('#metric-bookings')).to_have_text('0')
                    assert workspace.generation != old
                    current = workspace.generation
                    page.locator('#refresh-button').click()
                    expect(page.locator('#archived-box')).to_be_visible()
                    expect(page.locator('#pending-box')).to_be_hidden()
                    page.locator('#archived-box summary').first.click()
                    db = workspace.path/old/'upgrade.db'
                    def ledger():
                        with sqlite3.connect(db) as connection:
                            return '\n'.join(connection.iterdump())
                    before = ledger()
                    looked_up = []
                    page.on('request', lambda request: looked_up.append(request.url) if request.method == 'POST' else None)
                    page.locator('.archive-lookup').click()
                    expect(page.locator('.archive-result')).to_be_visible()
                    expect(page.locator('.archive-lookup')).to_be_enabled()
                    text = page.locator('.archive-result').inner_text()
                    if case == 'committed_reserve':
                        assert '预订成功' in text
                    elif case == 'committed_cancel':
                        assert '已取消' in text
                    else:
                        assert '未找到原请求记录' in text and '不证明请求从未执行' in text
                    assert looked_up == [origin+'/api/lookup'], looked_up
                    assert before == ledger()
                    assert workspace.generation == current
                    assert workspace.state('upgrade')['bookings'] == []
                    stored = page.evaluate("JSON.parse(sessionStorage.getItem('hotel-room-lab:retry:v1'))")
                    assert stored['archived'][0]['payload'] == captured['payload']
                    assert stored['archived'][0]['recovery']['read_only'] is True
                    page.reload()
                    expect(page.locator('#reserve-button')).to_be_enabled()
                    page.locator('#archived-box summary').first.click()
                    expect(page.locator('.archive-result')).to_be_visible()
                    page.screenshot(path='/tmp/hotel-recovery-browser-'+case+'.png', full_page=True)
                    assert errors == [], errors
                    results.append({'case': case, 'status': 'passed',
                        'response_dropped_after_real_server_result': captured['receipt']['status'],
                        'recovered_outcome': stored['archived'][0]['recovery']['outcome'],
                        'recovery_only_posted_lookup': True, 'archived_ledger_unchanged': True,
                        'current_round_unchanged': True, 'payload_survived_reload': True,
                        'javascript_errors': errors})
                    context.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
    print(json.dumps({'scope': 'Real headless Chromium, real local HTTP and SQLite, synthetic data; not production or performance',
        'cases': results}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
