# app.py — Backend Desentupi Pro v5.2
# IA Maria com pausa, anti-duplicata, controle pelo painel admin
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

processed_ids = set()
WARRANTY_DAYS = 90
MIN_RATING = 3.0
MAX_RETURN_ALERTS = 2
MAX_DISPATCH_ATTEMPTS = 3
DISPATCH_INTERVAL_SEC = 30
ASK_HUMAN_AFTER = 15  # depois de 15 mensagens, perguntar se quer humano
ANTI_DUPLICATE_MINUTES = 30  # janela anti-duplicata

ALL_SERVICES = [
    'Pia entupida', 'Vaso sanitário entupido', 'Ralo entupido',
    'Esgoto', 'Cano estourado', 'Caixa de gordura',
    'Desentupimento geral', 'Caça vazamentos',
]

# Palavras-chave que indicam que cliente quer humano
HUMAN_KEYWORDS = [
    'humano', 'atendente', 'pessoa', 'gente de verdade', 'gerente',
    'falar com alguem', 'falar com alguém', 'nao quero robo', 'não quero robô',
    'me passa pra alguem', 'me passa pra alguém', 'me transfere',
    'atendimento humano', 'falar com voce', 'falar com você',
]

SYSTEM_PROMPT = """Você é a Maria, atendente da Desentupi Pro, empresa de desentupimento 24h em São Paulo.

REGRAS DE ATENDIMENTO:
- Seja rápida, educada, objetiva e empática
- Pergunte UMA coisa por vez (nunca várias perguntas juntas)
- Não invente preços, prazos ou nomes de profissionais
- Tom amigável mas profissional, como uma boa atendente brasileira
- Use português brasileiro natural

DADOS QUE VOCÊ PRECISA COLETAR:
1. Nome do cliente
2. Endereço completo (rua, número, bairro)
3. Tipo de entupimento (vaso, pia, ralo, esgoto, cano, caixa de gordura, etc.)
4. Urgência (urgente, médio, baixa)

QUANDO TIVER TODOS OS DADOS:
Confirme com o cliente e finalize sua mensagem com este bloco (será removido antes do envio):
[ABRIR_CHAMADO]{"nome":"NOME","endereco":"ENDEREÇO","problema":"TIPO","urgencia":"alta|media|baixa"}[/ABRIR_CHAMADO]

IMPORTANTE - NUNCA ABRA CHAMADO DUPLICADO:
- Se já abriu chamado nesta conversa, não abra outro
- Se o cliente quiser informações adicionais, responda sem reabrir
- Se já enviou [ABRIR_CHAMADO] antes nesta conversa, não envie de novo

APÓS ABRIR CHAMADO:
- Avise que o profissional foi acionado
- Informe que receberá contato em alguns minutos
- Encerre a conversa de forma educada
- Não pergunte mais nada

NUNCA mostre os blocos [ABRIR_CHAMADO] ou [PEDE_HUMANO] ao cliente."""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def limpar_telefone(n):
    d = re.sub(r'\D', '', n)
    return d[-11:] if len(d) >= 11 else d

def cliente_pediu_humano(texto):
    """Detecta se cliente está pedindo atendimento humano"""
    t = texto.lower()
    return any(kw in t for kw in HUMAN_KEYWORDS)

def enviar_whatsapp(numero, texto):
    if not EVOLUTION_URL:
        print(f"[WPP-OFF] {numero}: {texto}")
        return
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    numero_limpo = numero.split('@')[0] if '@' in numero else numero
    try:
        r = requests.post(url,
            json={'number': numero_limpo, 'text': texto, 'delay': 800},
            headers={'apikey': EVOLUTION_KEY, 'Content-Type': 'application/json'},
            timeout=15)
        if r.status_code not in (200, 201):
            print(f"[WPP-ERRO] Status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[WPP-EXC] {type(e).__name__}: {e}")

def chamar_groq(historico, numero=None):
    """Chama IA com histórico. Se já tem muitas mensagens, adiciona instrução pra perguntar sobre humano."""
    msgs = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    
    # Se já tem muitas mensagens, adiciona instrução extra
    if len(historico) >= ASK_HUMAN_AFTER:
        msgs.append({
            'role': 'system',
            'content': 'CONTEXTO ESPECIAL: A conversa já tem mais de 15 mensagens. Se ainda não coletou todos os dados ou se o cliente está confuso, pergunte gentilmente: "Estou conseguindo ajudar ou você prefere falar com um atendente humano?" E adicione no final: [PEDE_HUMANO]ask[/PEDE_HUMANO]'
        })
    
    msgs.extend(historico)
    
    try:
        r = requests.post('https://api.groq.com/openai/v1/chat/completions',
            json={'model': 'llama-3.1-8b-instant', 'messages': msgs, 'max_tokens': 400, 'temperature': 0.6},
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            timeout=15)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"[Groq erro] {e}")
        return "Desculpe, tive um problema. Pode repetir?"

def extrair_chamado(texto):
    m = re.search(r'\[ABRIR_CHAMADO\](.*?)\[/ABRIR_CHAMADO\]', texto, re.DOTALL)
    if m:
        try: return json.loads(m.group(1).strip())
        except: return None
    return None

def extrair_pede_humano(texto):
    return bool(re.search(r'\[PEDE_HUMANO\]', texto))

def limpar_saida(texto):
    texto = re.sub(r'\[ABRIR_CHAMADO\].*?\[/ABRIR_CHAMADO\]', '', texto, flags=re.DOTALL)
    texto = re.sub(r'\[PEDE_HUMANO\].*?\[/PEDE_HUMANO\]', '', texto, flags=re.DOTALL)
    return re.sub(r'\[.*?\]', '', texto).strip()

def get_historico(numero, limite=20):
    try:
        msgs = db.collection('conversas').where('numero', '==', numero).order_by('criado_em', direction=firestore.Query.DESCENDING).limit(limite).get()
        return [{'role': m.to_dict()['role'], 'content': m.to_dict()['content']} for m in reversed(msgs)]
    except Exception as e:
        print(f"[HISTORICO] {e}")
        return []

def salvar_mensagem(numero, role, content):
    db.collection('conversas').add({
        'numero': numero, 'role': role, 'content': content,
        'criado_em': firestore.SERVER_TIMESTAMP
    })
    # Atualiza/cria documento de "conversa" pra agregar status
    conv_ref = db.collection('conversas_status').document(numero)
    update_data = {
        'numero': numero,
        'lastMessage': content[:200],
        'lastRole': role,
        'updatedAt': firestore.SERVER_TIMESTAMP,
    }
    try:
        existing = conv_ref.get()
        if not existing.exists:
            update_data['createdAt'] = firestore.SERVER_TIMESTAMP
            update_data['messageCount'] = 1
            update_data['status'] = 'active'
            update_data['aiEnabled'] = True
            update_data['needsHuman'] = False
            update_data['blocked'] = False
            update_data['callCreated'] = False
        else:
            data = existing.to_dict()
            update_data['messageCount'] = data.get('messageCount', 0) + 1
        conv_ref.set(update_data, merge=True)
    except Exception as e:
        print(f"[STATUS] {e}")

def get_conv_status(numero):
    try:
        doc = db.collection('conversas_status').document(numero).get()
        if doc.exists: return doc.to_dict()
    except: pass
    return {}

def is_ia_globally_enabled():
    try:
        doc = db.collection('config').document('ai').get()
        if doc.exists:
            return doc.to_dict().get('enabled', True)
    except: pass
    return True

def has_active_call(phone):
    """Verifica se cliente tem chamado ativo nos últimos 30min"""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ANTI_DUPLICATE_MINUTES)
        active_statuses = ['pending', 'dispatched', 'open', 'accepted', 'on_the_way', 'in_service']
        calls = (db.collection('calls')
            .where('clientPhone', '==', phone)
            .where('status', 'in', active_statuses)
            .limit(5).get())
        for c in calls:
            cd = c.to_dict()
            created = cd.get('createdAt')
            if created and hasattr(created, 'timestamp'):
                if datetime.fromtimestamp(created.timestamp(), tz=timezone.utc) > cutoff:
                    return c.id
        return None
    except Exception as e:
        print(f"[ANTIDUP] {e}")
        return None

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
        print(f"[Push] {len(messages)} disp: {r.status_code}")
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
        for s in accepted:
            if any(w in servico.lower() for w in s.lower().split()): return True
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
        preferred = cd.get('preferredPartnerId')
        already = cd.get('allNotifiedPartnerIds', [])
        parceiros_snap = db.collection('partners').get()
        parceiros = [(p.id, p.to_dict()) for p in parceiros_snap]
        if is_return and preferred and tentativa == 1 and not force_all:
            pd = next((d for pid, d in parceiros if pid == preferred), None)
            if pd and parceiro_elegivel(pd, servico):
                tok = pd.get('expoPushToken')
                db.collection('calls').document(call_id).update({
                    'status': 'dispatched', 'notifiedPartnerIds': [preferred],
                    'allNotifiedPartnerIds': already + [preferred],
                    'dispatchAttempt': tentativa, 'dispatchedAt': firestore.SERVER_TIMESTAMP,
                })
                if tok: enviar_push_expo([tok], '🔄 Retorno!', f"{cd.get('clientName','')} — Garantia", {'callId': call_id, 'type': 'return_call'})
                return
        elegíveis = [(pid, d) for pid, d in parceiros if d.get('status') == 'available' and parceiro_elegivel(d, servico) and pid not in already]
        if not elegíveis or tentativa > MAX_DISPATCH_ATTEMPTS:
            db.collection('calls').document(call_id).update({'status': 'open', 'openedAt': firestore.SERVER_TIMESTAMP, 'dispatchAttempt': tentativa})
            all_tokens = [d.get('expoPushToken') for _, d in parceiros if d.get('expoPushToken') and parceiro_elegivel(d)]
            if all_tokens: enviar_push_expo(all_tokens, '📋 Chamado disponível', f"{cd.get('clientName','')} — {cd.get('address','')}", {'callId': call_id, 'type': 'open_call'})
            return
        selected_pairs = elegíveis[:3]
        selected = [pid for pid, _ in selected_pairs]
        tokens = [d.get('expoPushToken') for pid, d in selected_pairs if d.get('expoPushToken')]
        db.collection('calls').document(call_id).update({
            'status': 'dispatched', 'notifiedPartnerIds': selected,
            'allNotifiedPartnerIds': already + selected, 'dispatchAttempt': tentativa,
            'dispatchedAt': firestore.SERVER_TIMESTAMP,
        })
        if tokens: enviar_push_expo(tokens, f'🔔 Novo chamado! ({tentativa}/{MAX_DISPATCH_ATTEMPTS})', f"{cd.get('clientName','')} — {cd.get('address','')}", {'callId': call_id, 'type': 'new_call'})
    except Exception as e:
        print(f"[Despacho] {e}")

def abrir_chamado(dados, numero):
    doc_ref = db.collection('calls').add({
        'clientName': dados.get('nome', ''), 'clientPhone': numero,
        'address': dados.get('endereco', ''), 'neighborhood': '',
        'description': dados.get('problema', ''), 'urgency': dados.get('urgencia', 'media'),
        'status': 'pending', 'notifiedPartnerIds': [], 'allNotifiedPartnerIds': [],
        'assignedPartnerId': None, 'isReturn': False, 'warrantyDays': WARRANTY_DAYS,
        'dispatchAttempt': 0, 'createdAt': firestore.SERVER_TIMESTAMP,
        'source': 'whatsapp_ai',
    })
    return doc_ref[1].id

# ─── Rotas básicas ────────────────────────────────────────────────────────────

@app.route('/')
def index(): return jsonify({'status': 'ok', 'app': 'Desentupi Pro Backend', 'version': '5.2'})

@app.route('/health')
def health(): return jsonify({'status': 'healthy'})

# ─── Webhook WhatsApp ─────────────────────────────────────────────────────────

@app.route('/webhook/wpp', methods=['POST'])
def webhook_wpp():
    data = request.json or {}
    msg_id = data.get('data', {}).get('key', {}).get('id', '')
    if msg_id and msg_id in processed_ids:
        return jsonify({'status': 'duplicate'}), 200
    if msg_id: processed_ids.add(msg_id)
    if len(processed_ids) > 10000: processed_ids.clear()
    
    if data.get('data', {}).get('key', {}).get('fromMe', False):
        return jsonify({'status': 'ignored'}), 200
    
    numero_raw = data.get('data', {}).get('key', {}).get('remoteJid', '')
    numero = limpar_telefone(numero_raw)
    
    # Ignora grupos
    if '@g.us' in numero_raw:
        return jsonify({'status': 'group_ignored'}), 200
    
    msg_obj = data.get('data', {}).get('message', {})
    texto = (
        msg_obj.get('conversation', '') or
        msg_obj.get('extendedTextMessage', {}).get('text', '') or
        msg_obj.get('imageMessage', {}).get('caption', '') or
        msg_obj.get('videoMessage', {}).get('caption', '')
    ).strip()
    
    if not numero or not texto:
        return jsonify({'status': 'no_content'}), 200
    
    # Verifica status da conversa
    conv_status = get_conv_status(numero)
    
    # Bloqueado? Ignora
    if conv_status.get('blocked', False):
        return jsonify({'status': 'blocked'}), 200
    
    # Salva mensagem do usuário sempre
    salvar_mensagem(numero, 'user', texto)
    
    # Cliente pediu humano? Marca e não responde com IA
    if cliente_pediu_humano(texto):
        db.collection('conversas_status').document(numero).set({
            'needsHuman': True, 'aiEnabled': False,
            'humanRequestedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        resposta = "Entendi! Um atendente humano vai falar com você em breve. Aguarde só um momentinho 🙏"
        salvar_mensagem(numero, 'assistant', resposta)
        enviar_whatsapp(numero_raw, resposta)
        return jsonify({'status': 'human_requested'}), 200
    
    # IA pausada pra essa conversa?
    if not conv_status.get('aiEnabled', True):
        # IA pausada — não responde automaticamente
        return jsonify({'status': 'ai_paused_conversation'}), 200
    
    # IA globalmente desligada?
    if not is_ia_globally_enabled():
        return jsonify({'status': 'ai_globally_disabled'}), 200
    
    # Já criou chamado nesta conversa nos últimos 30min? Não chama IA pra ela tentar criar de novo
    active_call = has_active_call(numero)
    
    # Chama IA
    historico = get_historico(numero)
    resposta_ia = chamar_groq(historico, numero)
    
    # IA quer abrir chamado?
    dados_chamado = extrair_chamado(resposta_ia)
    
    if dados_chamado and not active_call:
        # CRIA o chamado
        call_id = abrir_chamado(dados_chamado, numero)
        despachar_chamado(call_id)
        db.collection('conversas_status').document(numero).set({
            'callCreated': True, 'lastCallId': call_id,
            'lastCallAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        resposta_limpa = limpar_saida(resposta_ia)
        if not resposta_limpa:
            resposta_limpa = f"✅ Chamado #{call_id[:6].upper()} aberto! Em alguns minutos um profissional entra em contato. Obrigada! 🔧"
    elif dados_chamado and active_call:
        # JÁ TEM CHAMADO ATIVO — não cria duplicata
        resposta_limpa = f"Você já tem um chamado em andamento (#{active_call[:6].upper()}). Em alguns minutos um profissional entra em contato. Se precisar de algo a mais, me avise!"
    else:
        resposta_limpa = limpar_saida(resposta_ia)
    
    # IA detectou que precisa humano?
    if extrair_pede_humano(resposta_ia):
        db.collection('conversas_status').document(numero).set({
            'needsHuman': True,
            'humanRequestedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
    
    salvar_mensagem(numero, 'assistant', resposta_limpa)
    enviar_whatsapp(numero_raw, resposta_limpa)
    return jsonify({'status': 'ok'})

# ─── Endpoints de Chamados ────────────────────────────────────────────────────

@app.route('/api/calls', methods=['GET'])
def listar_calls():
    status = request.args.get('status')
    try:
        q = db.collection('calls').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100)
        if status: q = db.collection('calls').where('status', '==', status).order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100)
        docs = q.get()
        calls = []
        for d in docs:
            try:
                data = d.to_dict(); data['id'] = d.id
                for k in ['createdAt', 'completedAt', 'acceptedAt', 'startedAt', 'dispatchedAt', 'openedAt']:
                    if data.get(k) and hasattr(data[k], 'isoformat'): data[k] = data[k].isoformat()
                calls.append(data)
            except Exception as inner:
                print(f"[CALLS] Erro ao processar doc {d.id}: {inner}")
                continue
        return jsonify({'calls': calls})
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[CALLS ERRO 500] {type(e).__name__}: {e}")
        print(f"[CALLS TRACEBACK]\n{tb}")
        return jsonify({'error': str(e), 'type': type(e).__name__}), 500

@app.route('/api/calls', methods=['POST'])
def criar_call():
    body = request.json or {}
    for f in ['clientName', 'clientPhone', 'address', 'description']:
        if not body.get(f): return jsonify({'error': f'Campo: {f}'}), 400
    try:
        is_return = body.get('isReturn', False)
        preferred = None
        if is_return:
            cutoff = datetime.now(timezone.utc) - timedelta(days=WARRANTY_DAYS)
            prev = db.collection('calls').where('clientPhone', '==', body['clientPhone']).where('status', '==', 'completed').order_by('completedAt', direction=firestore.Query.DESCENDING).limit(1).get()
            for p in prev:
                pd = p.to_dict()
                ca = pd.get('completedAt')
                if ca and hasattr(ca, 'timestamp'):
                    if datetime.fromtimestamp(ca.timestamp(), tz=timezone.utc) > cutoff:
                        preferred = pd.get('assignedPartnerId'); break
        doc_ref = db.collection('calls').add({
            'clientName': body['clientName'], 'clientPhone': body['clientPhone'],
            'address': body['address'], 'neighborhood': body.get('neighborhood', ''),
            'description': body['description'], 'urgency': body.get('urgency', 'media'),
            'status': 'pending', 'notifiedPartnerIds': [], 'allNotifiedPartnerIds': [],
            'assignedPartnerId': None, 'isReturn': is_return,
            'preferredPartnerId': preferred, 'warrantyDays': WARRANTY_DAYS,
            'dispatchAttempt': 0, 'createdAt': firestore.SERVER_TIMESTAMP,
            'source': body.get('source', 'manual'),
        })
        call_id = doc_ref[1].id
        despachar_chamado(call_id, force_all=body.get('forceAll', False))
        return jsonify({'success': True, 'callId': call_id, 'preferredPartner': preferred}), 201
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/calls/<call_id>/dispatch', methods=['POST'])
def redispatch(call_id):
    try:
        despachar_chamado(call_id, force_all=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners', methods=['GET'])
def listar_partners():
    try:
        docs = db.collection('partners').get()
        partners = []
        for d in docs:
            try:
                data = d.to_dict(); data['id'] = d.id
                # Não calcula totalCompleted/totalRevenue aqui — economiza queries
                # Os valores já devem estar salvos no doc do partner
                data['totalCompleted'] = data.get('totalCompleted', 0)
                data['totalRevenue'] = data.get('totalRevenue', 0)
                partners.append(data)
            except Exception as inner:
                print(f"[PARTNERS] Erro ao processar doc {d.id}: {inner}")
                continue
        return jsonify({'partners': partners})
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[PARTNERS ERRO 500] {type(e).__name__}: {e}")
        print(f"[PARTNERS TRACEBACK]\n{tb}")
        return jsonify({'error': str(e), 'type': type(e).__name__}), 500
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners/<partner_id>/toggle', methods=['POST'])
def toggle_partner(partner_id):
    try:
        doc = db.collection('partners').document(partner_id).get()
        if not doc.exists: return jsonify({'error': 'Não existe'}), 404
        cur = doc.to_dict().get('disabledByAdmin', False)
        db.collection('partners').document(partner_id).update({'disabledByAdmin': not cur, 'updatedAt': firestore.SERVER_TIMESTAMP})
        return jsonify({'success': True, 'disabled': not cur})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners/<partner_id>/override-rating', methods=['POST'])
def override_rating(partner_id):
    try:
        db.collection('partners').document(partner_id).update({'ratingOverride': True, 'updatedAt': firestore.SERVER_TIMESTAMP})
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/partners/<partner_id>/clear-alerts', methods=['POST'])
def clear_alerts(partner_id):
    try:
        db.collection('partners').document(partner_id).update({'returnAlerts': 0, 'isBlocked': False, 'updatedAt': firestore.SERVER_TIMESTAMP})
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ─── Endpoints de WhatsApp/Conversas ──────────────────────────────────────────

@app.route('/api/conversations', methods=['GET'])
def listar_conversas():
    """Lista conversas resumidas pro painel admin"""
    try:
        docs = db.collection('conversas_status').order_by('updatedAt', direction=firestore.Query.DESCENDING).limit(50).get()
        conversas = []
        for d in docs:
            data = d.to_dict(); data['id'] = d.id
            for k in ['createdAt', 'updatedAt', 'humanRequestedAt', 'lastCallAt']:
                if data.get(k) and hasattr(data[k], 'isoformat'): data[k] = data[k].isoformat()
            conversas.append(data)
        return jsonify({'conversations': conversas})
    except Exception as e: return jsonify({'error': str(e), 'conversations': []}), 500

@app.route('/api/conversations/<numero>/messages', methods=['GET'])
def conversa_mensagens(numero):
    """Retorna histórico completo de uma conversa"""
    try:
        msgs = db.collection('conversas').where('numero', '==', numero).order_by('criado_em', direction=firestore.Query.ASCENDING).limit(200).get()
        out = []
        for m in msgs:
            md = m.to_dict()
            if md.get('criado_em') and hasattr(md['criado_em'], 'isoformat'):
                md['criado_em'] = md['criado_em'].isoformat()
            out.append(md)
        return jsonify({'messages': out})
    except Exception as e: return jsonify({'error': str(e), 'messages': []}), 500

@app.route('/api/conversations/<numero>/pause', methods=['POST'])
def pausar_ia_conversa(numero):
    """Pausa IA pra essa conversa (humano assume)"""
    try:
        db.collection('conversas_status').document(numero).set({
            'aiEnabled': False, 'pausedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/conversations/<numero>/resume', methods=['POST'])
def retomar_ia_conversa(numero):
    """Retoma IA pra essa conversa"""
    try:
        db.collection('conversas_status').document(numero).set({
            'aiEnabled': True, 'needsHuman': False,
        }, merge=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/conversations/<numero>/block', methods=['POST'])
def bloquear_conversa(numero):
    """Bloqueia um número (não responde mais)"""
    try:
        db.collection('conversas_status').document(numero).set({
            'blocked': True, 'blockedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/conversations/<numero>/unblock', methods=['POST'])
def desbloquear_conversa(numero):
    try:
        db.collection('conversas_status').document(numero).set({'blocked': False}, merge=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/conversations/<numero>/send', methods=['POST'])
def enviar_mensagem_manual(numero):
    """Envia mensagem manual (humano assumiu)"""
    body = request.json or {}
    texto = body.get('text', '').strip()
    if not texto: return jsonify({'error': 'Texto vazio'}), 400
    try:
        # Garante código do país e formato JID
        num_clean = re.sub(r'\D', '', numero)
        # Se tem 10-11 dígitos (DDD + telefone), adiciona 55
        if len(num_clean) <= 11 and not num_clean.startswith('55'):
            num_clean = '55' + num_clean
        jid = f'{num_clean}@s.whatsapp.net'
        enviar_whatsapp(jid, texto)
        salvar_mensagem(numero, 'assistant', f"[Humano] {texto}")
        return jsonify({'success': True, 'sent_to': jid})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/conversations/<numero>/mark-handled', methods=['POST'])
def marcar_atendida(numero):
    """Marca conversa como já atendida (limpa needsHuman)"""
    try:
        db.collection('conversas_status').document(numero).set({
            'needsHuman': False,
        }, merge=True)
        return jsonify({'success': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ─── Configuração global da IA ────────────────────────────────────────────────

@app.route('/api/ai-status', methods=['GET'])
def ai_status():
    try:
        doc = db.collection('config').document('ai').get()
        if doc.exists:
            return jsonify(doc.to_dict())
        return jsonify({'enabled': True})
    except Exception as e: return jsonify({'enabled': True, 'error': str(e)}), 500

@app.route('/api/ai-status', methods=['POST'])
def set_ai_status():
    body = request.json or {}
    enabled = body.get('enabled', True)
    try:
        db.collection('config').document('ai').set({
            'enabled': enabled, 'updatedAt': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        return jsonify({'success': True, 'enabled': enabled})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ─── Cron ─────────────────────────────────────────────────────────────────────

# ─── Gerenciamento Evolution API (WhatsApp) ───────────────────────────────────

@app.route('/api/whatsapp/status', methods=['GET'])
def whatsapp_status():
    """Verifica status da conexão do WhatsApp"""
    if not EVOLUTION_URL:
        return jsonify({'connected': False, 'error': 'EVOLUTION_URL não configurada'}), 200
    try:
        r = requests.get(
            f"{EVOLUTION_URL}/instance/connectionState/{EVOLUTION_INSTANCE}",
            headers={'apikey': EVOLUTION_KEY},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            instance = data.get('instance', {})
            state = instance.get('state', 'unknown')
            return jsonify({
                'connected': state == 'open',
                'state': state,
                'instance': EVOLUTION_INSTANCE,
                'raw': data
            })
        return jsonify({
            'connected': False,
            'state': 'error',
            'instance': EVOLUTION_INSTANCE,
            'http_status': r.status_code,
            'response': r.text[:300]
        }), 200
    except Exception as e:
        return jsonify({'connected': False, 'error': str(e)}), 200

@app.route('/api/whatsapp/qrcode', methods=['GET'])
def whatsapp_qrcode():
    """Pega o QR Code da instância para conectar"""
    if not EVOLUTION_URL:
        return jsonify({'error': 'EVOLUTION_URL não configurada'}), 500
    try:
        r = requests.get(
            f"{EVOLUTION_URL}/instance/connect/{EVOLUTION_INSTANCE}",
            headers={'apikey': EVOLUTION_KEY},
            timeout=15
        )
        if r.status_code in (200, 201):
            data = r.json()
            # Evolution v2 retorna 'base64' ou 'code'
            qr = data.get('base64') or data.get('qrcode', {}).get('base64') or data.get('code')
            return jsonify({
                'success': True,
                'qrcode': qr,
                'instance': EVOLUTION_INSTANCE,
                'raw': data
            })
        return jsonify({'error': f'HTTP {r.status_code}', 'response': r.text[:300]}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/logout', methods=['POST'])
def whatsapp_logout():
    """Desconecta o WhatsApp atual (para conectar outro número)"""
    if not EVOLUTION_URL:
        return jsonify({'error': 'EVOLUTION_URL não configurada'}), 500
    try:
        r = requests.delete(
            f"{EVOLUTION_URL}/instance/logout/{EVOLUTION_INSTANCE}",
            headers={'apikey': EVOLUTION_KEY},
            timeout=10
        )
        return jsonify({'success': r.status_code in (200, 201), 'status': r.status_code, 'response': r.text[:300]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/restart', methods=['POST'])
def whatsapp_restart():
    """Reinicia a instância (sem desconectar)"""
    if not EVOLUTION_URL:
        return jsonify({'error': 'EVOLUTION_URL não configurada'}), 500
    try:
        r = requests.post(
            f"{EVOLUTION_URL}/instance/restart/{EVOLUTION_INSTANCE}",
            headers={'apikey': EVOLUTION_KEY},
            timeout=15
        )
        return jsonify({'success': r.status_code in (200, 201), 'status': r.status_code, 'response': r.text[:300]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Cron ─────────────────────────────────────────────────────────────────────

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
                    tentativa_prox = data.get('dispatchAttempt', 1) + 1
                    if data.get('isReturn') and data.get('preferredPartnerId') and data.get('dispatchAttempt', 1) == 1:
                        pid = data['preferredPartnerId']
                        p = db.collection('partners').document(pid).get()
                        if p.exists:
                            alerts = p.to_dict().get('returnAlerts', 0) + 1
                            updates = {'returnAlerts': alerts}
                            if alerts >= MAX_RETURN_ALERTS: updates['isBlocked'] = True
                            db.collection('partners').document(pid).update(updates)
                    despachar_chamado(d.id, tentativa=tentativa_prox)
                    reprocessados += 1
        open_calls = db.collection('calls').where('status', '==', 'open').get()
        for d in open_calls:
            data = d.to_dict()
            dt = data.get('openedAt')
            if dt and hasattr(dt, 'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)).total_seconds() > 300:
                    parceiros = db.collection('partners').get()
                    tokens = [p.to_dict().get('expoPushToken') for p in parceiros if p.to_dict().get('expoPushToken') and parceiro_elegivel(p.to_dict())]
                    if tokens:
                        enviar_push_expo(tokens, '📋 Chamado disponível', f"{data.get('clientName','')} — {data.get('address','')}", {'callId': d.id, 'type': 'open_call'})
                    db.collection('calls').document(d.id).update({'openedAt': firestore.SERVER_TIMESTAMP})
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
