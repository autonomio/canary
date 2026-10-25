"""Local dashboard keeps provider authentication in the API-backed CLI."""
from test_coverage_evidence_ui import UI, run_dashboard


def test_local_dashboard_has_no_provider_login_controls(tmp_path):
    html=UI.read_text()
    for removed in ('data-auth=', 'id="codex-auth"', '/api/auth/login', '/api/auth/logout', 'refreshAuth'):
        assert removed not in html
    assert 'Local footprint' in html
    run_dashboard(tmp_path,{},r'''
assert.equal(active({kind:'question',status:'activity_unverified'}),false);
assert.equal(statusInfo({status:'activity_unverified'})[1],'Activity unverified');
''')
