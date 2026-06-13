# app.py — Backend Desentupi Pro v2
# Flask + Firebase + Groq IA + Evolution API + Expo Push

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

# ─── Push via Expo ────────────────────────────────────────────────────────────

def enviar_push_expo(tokens, title, body, data=None):
    """Envia push notification via Expo Push API."""
    if not tokens: return
    messages = []
    for token in tokens:
        if not token or not token.startswith('ExponentPushToken'): continue
        msg = {'to': token, 'sound': 'default', 'title': title, 'body': body, 'priority': 'high',
               'channelId': 'chamados', 'badge': 1}
        if data: msg['data'] = data
        messages.append(msg)
    if not messages: return
    try:
        r = requests.post('https://exp.host/--/api/v2/push/send',
            json=messages, headers={'Content-Type':'application/json'}, timeout=10)
        print(f"[Push] Enviado para {len(messages)} dispositivos: {r.status_code}")
    except Exception as e:
        print(f"[Push erro] {e}")

# ─── Despacho ─────────────────────────────────────────────────────────────────

def despachar_chamado(call_id):
    """Busca parceiros disponíveis e notifica via Firestore + Push."""
    try:
        parceiros = db.collection('partners').where('isBlocked','==',False).get()
        ids = [p.id for p in parceiros if p.to_dict().get('status') in ('available',)]
        tokens = [p.to_dict().get('expoPushToken') for p in parceiros
                  if p.id in ids and p.to_dict().get('expoPushToken')]

        if not ids:
            # Nenhum disponível — marca como aberto para aceite manual
            db.collection('calls').document(call_id).update({
                'status': 'open',
                'openedAt': firestore.SERVER_TIMESTAMP,
            })
            print(f"[Despacho] Nenhum parceiro disponível — chamado {call_id} marcado como aberto")
            # Notifica todos mesmo assim para verem em "Chamados abertos"
            all_tokens = [p.to_dict().get('expoPushToken') for p in parceiros if p.to_dict().get('expoPushToken')]
            if all_tokens:
                call_doc = db.collection('calls').document(call_id).get()
                cd = call_doc.to_dict() if call_doc.exists else {}
                enviar_push_expo(all_tokens, '📋 Novo chamado aberto',
                    f"{cd.get('clientName','Cliente')} — {cd.get('address','')}",
                    {'callId': call_id, 'type': 'open_call'})
            return

        # Limita a 3 parceiros
        selected = ids[:3]
        db.collection('calls').document(call_id).update({
            'status': 'dispatched',
            'notifiedPartnerIds': selected,
            'dispatchedAt': firestore.SERVER_TIMESTAMP,
        })
        print(f"[Despacho] Chamado {call_id} → {selected}")

        # Envia push para os selecionados
        selected_tokens = [p.to_dict().get('expoPushToken') for p in parceiros
                          if p.id in selected and p.to_dict().get('expoPushToken')]
        if selected_tokens:
            call_doc = db.collection('calls').document(call_id).get()
            cd = call_doc.to_dict() if call_doc.exists else {}
            enviar_push_expo(selected_tokens, '🔔 Novo chamado!',
                f"{cd.get('clientName','Cliente')} — {cd.get('address','')}",
                {'callId': call_id, 'type': 'new_call'})

    except Exception as e:
        print(f"[Despacho erro] {e}")

def abrir_chamado(dados, numero):
    doc_ref = db.collection('calls').add({
        'clientName': dados.get('nome',''), 'clientPhone': numero,
        'address': dados.get('endereco',''), 'neighborhood': '',
        'description': dados.get('problema',''), 'urgency': dados.get('urgencia','media'),
        'status': 'pending', 'notifiedPartnerIds': [], 'assignedPartnerId': None,
        'createdAt': firestore.SERVER_TIMESTAMP,
    })
    return doc_ref[1].id

# ─── Rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return jsonify({'status':'ok','app':'Desentupi Pro Backend','version':'2.0'})

@app.route('/health')
def health(): return jsonify({'status':'healthy','timestamp':datetime.now(timezone.utc).isoformat()})

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
    return jsonify({'status':'ok','chamado_aberto':dados_chamado is not None})

# ── Chamados ──
@app.route('/api/calls', methods=['GET'])
def listar_calls():
    status = request.args.get('status')
    try:
        q = db.collection('calls').order_by('createdAt',direction=firestore.Query.DESCENDING).limit(50)
        if status: q = db.collection('calls').where('status','==',status).order_by('createdAt',direction=firestore.Query.DESCENDING).limit(50)
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
        doc_ref = db.collection('calls').add({
            'clientName':body['clientName'],'clientPhone':body['clientPhone'],
            'address':body['address'],'neighborhood':body.get('neighborhood',''),
            'description':body['description'],'urgency':body.get('urgency','media'),
            'status':'pending','notifiedPartnerIds':[],'assignedPartnerId':None,
            'createdAt':firestore.SERVER_TIMESTAMP,
        })
        call_id = doc_ref[1].id
        despachar_chamado(call_id)
        return jsonify({'success':True,'callId':call_id}),201
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/calls/<call_id>', methods=['GET'])
def get_call(call_id):
    doc = db.collection('calls').document(call_id).get()
    if not doc.exists: return jsonify({'error':'Não encontrado'}),404
    data = doc.to_dict(); data['id'] = doc.id
    return jsonify(data)

# ── Parceiros ──
@app.route('/api/partners', methods=['GET'])
def listar_partners():
    try:
        docs = db.collection('partners').get()
        return jsonify({'partners':[{**d.to_dict(),'id':d.id} for d in docs]})
    except Exception as e: return jsonify({'error':str(e)}),500

# ── Cron — redespacho + manutenção ──
@app.route('/api/cron/processar', methods=['GET'])
def cron_processar():
    key = request.args.get('key','')
    if key != WEBHOOK_SECRET: return jsonify({'error':'Unauthorized'}),401
    try:
        agora = datetime.now(timezone.utc)
        reprocessados = 0

        # 1. Chamados dispatched há mais de 30s sem aceite → abre para todos
        dispatched = db.collection('calls').where('status','==','dispatched').get()
        for d in dispatched:
            data = d.to_dict()
            dt = data.get('dispatchedAt')
            if dt and hasattr(dt,'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)).total_seconds() > 30:
                    db.collection('calls').document(d.id).update({
                        'status': 'open',
                        'openedAt': firestore.SERVER_TIMESTAMP,
                    })
                    # Notifica todos os parceiros
                    parceiros = db.collection('partners').get()
                    tokens = [p.to_dict().get('expoPushToken') for p in parceiros if p.to_dict().get('expoPushToken')]
                    if tokens:
                        enviar_push_expo(tokens, '📋 Chamado disponível',
                            f"{data.get('clientName','')} — {data.get('address','')}",
                            {'callId': d.id, 'type': 'open_call'})
                    reprocessados += 1

        # 2. Chamados pending há mais de 10s → despacha
        pending = db.collection('calls').where('status','==','pending').get()
        for d in pending:
            data = d.to_dict()
            dt = data.get('createdAt')
            if dt and hasattr(dt,'timestamp'):
                if (agora - datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)).total_seconds() > 10:
                    despachar_chamado(d.id)
                    reprocessados += 1

        return jsonify({'reprocessados':reprocessados})
    except Exception as e: return jsonify({'error':str(e)}),500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
