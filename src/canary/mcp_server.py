"""Stdio MCP relay. Tools and schemas come from the live HTTP contract."""
import asyncio
from copy import deepcopy
import json
import sys
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from .api_client import endpoint


def local_schema(schema, definitions):
    """Carry only referenced definitions, keeping tool descriptions compact."""
    result = deepcopy(definitions[schema['$ref'].rsplit('/',1)[1]] if '$ref' in schema else schema)
    needed = {}
    def walk(value):
        if isinstance(value, dict):
            target = value.get('$ref','')
            if target.startswith('#/components/schemas/'):
                name = target.rsplit('/',1)[1]
                value['$ref'] = '#/$defs/'+name
                if name not in needed:
                    needed[name] = deepcopy(definitions[name]);walk(needed[name])
            for key, child in value.items():
                if key != '$ref': walk(child)
        elif isinstance(value,list):
            for child in value: walk(child)
    walk(result)
    if needed: result['$defs'] = needed
    return result


def tool_catalog(contract):
    definitions = contract['components']['schemas']
    catalog = {}
    for path, methods in contract['paths'].items():
        for method, operation in methods.items():
            meta = operation.get('x-canary-mcp')
            if not meta: continue
            body = meta.get('bodySchema') or operation.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema')
            if body and '$ref' in body: body = definitions[body['$ref'].rsplit('/',1)[1]]
            input_schema = deepcopy(body or {'type':'object','properties':{},'required':[],'additionalProperties':False})
            input_schema.setdefault('properties',{})
            input_schema.setdefault('required',[])
            for parameter in operation.get('parameters',[]):
                input_schema['properties'][parameter['name']] = parameter['schema']
                if parameter.get('required'): input_schema['required'].append(parameter['name'])
            response = next(v for k,v in operation['responses'].items() if k.isdigit() and 200 <= int(k) < 300)
            output_schema = response['content']['application/json']['schema']
            catalog[meta['name']] = {'method':method,'path':path,'operation':operation,'body':bool(body),
                'tool':Tool(name=meta['name'],description=operation['summary'],
                    inputSchema=local_schema(input_schema,definitions),outputSchema=local_schema(output_schema,definitions),
                    annotations=ToolAnnotations(readOnlyHint=meta['readOnly'],
                        destructiveHint=operation['operationId'] in ('cancel','auth_cancel','auth_logout'),
                        idempotentHint=meta['readOnly'],openWorldHint=True))}
    return catalog


async def serve(port):
    async with httpx.AsyncClient(base_url=endpoint(port),timeout=30,trust_env=False) as api:
        response = await api.get('/openapi.json');response.raise_for_status()
        catalog = tool_catalog(response.json())
        server = Server('canary',version='1.0.0',instructions=(
            'Canary is the research producer. Ask returns a run key; inspect that run for progress. '
            'Execution completion, source binding and scientific audit are separate. '
            'Claims retain uncertainty/freshness; follow evidence and paragraph IDs to exact source context. '
            'Bundle exports require destination admission and do not publish a world model. '
            f'Resolve relative source/artifact URLs against {endpoint(port)}.'))

        @server.list_tools()
        async def list_tools():
            return [entry['tool'] for entry in catalog.values()]

        @server.call_tool()
        async def call_tool(name, arguments):
            if name not in catalog:
                return CallToolResult(isError=True,content=[TextContent(type='text',text='Unknown Canary tool')])
            entry = catalog[name]
            remaining, query, path = dict(arguments), {}, entry['path']
            for parameter in entry['operation'].get('parameters',[]):
                value = remaining.pop(parameter['name'],None)
                if value is None: continue
                if parameter['in'] == 'path': path = path.replace('{'+parameter['name']+'}',quote(str(value),safe=''))
                else: query[parameter['name']] = value
            try:
                kwargs = {'params':query}
                if entry['method'] != 'get': kwargs['json'] = remaining
                response = await api.request(entry['method'].upper(),path,**kwargs)
                value = response.json()
                if response.is_error:
                    return CallToolResult(isError=True,structuredContent=value,
                        content=[TextContent(type='text',text=json.dumps(value,ensure_ascii=False))])
                return value
            except (httpx.HTTPError, ValueError) as error:
                return CallToolResult(isError=True,content=[TextContent(type='text',text='Canary API request failed: '+str(error))])

        async with stdio_server() as (incoming,outgoing):
            await server.run(incoming,outgoing,server.create_initialization_options())


def main(port=8789):
    try:
        asyncio.run(serve(port));return 0
    except (httpx.HTTPError,ValueError,KeyError) as error:
        print(f'Canary MCP cannot read the API contract on port {port}: {error}. Start canary runs serve first.',file=sys.stderr)
        return 1
