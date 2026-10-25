"""Published HTTP contract and MCP operation metadata for the same service."""
from copy import deepcopy
import types
import typing

from . import coverage_protocol as protocol
from .claim_freshness import schema as freshness_schema
from .codex_auth import AuthStatus, Login
from .research_spec import CompiledResearch
from .research_bundle import ResearchBundle
from .service_status import ServiceStatus

S = {'type':'string'}
I = {'type':'integer'}
B = {'type':'boolean'}
O = {'type':'object','additionalProperties':True}


def obj(properties, required=(), *, closed=True):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':not closed}


def arr(items):
    return {'type':'array','items':items}


def ref(name):
    return {'$ref':'#/components/schemas/'+name}


def nullable(schema):
    return {'anyOf':[schema,{'type':'null'}]}


def typed(annotation):
    """Reuse existing TypedDict response records, rather than a second DTO layer."""
    if typing.is_typeddict(annotation):
        fields = typing.get_type_hints(annotation)
        return obj({k:typed(v) for k,v in fields.items()}, [k for k in fields if k in annotation.__required_keys__])
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType): return {'anyOf':[typed(a) for a in args]}
    if origin is typing.Literal: return {'enum':list(args)}
    if origin is list: return arr(typed(args[0]))
    if origin is dict or annotation is dict: return deepcopy(O)
    return {str:S,int:I,bool:B,float:{'type':'number'},type(None):{'type':'null'}}.get(annotation,{})


def schemas():
    fixed = lambda value: {'type':'string','const':value}
    declaration = obj({'version':{'type':'integer','const':1},'question':dict(S,minLength=1,maxLength=20000),
        'research':obj({'model':fixed(protocol.RESEARCH_MODEL),'effort':fixed(protocol.RESEARCH_EFFORT)}),
        'audit':obj({'model':fixed(protocol.AUDIT_MODEL),'effort':fixed(protocol.AUDIT_EFFORT),
                     'budget_seconds':dict(I,minimum=1,default=600)}),
        'retrieval':obj({'selection':fixed('coverage'),'target_papers':dict(I,minimum=1,default=50),
                         'corpus':fixed('fresh'),'access':fixed('open_full_text')}),
        'subagents':{'type':'boolean','const':False}}, ['question'])
    native = obj({'yaml':dict(S,minLength=1),'spec':ref('ResearchDeclaration'),'name':S,'brief':S})
    native['oneOf'] = [{'required':['yaml']},{'required':['spec']}]
    compiled = typed(CompiledResearch);compiled['additionalProperties'] = True
    compiled['properties']['resolved'] = ref('ResearchDeclaration')
    manifest = typed(ResearchBundle)
    manifest['properties'].update({'specification':nullable(ref('CompiledResearch')),
        'artifacts':arr(obj({'path':S,'bytes':dict(I,minimum=0),'sha256':dict(S,pattern='^[a-f0-9]{64}$')},['path','bytes','sha256']))})
    generated = protocol.investigation_schema()
    claim = deepcopy(generated['properties']['claims']['items'])
    claim.update(required=['id'],additionalProperties=True)
    # Recorded legacy/corrupt values remain visible with binding diagnostics.
    for field in ('uncertainty','freshness'): claim['properties'][field] = {}
    claim['description'] = 'Recorded claim. New generation follows GeneratedInvestigation; old values and binding failures are preserved.'
    evidence = obj({'id':S,'origin_evidence_id':S,'paragraph_id':nullable(S),'quote':S,
        'interpretation':S,'relation':S,'passage_ids':arr(S),'verbatim_status':S},['id'],closed=False)
    passage = obj({'id':S,'source_id':S,'reference_id':S,'kind':S,'text':S,'start':I,'end':I,
                   'page':nullable(I),'selected':B},['id'],closed=False)
    findings = obj({'question':S,'executive_summary':arr({}),'claims':arr(ref('RecordedClaim')),
        'evidence':arr(ref('RecordedEvidence')),'passages':arr(ref('Passage')),'references':arr(O),
        'gaps':arr(O),'residue':arr(O),'contradictions':arr(O),'study_results':arr(O),
        'caveats':arr(S),'next_questions':arr(O),'binding_issues':arr(O)},closed=False)
    research = obj({'kind':S,'answer':ref('Findings'),'run':O,'peer_review':O,'query_net':O,
        'access':O,'profile':O,'coverage':O,'paper_selection':O,'usage':O,'synthesis_md':nullable(S),
        'seed':S,'stopped':S,'model':nullable(S),'papers':arr(O),'iterations':arr(O)},closed=False)
    run = obj({'key':S,'name':S,'brief':S,'kind':S,'status':nullable(S),'live':B,'can_cancel':B,
        'started_at':nullable(S),'ended_at':nullable(S),'duration_seconds':nullable({'type':'number'}),
        'answered':nullable(B),'papers_covered':nullable(I),'input_tokens':nullable(I),
        'output_tokens':nullable(I),'cached_tokens':nullable(I),'usage':O,'research_summary':O},['key'],closed=False)
    run['properties']['input_tokens']['description'] = 'Total input, including cached input. Null means unavailable.'
    run['properties']['cached_tokens']['description'] = 'Subset of input tokens; do not add to total input. Null means unavailable.'
    run['properties']['output_tokens']['description'] = 'Total output, including reasoning where the native provider reports it. Null means unavailable.'
    context = obj({'source_id':S,'reference':O,'paragraphs':arr(ref('Passage')),'page':nullable(I),
        'local_file_url':nullable(S),'original_url':nullable(S),'requested_scope':S,'scope':S,
        'context_label':S,'text_available':B,'text_complete':nullable(B),'offset_verified':B,'warning':S},
        ['source_id','reference','paragraphs','text_available','offset_verified','warning'],closed=False)
    named_findings = {name:generated['properties'][field]['items'] for name,field in (
        ('GeneratedClaim','claims'),('GeneratedEvidence','evidence'),('Gap','gaps'),('Residue','residue'),
        ('StudyResult','study_results'),('Contradiction','contradictions'),('NextQuestion','next_questions'))}
    return {**named_findings,'Object':deepcopy(O),'Error':obj({'error':S,'code':S,'issues':arr(O)},['error'],closed=False),
        'ResearchDeclaration':declaration,'NativeSubmission':native,
        'CompileRequest':dict(native,properties={k:v for k,v in native['properties'].items() if k in ('yaml','spec')}),
        'RunSubmission':{'anyOf':[ref('NativeSubmission'),
            obj({'kind':fixed('question'),'question':S,'name':S,'brief':S},['kind','question'],closed=False),
            obj({'name':S,'brief':S,'argv':arr(S),'env':obj({name:S for name in ('CANARY_STAGE','CANARY_TIMEOUT_S','CANARY_WHEELS')})},['name','argv'],closed=False)]},
        'CompiledResearch':compiled,'BundleManifest':manifest,'AuthStatus':typed(AuthStatus),'Login':typed(Login),
        'ServiceStatus':typed(ServiceStatus),'Run':run,'Runs':obj({'runs':arr(ref('Run'))},['runs']),
        'RunStarted':obj({'key':S,'bundle':S,'spec_sha256':S,'bundle_url':S},['key'],closed=False),
        'RunControl':obj({'key':S,'status':nullable(S)},['key','status'],closed=False),
        'Research':research,'ResearchResponse':obj({'key':S,'live':B,'research':ref('Research')},['key','live','research']),
        'BundleResponse':obj({'key':S,'live':B,'bundle':ref('BundleManifest'),'research':ref('Research')},['key','live','bundle','research']),
        'Findings':findings,'RecordedClaim':claim,'RecordedEvidence':evidence,'Passage':passage,
        'SourceContext':context,'GeneratedInvestigation':generated,'QuestionPlan':protocol.question_plan_schema(),
        'ScientificAudit':protocol.audit_schema(),'Freshness':freshness_schema(),
        'ImportRequest':obj({'server_path':S,'name':S,'brief':S},['server_path']),
        'ActionRequest':obj({'command':{'enum':['review','analyze','cycle','inspect','resume','assess','revise','publish']},'options':O},['command','options']),
        'InputReceipt':obj({'id':S,'bytes':I},['id','bytes']),
        'Log':obj({'runs':arr(ref('Run'))},['runs']),
        'LoginRequest':obj({'method':{'type':'string','enum':['browser','device'],'default':'browser'}}),
        'DiscoverRequest':obj({'plan':ref('QuestionPlan'),'pages':dict(I,minimum=1,default=5)},['plan']),
        'AcquireRequest':obj({'ids':arr(S),'workers':dict(I,minimum=1,maximum=32,default=12),'retry_failed':B},['ids']),
        'ReadSourceRequest':obj({'reference_id':S,'match':nullable(S),'page':nullable(dict(I,minimum=1)),
                                 'context':dict(I,minimum=0,default=2),'limit':dict(I,minimum=1,default=8)},['reference_id']),
        'FinalizeRequest':obj({'investigation':ref('GeneratedInvestigation')})}


def document():
    result = {'openapi':'3.1.0','info':{'title':'Canary research API','version':'1.0.0',
        'description':'Owner-operated research service. Submission is asynchronous; execution, scientific audit and source binding are separate. Unknown/legacy values remain explicit.'},
        'servers':[{'url':'/'}],'paths':{},'components':{'schemas':schemas()}}
    def op(identifier, method, path, summary, response='Object', *, body=None, params=(), status=200,
           mcp=False, readonly=None, media='application/json', mcp_body=None):
        operation = {'operationId':identifier,'summary':summary,'responses':{
            str(status):{'description':'Result','content':{media:{'schema':ref(response) if media=='application/json' else {'type':'string','format':'binary'}}}},
            'default':{'description':'Error; unavailable/partial state is never acceptance','content':{'application/json':{'schema':ref('Error')}}}}}
        parameters = []
        for name, location, schema, required in params:
            parameters.append({'name':name,'in':location,'required':required,'schema':schema})
        if parameters: operation['parameters'] = parameters
        if body:
            operation['requestBody'] = {'required':True,'content':{'application/json':{'schema':ref(body)}}}
        if mcp:
            operation['x-canary-mcp'] = {'name':'canary_'+identifier,'readOnly':readonly if readonly is not None else method=='get'}
            if mcp_body: operation['x-canary-mcp']['bodySchema'] = ref(mcp_body)
        result['paths'].setdefault(path,{})[method] = operation
    key = ('key','path',S,True)
    source = ('source_id','path',S,True)
    op('status','get','/api/status','Inspect service, storage and extraction; not a model-access certificate','ServiceStatus',mcp=True)
    op('auth_status','get','/api/auth/status','Inspect the service-owned Codex login','AuthStatus',mcp=True)
    op('auth_login','post','/api/auth/login','Start native Codex login; a human completes the challenge','Login',body='LoginRequest',mcp=True)
    op('auth_progress','get','/api/auth/login/{id}','Read pending native login','Login',params=[('id','path',S,True)],mcp=True)
    op('auth_cancel','post','/api/auth/login/{id}/cancel','Cancel pending native login','Login',body='Object',params=[('id','path',S,True)],mcp=True)
    op('auth_logout','post','/api/auth/logout','Sign out of the shared native credential context','AuthStatus',body='Object',mcp=True)
    op('compile','post','/api/runs/compile','Compile only; no search or scientific inference','CompiledResearch',body='CompileRequest',mcp=True,readonly=True)
    op('ask','post','/api/runs','Submit research and return its run key','RunStarted',body='RunSubmission',status=201,mcp=True,mcp_body='NativeSubmission')
    op('runs','get','/api/runs','List registered runs and honest live state','Runs',mcp=True)
    op('run','get','/api/runs/{key}','Inspect one run and its available controls','Run',params=[key],mcp=True)
    op('cancel','post','/api/runs/{key}/cancel','Stop native worker/tools; preserve committed evidence','RunControl',params=[key],body='Object',status=202,mcp=True)
    op('rerun','post','/api/runs/{key}/rerun','Repeat saved settings in a new workspace','RunStarted',params=[key],body='Object',status=201,mcp=True)
    for verb in ('pause','unpause'):
        op(verb,'post','/api/runs/{key}/'+verb,'Retained container control; native workers use cancel','RunControl',params=[key],body='Object')
    op('research','get','/api/runs/{key}/research','Read findings, binding issues, coverage and separate audit','ResearchResponse',params=[key],mcp=True)
    op('bundle','get','/api/runs/{key}/bundle','Read bundle settings, artifact hashes and findings','BundleResponse',params=[key],mcp=True)
    op('archive','get','/api/runs/{key}/bundle/archive','Download a finished bundle with exact saved materials',params=[key],media='application/zip')
    op('artifact','get','/api/runs/{key}/bundle/artifacts/{artifact_path}','Download one hash-checked inventoried artifact',params=[key,('artifact_path','path',S,True)],media='application/octet-stream')
    op('export','get','/api/runs/{key}/exports/{connector}','Normalize a bundle; destination admission remains separate',params=[key,('connector','path',{'enum':['resolvent']},True)],mcp=True)
    op('source_context','get','/api/runs/{key}/sources/{source_id}/context','Read exact passages and broader saved section or paper','SourceContext',
       params=[key,source,('paragraph_id','query',S,False),('scope','query',{'enum':['section','paper'],'default':'section'},False)],mcp=True)
    op('source_file','get','/api/runs/{key}/sources/{source_id}/file','Download the saved original source; never an abstract substitute',params=[key,source],media='application/octet-stream')
    op('run_log','get','/api/runs/{key}/log','Read recorded progress and launcher output',params=[key,('tail','query',dict(I,minimum=1,default=200),False),('stream','query',{'enum':['all','timeline','console']},False)],mcp=True)
    op('log','get','/api/log','Read run times, coverage, answer availability and token accounting','Log',mcp=True)
    op('usage','get','/api/usage','Read recorded usage; missing values remain unknown',mcp=True)
    op('thread_usage','get','/api/usage/threads/{thread_id}','Read native usage in the service credential context',params=[('thread_id','path',S,True)],mcp=True)
    op('openalex_status','get','/api/sources/openalex/status','Read source search counts and known provider quota',mcp=True)
    op('import_run','post','/api/runs/import','Register an explicit service-host bundle directory','Run',body='ImportRequest',status=201)
    result['paths']['/api/runs/import']['post']['responses']['200'] = deepcopy(result['paths']['/api/runs/import']['post']['responses']['201'])
    op('action','post','/api/actions','Run a retained compatibility workflow','RunStarted',body='ActionRequest',status=201)
    result['paths']['/api/actions']['post']['responses']['200'] = {'description':'Synchronous inspection result','content':{'application/json':{'schema':ref('Object')}}}
    op('input','post','/api/inputs','Upload an input body; returned ID is used by actions','InputReceipt',status=201)
    result['paths']['/api/inputs']['post']['requestBody'] = {'required':True,'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}}
    result['paths']['/api/inputs']['post']['parameters'] = [{'in':'header','name':'Content-Length','required':True,'schema':dict(I,minimum=1)}]
    source_content = result['paths']['/api/runs/{key}/sources/{source_id}/file']['get']['responses']['200']['content']
    for media in ('application/pdf','application/xml','text/html','text/plain'):
        source_content[media] = deepcopy(source_content['application/octet-stream'])
    for action, body, response in (('discover','DiscoverRequest','Object'),('acquire','AcquireRequest','Object'),
                                   ('catalog','Object',None),('source','ReadSourceRequest',None),('finalize','FinalizeRequest','Object')):
        op('tool_'+action,'post','/api/runs/{key}/tools/'+action,'Run-scoped deterministic '+action,body=body,params=[key])
        if response is None:
            result['paths']['/api/runs/{key}/tools/'+action]['post']['responses']['200']['content']['application/json']['schema'] = arr(O)
    for name in ('findings','experiments'):
        op(name,'get','/api/'+name,'Retained operational '+name+' view')
    return result
