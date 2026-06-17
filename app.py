# app.py — Backend Desentupi Pro v4.1 DEBUG
# Versão com logs detalhados para descobrir onde trava
import os, re, json
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import requests

app = Flask(__name__)
CORS(app)

cred_json = json.loads(os.environ.get('FIREBASE_CREDENTIALS', '{}'))
if not firebase_admin._apps:
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
db = firestore.client()

GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
EVOLUTION_URL = os.environ.get('EVOLUTION_URL', '')
EVOLUTION_KEY = os.environ.get('EVOLUTION_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'desentupi-pro')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'desentupi2024')

print("=" * 60)
print(f"[STARTUP] EVOLUTION_URL = '{EVOLUTION_URL}'")
print(f"[STARTUP] EVOLUTION_KEY = '{EVOLUTION_KEY[:5]}...' (len={len(EVOLUTION_KEY)})")
print(f"[STARTUP] EVOLUTION_INSTANCE = '{EVOLUTION_INSTANCE}'")
print(f"[STARTUP] GROQ_API_KEY = '{GROQ_API_KEY[:8]}...' (len={len(GROQ_API_KEY)})")
print("=" * 60)

processed_ids = set()
WARRANTY_DAYS = 90
MIN_RATING = 3.0
MAX_RETURN_ALERTS = 2
MAX_DISPATCH_ATTEMPTS = 3
DISPATCH_INTERVAL_SEC = 30

ALL_SERVICES = [
    'Pia entupida', 'Vaso sanitário entupido', 'Ralo entupido',
    'Esgoto', 'Cano estourado', 'Caixa de gordura',
    'Desentupimento geral', 'Caça vazamentos',
]

SYSTEM_PROMPT = """Você é a Maria, atendente da Desentupi Pro, empresa de desentupimento em São Paulo.
Seja rápida, educada e objetiva. Colete: nome, endereço completo, tipo de entupimento e se é urgente.
Pergunte UMA coisa por vez. Não invente preços nem prazos.
Quando tiver TODOS os dados, confirme e finalize com:
[ABRIR_CHAMADO]{"nome":"","endereco":"","problema":"","urgencia":"alta|media|baixa"}[/ABRIR_CHAMADO]
Nunca mostre esse bloco ao cliente."""

def limpar_telefone(n):
    d = re.sub(r'\D', '', n)
    return d[-11:] if len(d) >= 11 else d

def enviar_whatsapp(numero, texto):
    print(f"[ENVIAR_WPP] Tentando enviar para {numero}")
    print(f"[ENVIAR_WPP] EVOLUTION_URL atual: '{EVOLUTION_URL}'")
    print(f"[ENVIAR_WPP] Texto: {texto[:80]}...")
    
    if not EVOLUTION_URL:
        print(f"[ENVIAR_WPP] ❌ ERRO: EVOLUTION_URL está vazia! Mensagem só impressa no log.")
        return
    
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    payload = {
        'number': numero,
        'options': {'delay': 800, 'presence': 'composing'},
        'textMessage': {'text': texto}
    }
    headers = {'apikey': EVOLUTION_KEY, 'Content-Type': 'application/json'}
    
    print(f"[ENVIAR_WPP] POST {url}")
    print(f"[ENVIAR_WPP] Headers: apikey={EVOLUTION_KEY[:5]}...")
    print(f"[ENVIAR_WPP] Payload: {json.dumps(payload)[:200]}")
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"[ENVIAR_WPP] ✅ Status: {r.status_code}")
        print(f"[ENVIAR_WPP] Resposta: {r.text[:500]}")
    except Exception as e:
        print(f"[ENVIAR_WPP] ❌ Erro: {type(e).__name__}: {e}")

def chamar_groq(historico):
    print(f"[GROQ] Chamando IA com {len(historico)} mensagens")
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            json={
                'model': 'llama-3.1-8b-instant',
                'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}] + historico,
                'max_tokens': 400,
                'temperature': 0.6
            },
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            timeout=15
        )
        print(f"[GROQ] Status: {r.status_code}")
        resposta = r.json()['choices'][0]['message']['content']
        print(f"[GROQ] Resposta: {resposta[:100]}")
        return resposta
    except Exception as e:
        print(f"[GROQ] ❌ Erro: {e}")
        return "Desculpe, tive um problema. Pode repetir?"

def extrair_chamado(texto):
    m = re.search(r'\[ABRIR_CHAMADO\](.*?)\[/ABRIR_CHAMADO\]', texto, re.DOTALL)
    if m:
        try: return json.loads(m.group(1).strip())
        except: return None
    return None

def limpar_saida(texto):
    texto = re.sub(r'\[ABRIR_CHAMADO\].*?\[/ABRIR_CHAMADO\]', '', texto, flags=re.DOTALL)
    return re.sub(r'\[.*?\]', '', texto).strip()

def get_historico(numero, limite=15):
    try:
        msgs = db.collection('conversas').where('numero', '==', numero).order_by('criado_em', direction=firestore.Query.DESCENDING).limit(limite).get()
        return [{'role': m.to_dict()['role'], 'content': m.to_dict()['content']} for m in reversed(msgs)]
    except Exception as e:
        print(f"[HISTORICO] Erro: {e}")
        return []

def salvar_mensagem(numero, role, content):
    db.collection('conversas').add({
        'numero': numero, 'role': role, 'content': content,
        'criado_em': firestore.SERVER_TIMESTAMP
    })

def enviar_push_expo(tokens, title, body, data=None):
    if not tokens: return
    messages = []
    for token in tokens:
        if not token or not token.startswith('ExponentPushToken'): continue
        msg = {'to': token, 'sound': 'default', 'title': title, 'body': body, 'priority': 'high', 'channelId': 'chamados', 'badge': 1}
        if data: msg['data'] = data
        messages.append(msg)
    if not messages: return
    try:
        r = requests.post('https://exp.host/--/api/v2/push/send', json=messages, headers={'Content-Type': 'application/json'}, timeout=10)
        print(f"[Push] {len(messages)} dispositivos: {r.status_code}")
    except Exception as e:
        print(f"[Push erro] {e}")

def parceiro_elegivel(p_data, servico=None):
    if p_data.get('isBlocked', False): return False
    if p_data.get('disabledByAdmin', False): return False
    rating = p_data.get('rating', 5.0)
    if rating < MIN_RATING and not p_data.get('ratingOverride', False): return False
    if p_data.get('returnAlerts', 0) >= MAX_RETURN_ALERTS: return False
    if servico:
        accepted = p_data.get('acceptedServices', ALL_SERVICES)
        servico_lower = servico.lower()
        for s in accepted:
            if any(word in servico_lower for word in s.lower().split()): return True
        return False
    return True

def despachar_chamado(call_id, force_all=False, tentativa=1):
    try:
        call_doc = db.collection('calls').document(call_id).get()
        if not call_doc.exists: return
        cd = call_doc.to_dict()
        if cd.get('status') in ('accepted', 'in_service', 'completed', 'cancelled'): return
        servico = cd.get('description', '')
        is_return = cd.get('isReturn', False)
        preferred_partner = cd.get('preferredPartnerId')
        already_notified = cd.get('allNotifiedPartnerIds', [])
        parceiros_snap = db.collection('partners').get()
        parceiros = [(p.id, p.to_dict()) for p in parceiros_snap]
        if is_return and preferred_partner and tentativa == 1 and not force_all:
            p_data = next((d for pid, d in parceiros if pid == preferred_partner), None)
            if p_data and parceiro_elegivel(p_data, servico):
                token = p_data.get('expoPushToken')
                db.collection('calls').document(call_id).update({
                    'status': 'dispatched', 'notifiedPartnerIds': [preferred_partner],
                    'allNotifiedPartnerIds': already_notified + [preferred_partner],
                    'dispatchAttempt': tentativa, 'dispatchedAt': firestore.SERVER_TIMESTAMP,
                })
                if token: enviar_push_expo([token], '🔄 Chamado de retorno!', f"Cliente: {cd.get('clientName','')} — Garantia", {'callId': call_id, 'type': 'return_call'})
                print(f"[Despacho Retorno t{tentativa}] {call_id} → {preferred_partner}")
                return
        elegíveis = [(pid, d) for pid, d in parceiros if d.get('status') == 'available' and parceiro_elegivel(d, servico) and pid not in already_notified]
        if not elegíveis or tentativa > MAX_DISPATCH_ATTEMPTS:
            db.collection('calls').document(call_id).update({'status': 'open', 'openedAt': firestore.SERVER_TIMESTAMP, 'dispatchAttempt': tentativa})
            all_tokens = [d.get('expoPushToken') for _, d in parceiros if d.get('expoPushToken') and parceiro_elegivel(d)]
            if all_tokens: enviar_push_expo(all_tokens, '📋 Chamado disponível', f"{cd.get('clientName','')} — {cd.get('address','')}", {'callId': call_id, 'type': 'open_call'})
            print(f"[Despacho] {call_id} → sem parceiros (t{tentativa}) → aberto para todos")
            return
        selected_pairs = elegíveis[:3]
        selected = [pid for pid, _ in selected_pairs]
        tokens = [d.get('expoPushToken') for pid, d in selected_pairs if d.get('expoPushToken')]
        db.collection('calls').document(call_id).update({'status': 'dispatched', 'notifiedPartnerIds': selected, 'allNotifiedPartnerIds': already_notified + selected, 'dispatchAttempt': tentativa, 'dispatchedAt': firestore.SERVER_TIMESTAMP})
        if tokens: enviar_push_expo(tokens, f'🔔 Novo chamado! (Tentativa {tentativa}/{MAX_DISPATCH_ATTEMPTS})', f"{cd.get('clientName','')} — {cd.get('address','')}", {'callId': call_id, 'type': 'new_call'})
        print(f"[Despacho t{tentativa}] {call_id} → {selected}")
    except Exception as e:
        print(f"[Despacho erro] {e}")

def abrir_chamado(dados, numero):
    doc_ref = db.collection('calls').add({
        'clientName': dados.get('nome', ''), 'clientPhone': numero,
        'address': dados.get('endereco', ''), 'neighborhood': '',
        'description': dados.get('problema', ''), 'urgency': dados.get('urgencia', 'media'),
        'status': 'pending', 'notifiedPartnerIds': [],
        'allNotifiedPartnerIds': [], 'assignedPartnerId': None,
        'isReturn': False, 'warrantyDays': WARRANTY_DAYS,
        'dispatchAttempt': 0, 'createdAt': firestore.SERVER_TIMESTAMP,
    })
    return doc_ref[1].id

@app.route('/')
def index(): return jsonify({'status': 'ok', 'app': 'Desentupi Pro Backend', 'version': '4.1-debug'})

@app.route('/health')
def health(): return jsonify({'status': 'healthy'})

@app.route('/debug/env')
def debug_env():
    """Endpoint pra verificar se variáveis estão carregadas"""
    return jsonify({
        'EVOLUTION_URL': EVOLUTION_URL[:30] + '...' if len(EVOLUTION_URL) > 30 else EVOLUTION_URL,
        'EVOLUTION_URL_len': len(EVOLUTION_URL),
        'EVOLUTION_KEY_set': bool(EVOLUTION_KEY),
        'EVOLUTION_KEY_len': len(EVOLUTION_KEY),
        'EVOLUTION_INSTANCE': EVOLUTION_INSTANCE,
        'GROQ_API_KEY_set': bool(GROQ_API_KEY),
        'FIREBASE_set': bool(cred_json),
    })

@app.route('/debug/test-send', methods=['POST'])
def debug_test_send():
    """Endpoint pra testar envio direto. Use: POST com {"number":"5511XXX","text":"oi"}"""
    body = request.json or {}
    numero = body.get('number', '')
    texto = body.get('text', 'Teste do backend Desentupi')
    if not numero:
        return jsonify({'error': 'Forneça number'}), 400
    enviar_whatsapp(numero, texto)
    return jsonify({'sent': True, 'number': numero})

@app.route('/webhook/wpp', methods=['POST'])
def webhook_wpp():
    print("\n" + "=" * 60)
    print(f"[WEBHOOK] 📥 Mensagem recebida em {datetime.now().isoformat()}")
    
    data = request.json or {}
    
    # IMPRIME O JSON COMPLETO PRA DEBUG
    print(f"[WEBHOOK] JSON completo:")
    print(json.dumps(data, indent=2)[:2000])  # Primeiros 2000 chars
    
    msg_id = data.get('data', {}).get('key', {}).get('id', '')
    print(f"[WEBHOOK] msg_id: {msg_id}")
    
    if msg_id and msg_id in processed_ids:
        print(f"[WEBHOOK] ⚠️ Mensagem duplicada, ignorando")
        return jsonify({'status': 'duplicate'}), 200
    if msg_id: processed_ids.add(msg_id)
    if len(processed_ids) > 10000: processed_ids.clear()
    
    from_me = data.get('data', {}).get('key', {}).get('fromMe', False)
    print(f"[WEBHOOK] fromMe: {from_me}")
    
    if from_me:
        print(f"[WEBHOOK] ⚠️ Mensagem é minha (fromMe=true), ignorando")
        return jsonify({'status': 'ignored'}), 200
    
    numero_raw = data.get('data', {}).get('key', {}).get('remoteJid', '')
    print(f"[WEBHOOK] numero_raw: '{numero_raw}'")
    
    numero = limpar_telefone(numero_raw)
    print(f"[WEBHOOK] numero limpo: '{numero}'")
    
    # Tenta vários jeitos de extrair o texto
    msg_obj = data.get('data', {}).get('message', {})
    print(f"[WEBHOOK] message obj keys: {list(msg_obj.keys())}")
    
    texto = (
        msg_obj.get('conversation', '') or
        msg_obj.get('extendedTextMessage', {}).get('text', '') or
        msg_obj.get('imageMessage', {}).get('caption', '') or
        msg_obj.get('videoMessage', {}).get('caption', '')
    ).strip()
    
    print(f"[WEBHOOK] texto extraído: '{texto}'")
    
    if not numero or not texto:
        print(f"[WEBHOOK] ⚠️ Sem número ({bool(numero)}) ou sem texto ({bool(texto)}), ignorando")
        return jsonify({'status': 'no_content'}), 200
    
    print(f"[WEBHOOK] ✅ Salvando mensagem do usuário")
    salvar_mensagem(numero, 'user', texto)
    
    print(f"[WEBHOOK] 📚 Buscando histórico")
    historico = get_historico(numero)
    print(f"[WEBHOOK] {len(historico)} mensagens no histórico")
    
    print(f"[WEBHOOK] 🤖 Chamando IA")
    resposta_ia = chamar_groq(historico)
    print(f"[WEBHOOK] Resposta IA: {resposta_ia[:100]}")
    
    dados_chamado = extrair_chamado(resposta_ia)
    if dados_chamado:
        print(f"[WEBHOOK] 📞 IA decidiu abrir chamado: {dados_chamado}")
        call_id = abrir_chamado(dados_chamado, numero)
        despachar_chamado(call_id)
        resposta_limpa = limpar_saida(resposta_ia)
        if not resposta_limpa:
            resposta_limpa = f"✅ Chamado #{call_id[:6].upper()} aberto! Um técnico está sendo acionado 🔧"
    else:
        resposta_limpa = limpar_saida(resposta_ia)
    
    print(f"[WEBHOOK] 💾 Salvando resposta do assistant")
    salvar_mensagem(numero, 'assistant', resposta_limpa)
    
    print(f"[WEBHOOK] 📤 Enviando WhatsApp")
    enviar_whatsapp(numero_raw, resposta_limpa)
    
    print(f"[WEBHOOK] ✅ Fim do processamento")
    print("=" * 60 + "\n")
    return jsonify({'status': 'ok'})

@app.route('/api/calls', methods=['GET'])
def listar_calls():
    status = request.args.get('status')
    try:
        q = db.collection('calls').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100)
        if status: q = db.collection('calls').where('status', '==', status).order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100)
        docs = q.get()
        calls = []
        for d in docs:
            data = d.to_dict(); data['id'] = d.id
            for k in ['createdAt', 'completedAt', 'acceptedAt', 'startedAt', 'dispatchedAt', 'openedAt']:
                if data.get(k) and hasattr(data[k], 'isoformat'): data[k] = data[k].isoformat()
            calls.append(data)
        return jsonify({'calls': calls})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/calls', methods=['POST'])
def criar_call():
    body = request.json or {}
    for f in ['clientName', 'clientPhone', 'address', 'description']:
        if not body.get(f): return jsonify({'error': f'Campo obrigatório: {f}'}), 400
    try:
        is_return = body.get('isReturn', False)
        preferred_partner = None
        if is_return:
            cutoff = datetime.now(timezone.utc) - timedelta(days=WARRANTY_DAYS)
            prev = db.collection('calls').where('clientPhone', '==', body['clientPhone']).where('status', '==', 'completed').order_by('completedAt', direction=firestore.Query.DESCENDING).limit(1).get()
            for p in prev:
                pd = p.to_dict()
                completed_at = pd.get('completedAt')
                if completed_at and hasattr(completed_at, 'timestamp'):
                    if datetime.fromtimestamp(completed_at.timestamp(), tz=timezone.utc) > cutoff:
                        preferred_partner = pd.get('assignedPartnerId')
                        break
        doc_ref = db.collection('calls').add({
            'clientName': body['clientName'], 'clientPhone': body['clientPhone'],
            'address': body['address'], 'neighborhood': body.get('neighborhood', ''),
            'description': body['description'], 'urgency': body.get('urgency', 'media'),
            'status': 'pending', 'notifiedPartnerIds': [], 'allNotifiedPartnerIds': [],
            'assignedPartnerId': None, 'isReturn': is_return,
            'preferredPartnerId': preferred_partner, 'warrantyDays': WARRANTY_DAYS,
            'dispatchAttempt': 0, 'createdAt': firestore.SERVER_TIMESTAMP,
        })
        call_id = doc_ref[1].id
        force_all = body.get('forceAll', False)
        despachar_chamado(call_id, force_all=force_all)
        return jsonify({'success': True, 'callId': call_id, 'preferredPartner': preferred_partner}), 201
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners', methods=['GET'])
def listar_partners():
    try:
        docs = db.collection('partners').get()
        partners = []
        for d in docs:
            data = d.to_dict(); data['id'] = d.id
            calls = db.collection('calls').where('assignedPartnerId', '==', d.id).where('status', '==', 'completed').get()
            data['totalCompleted'] = len(calls)
            data['totalRevenue'] = sum(float(c.to_dict().get('valor', 0) or 0) for c in calls)
            partners.append(data)
        return jsonify({'partners': partners})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners/<partner_id>/toggle', methods=['POST'])
def toggle_partner(partner_id):
    try:
        doc = db.collection('partners').document(partner_id).get()
        if not doc.exists: return jsonify({'error': 'Não encontrado'}), 404
        current = doc.to_dict().get('disabledByAdmin', False)
        db.collection('partners').document(partner_id).update({'disabledByAdmin': not current, 'updatedAt': firestore.SERVER_TIMESTAMP})
        return jsonify({'success': True, 'disabled': not current})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/cron/processar', methods=['GET'])
def cron_processar():
    key = request.args.get('key', '')
    if key != WEBHOOK_SECRET: return jsonify({'error': 'Unauthorized'}), 401
    try:
        agora = datetime.now(timezone.utc)
        reprocessados = 0
        dispatched = db.collection('calls').where('status', '==', 'dispatched').get()
        for d in dispatched:
            data = d.to_dict()
            dt = data.get('dispatchedAt')
            if dt and hasattr(dt, 'timestamp'):
                elapsed = (agora - datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)).total_seconds()
                if elapsed > DISPATCH_INTERVAL_SEC:
                    tentativa_atual = data.get('dispatchAttempt', 1)
                    tentativa_prox = tentativa_atual + 1
                    despachar_chamado(d.id, tentativa=tentativa_prox)
                    reprocessados += 1
        pending = db.collection('calls').where('status', '==', 'pending').get()
        for d in pending:
            data = d.to_dict()
            dt = data.get('createdAt')
            if dt and hasattr(dt, 'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)).total_seconds() > 10:
                    despachar_chamado(d.id, tentativa=1)
                    reprocessados += 1
        return jsonify({'reprocessados': reprocessados, 'timestamp': agora.isoformat()})
    except Exception as e: return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
