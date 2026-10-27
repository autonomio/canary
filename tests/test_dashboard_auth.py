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


def test_connection_receipt_is_read_only_and_does_not_certify_model_access(tmp_path):
    run_dashboard(tmp_path,{},r'''
const c={auth:{authenticated:true,available:true,mode:'chatgpt'},cli_version:'codex 0.162.0',credential_store:'keyring',model_access:'Checked when research starts.'};
assert.match(connectionHtml(c),/Connected via ChatGPT/);
assert.match(connectionHtml(c),/keyring credentials/);
assert.match(connectionHtml(c),/Research runs on the Canary host/);
assert.match(connectionHtml(c),/Checked when research starts/);
assert.doesNotMatch(connectionHtml(c),/<button|data-auth/);
c.auth={authenticated:false,available:true};
assert.match(connectionHtml(c),/Codex signed out/);
c.auth={authenticated:false,available:false,error:'<unsafe>'};
assert.match(connectionHtml(c),/Codex unavailable/);
assert.match(connectionHtml(c),/&lt;unsafe&gt;/);
''')
