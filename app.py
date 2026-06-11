# app.py — Backend Desentupi Pro
# Flask + Firebase + Groq IA + Evolution API (WhatsApp)

import os, re, json, hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import requests

app = Flask(__name__)
CORS(app)

# ─── Firebase ─────────────────────────────────────────────────────────────────
# Inicializa usando variável de ambiente FIREBASE_CREDENTIALS (JSON string)
cred_json = json.loads(os.environ.get('FIREBASE_CREDENTIALS', '{}'))
if not firebase_admin._apps:
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ─── Variáveis de ambiente ────────────────────────────────────────────────────
GROQ_API_KEY       = os.environ.get('GROQ_API_KEY', '')
EVOLUTION_URL      = os.environ.get('EVOLUTION_URL', '')       # ex: https://sua-vps.com:8080
EVOLUTION_KEY      = os.environ.get('EVOLUTION_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'desentupi')
WEBHOOK_SECRET     = os.environ.get('WEBHOOK_SECRET', 'secret123')

# IDs já processados (anti-duplicação em memória — suficiente para início)
processed_ids = set()

# ─── System prompt da IA ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é a atendente virtual do Desentupi Pro, empresa de desentupimento em São Paulo.
Seja rápida, educada e objetiva. Seu trabalho é entender o problema e abrir um chamado.
Colete: nome do cliente, endereço completo (rua, número, bairro, referência), tipo de entupimento e se é urgente.
Pergunte UMA coisa por vez, de forma natural. Não invente preços nem prazos.
Quando tiver TODOS os dados (nome, endereço, problema, urgência), confirme com o cliente e finalize com:
[ABRIR_CHAMADO]{"nome":"","endereco":"","problema":"","urgencia":"alta|media|baixa"}[/ABRIR_CHAMADO]
Nunca mostre esse bloco ao cliente."""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def limpar_telefone(numero: str) -> str:
    """Normaliza número para os últimos 11 dígitos."""
    digits = re.sub(r'\D', '', numero)
    return digits[-11:] if len(digits) >= 11 else digits

def enviar_whatsapp(numero: str, texto: str):
    """Envia mensagem via Evolution API."""
    if not EVOLUTION_URL:
        print(f"[WhatsApp] {numero}: {texto}")
        return
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {'apikey': EVOLUTION_KEY, 'Content-Type': 'application/json'}
    payload = {
        'number': numero,
        'options': {'delay': 800, 'presence': 'composing'},
        'textMessage': {'text': texto}
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"[WhatsApp erro] {e}")

def chamar_groq(historico: list) -> str:
    """Chama a IA Groq e retorna o texto da resposta."""
    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'llama-3.1-8b-instant',
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}] + historico,
        'max_tokens': 400,
        'temperature': 0.6
    }
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            json=payload, headers=headers, timeout=15
        )
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"[Groq erro] {e}")
        return "Desculpe, tive um problema técnico. Pode repetir?"

def extrair_chamado(texto: str):
    """Extrai JSON do bloco [ABRIR_CHAMADO] se presente."""
    match = re.search(r'\[ABRIR_CHAMADO\](.*?)\[/ABRIR_CHAMADO\]', texto, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except:
            return None
    return None

def limpar_saida(texto: str) -> str:
    """Remove blocos internos da resposta antes de enviar ao cliente."""
    texto = re.sub(r'\[ABRIR_CHAMADO\].*?\[/ABRIR_CHAMADO\]', '', texto, flags=re.DOTALL)
    texto = re.sub(r'\[.*?\]', '', texto)
    return texto.strip()

def get_historico(numero: str, limite: int = 15) -> list:
    """Busca as últimas mensagens da conversa no Firestore."""
    try:
        msgs = (
            db.collection('conversas')
            .where('numero', '==', numero)
            .order_by('criado_em', direction=firestore.Query.DESCENDING)
            .limit(limite)
            .get()
        )
        historico = []
        for m in reversed(msgs):
            d = m.to_dict()
            historico.append({'role': d['role'], 'content': d['content']})
        return historico
    except:
        return []

def salvar_mensagem(numero: str, role: str, content: str):
    """Salva mensagem no Firestore."""
    db.collection('conversas').add({
        'numero':    numero,
        'role':      role,
        'content':   content,
        'criado_em': firestore.SERVER_TIMESTAMP
    })

def abrir_chamado(dados: dict, numero: str) -> str:
    """Cria chamado no Firestore e retorna o ID."""
    doc_ref = db.collection('calls').add({
        'clientName':        dados.get('nome', ''),
        'clientPhone':       numero,
        'address':           dados.get('endereco', ''),
        'neighborhood':      '',
        'description':       dados.get('problema', ''),
        'urgency':           dados.get('urgencia', 'media'),
        'status':            'pending',
        'notifiedPartnerIds':[],
        'assignedPartnerId': None,
        'createdAt':         firestore.SERVER_TIMESTAMP,
    })
    return doc_ref[1].id

def despachar_chamado(call_id: str):
    """
    Busca parceiros disponíveis e notifica via Firestore.
    Em produção: substituir por Cloud Function com Haversine.
    """
    try:
        parceiros = (
            db.collection('partners')
            .where('status', '==', 'available')
            .limit(3)
            .get()
        )
        ids = [p.id for p in parceiros]
        if ids:
            db.collection('calls').document(call_id).update({
                'status':             'dispatched',
                'notifiedPartnerIds': ids,
            })
            print(f"[Despacho] Chamado {call_id} enviado para: {ids}")
        else:
            print(f"[Despacho] Nenhum parceiro disponível para {call_id}")
    except Exception as e:
        print(f"[Despacho erro] {e}")

# ─── Rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'app': 'Desentupi Pro Backend', 'version': '1.0'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now(timezone.utc).isoformat()})

# ── Webhook WhatsApp ──────────────────────────────────────────────────────────
@app.route('/webhook/wpp', methods=['POST'])
def webhook_wpp():
    data = request.json or {}

    # Anti-duplicação
    msg_id = data.get('data', {}).get('key', {}).get('id', '')
    if msg_id and msg_id in processed_ids:
        return jsonify({'status': 'duplicate'}), 200
    if msg_id:
        processed_ids.add(msg_id)
        if len(processed_ids) > 10000:
            processed_ids.clear()

    # Ignora mensagens enviadas pelo próprio número
    from_me = data.get('data', {}).get('key', {}).get('fromMe', False)
    if from_me:
        return jsonify({'status': 'ignored'}), 200

    # Extrai número e texto
    numero_raw = data.get('data', {}).get('key', {}).get('remoteJid', '')
    numero     = limpar_telefone(numero_raw)
    texto      = (
        data.get('data', {}).get('message', {}).get('conversation', '') or
        data.get('data', {}).get('message', {}).get('extendedTextMessage', {}).get('text', '')
    ).strip()

    if not numero or not texto:
        return jsonify({'status': 'no_content'}), 200

    print(f"[WhatsApp] {numero}: {texto}")

    # Salva mensagem do cliente
    salvar_mensagem(numero, 'user', texto)

    # Monta histórico e chama IA
    historico = get_historico(numero)
    resposta_ia = chamar_groq(historico)

    # Verifica se IA quer abrir chamado
    dados_chamado = extrair_chamado(resposta_ia)
    if dados_chamado:
        call_id = abrir_chamado(dados_chamado, numero)
        despachar_chamado(call_id)
        resposta_limpa = limpar_saida(resposta_ia)
        if not resposta_limpa:
            resposta_limpa = (
                f"✅ Perfeito! Seu chamado #{call_id[:6].upper()} foi aberto.\n"
                f"Um técnico está sendo acionado e em breve você recebe a confirmação! 🔧"
            )
    else:
        resposta_limpa = limpar_saida(resposta_ia)

    # Salva resposta da IA e envia ao cliente
    salvar_mensagem(numero, 'assistant', resposta_limpa)
    enviar_whatsapp(numero_raw, resposta_limpa)

    return jsonify({'status': 'ok', 'chamado_aberto': dados_chamado is not None})

# ── Chamados (API REST para painel admin) ─────────────────────────────────────
@app.route('/api/calls', methods=['GET'])
def listar_calls():
    """Lista chamados — filtra por status se passado como query param."""
    status = request.args.get('status')
    try:
        q = db.collection('calls').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(50)
        if status:
            q = db.collection('calls').where('status', '==', status).order_by('createdAt', direction=firestore.Query.DESCENDING).limit(50)
        docs = q.get()
        calls = []
        for d in docs:
            data = d.to_dict()
            data['id'] = d.id
            if data.get('createdAt'):
                data['createdAt'] = data['createdAt'].isoformat() if hasattr(data['createdAt'], 'isoformat') else str(data['createdAt'])
            calls.append(data)
        return jsonify({'calls': calls})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/calls', methods=['POST'])
def criar_call():
    """Cria chamado manualmente (painel admin)."""
    body = request.json or {}
    required = ['clientName', 'clientPhone', 'address', 'description']
    for field in required:
        if not body.get(field):
            return jsonify({'error': f'Campo obrigatório: {field}'}), 400
    try:
        doc_ref = db.collection('calls').add({
            'clientName':        body['clientName'],
            'clientPhone':       body['clientPhone'],
            'address':           body['address'],
            'neighborhood':      body.get('neighborhood', ''),
            'description':       body['description'],
            'urgency':           body.get('urgency', 'media'),
            'status':            'pending',
            'notifiedPartnerIds':[],
            'assignedPartnerId': None,
            'createdAt':         firestore.SERVER_TIMESTAMP,
        })
        call_id = doc_ref[1].id
        despachar_chamado(call_id)
        return jsonify({'success': True, 'callId': call_id}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/calls/<call_id>', methods=['GET'])
def get_call(call_id):
    doc = db.collection('calls').document(call_id).get()
    if not doc.exists:
        return jsonify({'error': 'Chamado não encontrado'}), 404
    data = doc.to_dict()
    data['id'] = doc.id
    return jsonify(data)

@app.route('/api/calls/<call_id>/dispatch', methods=['POST'])
def despachar_manual(call_id):
    """Dispara chamado manualmente para parceiros disponíveis."""
    despachar_chamado(call_id)
    return jsonify({'success': True})

# ── Parceiros ──────────────────────────────────────────────────────────────────
@app.route('/api/partners', methods=['GET'])
def listar_partners():
    try:
        docs = db.collection('partners').get()
        partners = []
        for d in docs:
            data = d.to_dict()
            data['id'] = d.id
            partners.append(data)
        return jsonify({'partners': partners})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Cron — manutenção ──────────────────────────────────────────────────────────
@app.route('/api/cron/processar', methods=['GET'])
def cron_processar():
    """
    Chamado pelo cron-job.org a cada 5 minutos.
    Redespacha chamados pending sem parceiro há mais de 2 minutos.
    """
    key = request.args.get('key', '')
    if key != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        docs = (
            db.collection('calls')
            .where('status', '==', 'pending')
            .get()
        )
        reprocessados = 0
        for d in docs:
            data = d.to_dict()
            created = data.get('createdAt')
            if created and hasattr(created, 'timestamp'):
                if created.timestamp() < cutoff.timestamp():
                    despachar_chamado(d.id)
                    reprocessados += 1
        return jsonify({'reprocessados': reprocessados})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
