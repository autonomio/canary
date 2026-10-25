"""The shipped menu renders honest auth states and uses only HTTP actions."""
from test_coverage_evidence_ui import run_dashboard


def test_auth_menu_states_and_api_actions(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const base={state:'signed_out',available:true,authenticated:false,login:null};
assert.match(authHtml(base),/Signed out/);
assert.match(authHtml(base),/data-auth="browser"/);
assert.match(authHtml(base),/data-auth="device"/);
const signedIn=authHtml({...base,state:'signed_in',authenticated:true});
assert.match(signedIn,/Model access is checked when research starts/);
assert.match(signedIn,/data-auth="logout"/);
assert.doesNotMatch(signedIn,/data-auth="browser"/);
assert.match(authHtml({...base,state:'unavailable',available:false,error:'Missing CLI'}),/Unavailable.*Missing CLI/s);
const pending={id:'flow-1',state:'pending',method:'device',url:'https://auth.openai.com/codex/device',user_code:'ABCD-1234'};
const challenge=authHtml({...base,login:pending});
assert.match(challenge,/ABCD-1234/);
assert.match(challenge,/rel="noopener noreferrer"/);
assert.match(challenge,/data-auth="cancel"/);
assert.doesNotMatch(authHtml({...base,login:{...pending,state:'complete',url:null,user_code:null}}),/ABCD-1234/);
assert.match(authHtml({...base,error:'<img onerror=x>'}),/&lt;img/);
(async()=>{
  const requests=[], el={innerHTML:'',querySelectorAll:()=>[]};
  global.document={getElementById:()=>el};
  global.fetch=async(path,opts)=>{
    requests.push([path,opts]);
    const data=path==='/api/auth/status'?base:{...pending,state:'complete',url:null,user_code:null};
    return {ok:true,json:async()=>data};
  };
  await authAction('device');
  assert.equal(requests[0][0],'/api/auth/login');
  assert.deepEqual(JSON.parse(requests[0][1].body),{method:'device'});
  await authAction('logout');
  assert.ok(requests.some(([path,opts])=>path==='/api/auth/logout' && opts.method==='POST'));
  assert.match(el.innerHTML,/Signed out/);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
