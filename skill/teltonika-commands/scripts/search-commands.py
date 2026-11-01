#!/usr/bin/env python3
"""Buscador local del catálogo Teltonika y formateador de SMS.

Sin acceso a red, sin dependencias externas y SIN transmisión de SMS.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'references' / 'commands.json'
COMPATIBILITY = ROOT / 'references' / 'compatibility.json'


def read_json(path):
    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


def base_record(command, model, profile):
    models = profile['profiles'][0]['documented_models']
    recognized = model.upper() in models if model else None
    special = profile.get('command_overrides', {}).get(command['name'])
    return {
        **command,
        'requested_model': model.upper() if model else None,
        'model_in_family_profile': recognized,
        'compatibility_assessment': (
            'requiere_verificacion_por_comando_y_modelo' if special else
            'incluido_en_familia_firmware_sin_verificar' if recognized else
            'modelo_fuera_del_perfil_recopilado' if model else
            'modelo_no_indicado'
        ),
        'execution_allowed': False,
        'execution_note': 'Esta utilidad solo consulta y prepara datos; nunca envía SMS.'
    }


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))

def score(query, command):
    q = normalize(query).strip()
    hay = [command['name'], command['description'], command['category']] + command['keywords']
    total = 0
    if q == command['name'].lower():
        total += 100
    if q in normalize(command['name']):
        total += 40
    for text in hay:
        if q and q in normalize(text):
            total += 8
    words = set(re.findall(r'[a-z0-9_]+', q))
    indexed = normalize(' '.join(hay))
    total += sum(3 for word in words if len(word) >= 3 and word in indexed)
    return total


def fail(message):
    raise ValueError(message)


def format_payload(command, args, auth, login, password):
    name = command['name']
    if command['effect'] in ('destructive', 'physical_actuation'):
        fail('El formateo de comandos de alto impacto está deshabilitado en este laboratorio.')
    if auth == 'unknown':
        fail('Se desconoce la autenticación SMS. Verifica si hay usuario y contraseña configurados.')
    if auth == 'none' and (login or password):
        fail('No indiques usuario ni contraseña cuando utilices --auth none.')
    if auth == 'configured' and (not login or not password):
        fail('Se requieren --login y --password si las credenciales están configuradas. No escribas secretos reales en el historial de shell.')
    if command['arguments_required'] and not args:
        fail(f'{name} requiere argumentos verificados; no inventes IDs ni valores de parámetros.')
    if not command['arguments_required'] and args:
        fail(f'{name} no acepta argumentos según la sintaxis documentada en este catálogo.')
    if args and (any(x in args for x in ['<', '>', '\n', '\r']) or not args.strip()):
        fail('Los argumentos incluyen marcadores, saltos de línea o valores vacíos.')
    if name in ('getparam', 'setparam') and args:
        pattern = r'\d+(?:;\d+)*' if name == 'getparam' else r'\d+:[^;:\s]+(?:;\d+:[^;:\s]+)*'
        if not re.fullmatch(pattern, args):
            fail('Formato de parámetros inválido; consulta los IDs oficiales.')
    if name == 'readio' and args and not re.fullmatch(r'\d+', args):
        fail('readio requiere un identificador AVL numérico.')
    if name == 'flush':
        fail('La redirección de servidor requiere un procedimiento de cambios separado y revisado.')
    payload = name + (' ' + args if args else '')
    if auth == 'none':
        payload = '  ' + payload
    else:
        if re.search(r'\s', login + password):
            fail('El usuario y la contraseña SMS no deben contener espacios.')
        payload = f'{login} {password} {payload}'
    if len(payload) > 160:
        fail('La carga útil supera el límite conservador de 160 caracteres SMS.')
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p_search = sub.add_parser('search', help='Buscar comandos por lenguaje natural.')
    p_search.add_argument('query')
    p_search.add_argument('--limit', type=int, default=5)
    p_show = sub.add_parser('show', help='Mostrar un comando exacto.')
    p_show.add_argument('command')
    p_list = sub.add_parser('list', help='Enumerar los comandos del catálogo.')
    p_list.add_argument('--risk', choices=['low', 'high', 'critical'])
    p_format = sub.add_parser('format', help='Preparar SMS localmente; nunca enviar.')
    p_format.add_argument('command')
    p_format.add_argument('--args', default='')
    p_format.add_argument('--auth', choices=['none', 'configured', 'unknown'], default='unknown')
    p_format.add_argument('--login')
    p_format.add_argument('--password')
    for p in (p_search, p_show, p_list, p_format):
        p.add_argument('--model', help='Modelo de rastreador, por ejemplo FMC130')
        p.add_argument('--json', action='store_true', help='Emitir JSON legible por máquinas')
    args = parser.parse_args()
    catalog = read_json(CATALOG)
    profile = read_json(COMPATIBILITY)
    commands = catalog['commands']
    found = {c['name']: c for c in commands}
    if args.action == 'search':
        if args.limit < 1 or args.limit > 100:
            fail('El límite debe estar entre 1 y 100.')
        ranking = [(score(args.query, c), c) for c in commands]
        results = [base_record(c, args.model, profile) for points, c in sorted(ranking, key=lambda x: (-x[0], x[1]['name'])) if points > 0][:args.limit]
    elif args.action == 'list':
        results = [base_record(c, args.model, profile) for c in commands if not args.risk or c['risk'] == args.risk]
    else:
        command = found.get(args.command)
        if not command:
            fail(f'Comando desconocido: {args.command}. No se inventó ningún comando.')
        results = base_record(command, args.model, profile)
        if args.action == 'format':
            sms = format_payload(command, args.args, args.auth, args.login, args.password)
            results = {'command': results, 'sms_payload': sms if args.auth == 'none' else None,
                       'sms_payload_redacted': sms if args.auth == 'none' else '<credenciales SMS ocultas> ' + command['name'] + (' ' + args.args if args.args else ''),
                       'leading_ascii_spaces': len(sms) - len(sms.lstrip(' ')), 'sent': False,
                       'note': 'No se realizaron llamadas de red ni MCP. Las credenciales se omiten intencionalmente.'}
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif isinstance(results, list):
        if not results:
            print('No se encontraron coincidencias en el catálogo; consulta la documentación oficial.')
        for c in results:
            print(f"{c['name']:<17} {c['risk']:<8} {c['syntax']} — {c['description']} [{c['compatibility_assessment']}]")
    elif args.action == 'format':
        print('Preparado, no enviado:', json.dumps(results['sms_payload'] or results['sms_payload_redacted']))
        print('Espacios ASCII iniciales:', results['leading_ascii_spaces'])
    else:
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(2)
