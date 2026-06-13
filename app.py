# app.py — Backend Desentupi Pro v3
# Inclui: retorno garantia, filtro de serviços, bloqueio por avaliação/retorno, habilitar/desabilitar parceiro

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

GROQ_API_KEY       = os.environ.get('GROQ_API_KEY', '')
EVOLUTION_URL      = os.environ.get('EVOLUTION_URL', '')
EVOLUTION_KEY      = os.environ.get('EVOLUTION_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'desentupi')
WEBHOOK_SECRET     = os.environ.get('WEBHOOK_SECRET', 'desentupi2024')
processed_ids = set()

SYSTEM_PROMPT = """Você é a atendente virtual do Desentupi Pro, empresa de desentupimento em São Paulo.
Seja rápida, educada e objetiva. Colete: nome, endereço completo, tipo de entupimento e se é urgente.
Pergunte UMA coisa por vez. Não invente preços nem prazos.
Quando tiver TODOS os dados, confirme e finalize com:
[ABRIR_CHAMADO]{"nome":"","endereco":"","problema":"","urgencia":"alta|media|baixa"}[/ABRIR_CHAMADO]
Nunca mostre esse bloco ao cliente."""

ALL_SERVICES = [
  'Pia entupida', 'Vaso sanitário entupido', 'Ralo entupido',
  'Esgoto', 'Cano estourado', 'Caixa de gordura',
  'Desentupimento geral', 'Caça vazamentos',
]
WARRANTY_DAYS = 90
MIN_RATING = 3.0
MAX_RETURN_ALERTS = 2

# ─── Helpers ──────────────────────────────────────────────────────────────────

def limpar_telefone(n):
    d = re.sub(r'\D', '', n)
    return d[-11:] if len(d) >= 11 else d

def enviar_whatsapp(numero, texto):
    if not EVOLUTION_URL: print(f"[WPP] {numero}: {texto}"); return
    try:
        requests.post(f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
            json={'number':numero,'options':{'delay':800,'presence':'composing'},'textMessage':{'text':texto}},
            headers={'apikey':EVOLUTION_KEY,'Content-Type':'application/json'}, timeout=10)
    except Exception as e: print(f"[WPP erro] {e}")

def chamar_groq(historico):
    try:
        r = requests.post('https://api.groq.com/openai/v1/chat/completions',
            json={'model':'llama-3.1-8b-instant','messages':[{'role':'system','content':SYSTEM_PROMPT}]+historico,'max_tokens':400,'temperature':0.6},
            headers={'Authorization':f'Bearer {GROQ_API_KEY}','Content-Type':'application/json'}, timeout=15)
        return r.json()['choices'][0]['message']['content']
    except Exception as e: print(f"[Groq erro] {e}"); return "Desculpe, tive um problema. Pode repetir?"

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
        msgs = db.collection('conversas').where('numero','==',numero).order_by('criado_em',direction=firestore.Query.DESCENDING).limit(limite).get()
        return [{'role':m.to_dict()['role'],'content':m.to_dict()['content']} for m in reversed(msgs)]
    except: return []

def salvar_mensagem(numero, role, content):
    db.collection('conversas').add({'numero':numero,'role':role,'content':content,'criado_em':firestore.SERVER_TIMESTAMP})

def enviar_push_expo(tokens, title, body, data=None):
    if not tokens: return
    messages = []
    for token in tokens:
        if not token or not token.startswith('ExponentPushToken'): continue
        msg = {'to':token,'sound':'default','title':title,'body':body,'priority':'high','channelId':'chamados','badge':1}
        if data: msg['data'] = data
        messages.append(msg)
    if not messages: return
    try:
        r = requests.post('https://exp.host/--/api/v2/push/send', json=messages,
            headers={'Content-Type':'application/json'}, timeout=10)
        print(f"[Push] {len(messages)} dispositivos: {r.status_code}")
    except Exception as e: print(f"[Push erro] {e}")

# ─── Verificação de elegibilidade do parceiro ─────────────────────────────────

def parceiro_elegivel(p_data, servico=None):
    """Verifica se o parceiro pode receber chamados."""
    if p_data.get('isBlocked', False): return False
    if p_data.get('disabledByAdmin', False): return False
    # Bloqueio por avaliação
    rating = p_data.get('rating', 5.0)
    if rating < MIN_RATING and not p_data.get('ratingOverride', False): return False
    # Bloqueio por alertas de retorno
    return_alerts = p_data.get('returnAlerts', 0)
    if return_alerts >= MAX_RETURN_ALERTS: return False
    # Filtro de serviço
    if servico:
        accepted = p_data.get('acceptedServices', ALL_SERVICES)
        # Verifica se algum serviço aceito corresponde à descrição
        servico_lower = servico.lower()
        for s in accepted:
            if any(word in servico_lower for word in s.lower().split()): return True
        return False
    return True

# ─── Despacho ─────────────────────────────────────────────────────────────────

def despachar_chamado(call_id, force_all=False):
    """Despacha chamado para parceiros elegíveis."""
    try:
        call_doc = db.collection('calls').document(call_id).get()
        if not call_doc.exists: return
        cd = call_doc.to_dict()
        servico = cd.get('description', '')
        is_return = cd.get('isReturn', False)
        preferred_partner = cd.get('preferredPartnerId')

        parceiros_snap = db.collection('partners').get()
        parceiros = [(p.id, p.to_dict()) for p in parceiros_snap]

        # Se é retorno e tem parceiro preferencial — envia só para ele
        if is_return and preferred_partner and not force_all:
            p_data = next((d for pid, d in parceiros if pid == preferred_partner), None)
            if p_data and parceiro_elegivel(p_data, servico):
                token = p_data.get('expoPushToken')
                db.collection('calls').document(call_id).update({
                    'status': 'dispatched',
                    'notifiedPartnerIds': [preferred_partner],
                    'dispatchedAt': firestore.SERVER_TIMESTAMP,
                })
                if token: enviar_push_expo([token], '🔔 Chamado de retorno!',
                    f"Cliente: {cd.get('clientName','')} — Garantia ativa",
                    {'callId': call_id, 'type': 'return_call'})
                print(f"[Despacho Retorno] {call_id} → {preferred_partner}")
                return

        # Filtra parceiros elegíveis e disponíveis
        elegíveis = [(pid, d) for pid, d in parceiros
                     if d.get('status') == 'available' and parceiro_elegivel(d, servico)]

        if not elegíveis:
            db.collection('calls').document(call_id).update({
                'status': 'open', 'openedAt': firestore.SERVER_TIMESTAMP,
            })
            all_tokens = [d.get('expoPushToken') for _, d in parceiros if d.get('expoPushToken') and not d.get('isBlocked') and not d.get('disabledByAdmin')]
            if all_tokens:
                enviar_push_expo(all_tokens, '📋 Chamado disponível',
                    f"{cd.get('clientName','')} — {cd.get('address','')}", {'callId': call_id, 'type': 'open_call'})
            return

        selected = [pid for pid, _ in elegíveis[:3]]
        tokens = [d.get('expoPushToken') for pid, d in elegíveis[:3] if d.get('expoPushToken')]

        db.collection('calls').document(call_id).update({
            'status': 'dispatched',
            'notifiedPartnerIds': selected,
            'dispatchedAt': firestore.SERVER_TIMESTAMP,
        })
        if tokens:
            enviar_push_expo(tokens, '🔔 Novo chamado!',
                f"{cd.get('clientName','')} — {cd.get('address','')}",
                {'callId': call_id, 'type': 'new_call'})
        print(f"[Despacho] {call_id} → {selected}")

    except Exception as e:
        print(f"[Despacho erro] {e}")

def abrir_chamado(dados, numero):
    doc_ref = db.collection('calls').add({
        'clientName': dados.get('nome',''), 'clientPhone': numero,
        'address': dados.get('endereco',''), 'neighborhood': '',
        'description': dados.get('problema',''), 'urgency': dados.get('urgencia','media'),
        'status': 'pending', 'notifiedPartnerIds': [], 'assignedPartnerId': None,
        'isReturn': False, 'warrantyDays': WARRANTY_DAYS,
        'createdAt': firestore.SERVER_TIMESTAMP,
    })
    return doc_ref[1].id

# ─── Rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return jsonify({'status':'ok','app':'Desentupi Pro Backend','version':'3.0'})

@app.route('/health')
def health(): return jsonify({'status':'healthy'})

# ── Webhook WhatsApp ──
@app.route('/webhook/wpp', methods=['POST'])
def webhook_wpp():
    data = request.json or {}
    msg_id = data.get('data',{}).get('key',{}).get('id','')
    if msg_id and msg_id in processed_ids: return jsonify({'status':'duplicate'}),200
    if msg_id: processed_ids.add(msg_id)
    if len(processed_ids)>10000: processed_ids.clear()
    if data.get('data',{}).get('key',{}).get('fromMe',False): return jsonify({'status':'ignored'}),200

    numero_raw = data.get('data',{}).get('key',{}).get('remoteJid','')
    numero = limpar_telefone(numero_raw)
    texto = (data.get('data',{}).get('message',{}).get('conversation','') or
             data.get('data',{}).get('message',{}).get('extendedTextMessage',{}).get('text','')).strip()
    if not numero or not texto: return jsonify({'status':'no_content'}),200

    salvar_mensagem(numero,'user',texto)
    historico = get_historico(numero)
    resposta_ia = chamar_groq(historico)
    dados_chamado = extrair_chamado(resposta_ia)

    if dados_chamado:
        call_id = abrir_chamado(dados_chamado, numero)
        despachar_chamado(call_id)
        resposta_limpa = limpar_saida(resposta_ia)
        if not resposta_limpa:
            resposta_limpa = f"✅ Chamado #{call_id[:6].upper()} aberto! Um técnico está sendo acionado 🔧"
    else:
        resposta_limpa = limpar_saida(resposta_ia)

    salvar_mensagem(numero,'assistant',resposta_limpa)
    enviar_whatsapp(numero_raw, resposta_limpa)
    return jsonify({'status':'ok'})

# ── Chamados ──
@app.route('/api/calls', methods=['GET'])
def listar_calls():
    status = request.args.get('status')
    try:
        q = db.collection('calls').order_by('createdAt',direction=firestore.Query.DESCENDING).limit(100)
        if status: q = db.collection('calls').where('status','==',status).order_by('createdAt',direction=firestore.Query.DESCENDING).limit(100)
        docs = q.get()
        calls = []
        for d in docs:
            data = d.to_dict(); data['id'] = d.id
            for k in ['createdAt','completedAt','acceptedAt','startedAt','dispatchedAt','openedAt']:
                if data.get(k) and hasattr(data[k],'isoformat'): data[k] = data[k].isoformat()
            calls.append(data)
        return jsonify({'calls':calls})
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/calls', methods=['POST'])
def criar_call():
    body = request.json or {}
    for f in ['clientName','clientPhone','address','description']:
        if not body.get(f): return jsonify({'error':f'Campo obrigatório: {f}'}),400
    try:
        is_return = body.get('isReturn', False)
        preferred_partner = None

        # Se é retorno, busca o parceiro que atendeu nos últimos 90 dias
        if is_return:
            cutoff = datetime.now(timezone.utc) - timedelta(days=WARRANTY_DAYS)
            prev = (db.collection('calls')
                .where('clientPhone','==',body['clientPhone'])
                .where('status','==','completed')
                .order_by('completedAt',direction=firestore.Query.DESCENDING)
                .limit(1).get())
            for p in prev:
                pd = p.to_dict()
                completed_at = pd.get('completedAt')
                if completed_at and hasattr(completed_at,'timestamp'):
                    if datetime.fromtimestamp(completed_at.timestamp(), tz=timezone.utc) > cutoff:
                        preferred_partner = pd.get('assignedPartnerId')
                        break

        doc_ref = db.collection('calls').add({
            'clientName':body['clientName'], 'clientPhone':body['clientPhone'],
            'address':body['address'], 'neighborhood':body.get('neighborhood',''),
            'description':body['description'], 'urgency':body.get('urgency','media'),
            'status':'pending', 'notifiedPartnerIds':[], 'assignedPartnerId':None,
            'isReturn': is_return,
            'preferredPartnerId': preferred_partner,
            'warrantyDays': WARRANTY_DAYS,
            'warrantyExpiresAt': None,
            'createdAt':firestore.SERVER_TIMESTAMP,
        })
        call_id = doc_ref[1].id
        force_all = body.get('forceAll', False)
        despachar_chamado(call_id, force_all=force_all)
        return jsonify({'success':True,'callId':call_id,'preferredPartner':preferred_partner}),201
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/calls/<call_id>/complete', methods=['POST'])
def completar_call(call_id):
    """Registra a conclusão com prazo de garantia."""
    body = request.json or {}
    try:
        warranty_expires = datetime.now(timezone.utc) + timedelta(days=WARRANTY_DAYS)
        db.collection('calls').document(call_id).update({
            'status': 'completed',
            'completedAt': firestore.SERVER_TIMESTAMP,
            'warrantyExpiresAt': warranty_expires,
            'warrantyDays': WARRANTY_DAYS,
            'valor': body.get('valor'),
            'paymentMethod': body.get('paymentMethod'),
            'serviceNotes': body.get('serviceNotes',''),
            'clientRating': body.get('clientRating',5),
        })
        return jsonify({'success':True,'warrantyDays':WARRANTY_DAYS})
    except Exception as e: return jsonify({'error':str(e)}),500

# ── Parceiros ──
@app.route('/api/partners', methods=['GET'])
def listar_partners():
    try:
        docs = db.collection('partners').get()
        partners = []
        for d in docs:
            data = d.to_dict(); data['id'] = d.id
            # Conta atendimentos e faturamento
            calls = db.collection('calls').where('assignedPartnerId','==',d.id).where('status','==','completed').get()
            data['totalCompleted'] = len(calls)
            data['totalRevenue'] = sum(float(c.to_dict().get('valor',0) or 0) for c in calls)
            partners.append(data)
        return jsonify({'partners':partners})
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/partners/<partner_id>/toggle', methods=['POST'])
def toggle_partner(partner_id):
    """Habilita ou desabilita parceiro manualmente."""
    try:
        doc = db.collection('partners').document(partner_id).get()
        if not doc.exists: return jsonify({'error':'Parceiro não encontrado'}),404
        current = doc.to_dict().get('disabledByAdmin', False)
        db.collection('partners').document(partner_id).update({
            'disabledByAdmin': not current,
            'updatedAt': firestore.SERVER_TIMESTAMP,
        })
        return jsonify({'success':True,'disabled': not current})
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/partners/<partner_id>/override-rating', methods=['POST'])
def override_rating(partner_id):
    """Admin libera parceiro com nota baixa."""
    try:
        db.collection('partners').document(partner_id).update({
            'ratingOverride': True, 'updatedAt': firestore.SERVER_TIMESTAMP,
        })
        return jsonify({'success':True})
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/partners/<partner_id>/clear-alerts', methods=['POST'])
def clear_alerts(partner_id):
    """Admin limpa alertas de retorno do parceiro."""
    try:
        db.collection('partners').document(partner_id).update({
            'returnAlerts': 0, 'isBlocked': False, 'updatedAt': firestore.SERVER_TIMESTAMP,
        })
        return jsonify({'success':True})
    except Exception as e: return jsonify({'error':str(e)}),500

# ── Cron ──
@app.route('/api/cron/processar', methods=['GET'])
def cron_processar():
    key = request.args.get('key','')
    if key != WEBHOOK_SECRET: return jsonify({'error':'Unauthorized'}),401
    try:
        agora = datetime.now(timezone.utc)
        reprocessados = 0

        # 1. Dispatched há >30s sem aceite → open + notifica todos
        dispatched = db.collection('calls').where('status','==','dispatched').get()
        for d in dispatched:
            data = d.to_dict()
            dt = data.get('dispatchedAt')
            if dt and hasattr(dt,'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(),tz=timezone.utc)).total_seconds() > 30:
                    db.collection('calls').document(d.id).update({'status':'open','openedAt':firestore.SERVER_TIMESTAMP})
                    # Alerta de retorno se era chamado de retorno
                    if data.get('isReturn') and data.get('preferredPartnerId'):
                        pid = data['preferredPartnerId']
                        p = db.collection('partners').document(pid).get()
                        if p.exists:
                            alerts = p.to_dict().get('returnAlerts',0) + 1
                            updates = {'returnAlerts': alerts}
                            if alerts >= MAX_RETURN_ALERTS: updates['isBlocked'] = True
                            db.collection('partners').document(pid).update(updates)
                    parceiros = db.collection('partners').get()
                    tokens = [p.to_dict().get('expoPushToken') for p in parceiros if p.to_dict().get('expoPushToken') and not p.to_dict().get('isBlocked') and not p.to_dict().get('disabledByAdmin')]
                    if tokens:
                        enviar_push_expo(tokens,'📋 Chamado disponível',f"{data.get('clientName','')} — {data.get('address','')}", {'callId':d.id,'type':'open_call'})
                    reprocessados += 1

        # 2. Pending há >10s → despacha
        pending = db.collection('calls').where('status','==','pending').get()
        for d in pending:
            data = d.to_dict()
            dt = data.get('createdAt')
            if dt and hasattr(dt,'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(),tz=timezone.utc)).total_seconds() > 10:
                    despachar_chamado(d.id)
                    reprocessados += 1

        return jsonify({'reprocessados':reprocessados})
    except Exception as e: return jsonify({'error':str(e)}),500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
