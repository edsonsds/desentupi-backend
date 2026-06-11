# Desentupi Pro — Backend

## Arquitetura
```
WhatsApp → Evolution API → Flask (Render) → Firebase Firestore → App do parceiro
```

## Deploy no Render (grátis)

### 1. Criar conta e novo serviço
1. Acesse render.com e crie uma conta
2. Clique em "New" → "Web Service"
3. Conecte seu GitHub e faça upload deste projeto
4. Configurações:
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`

### 2. Variáveis de ambiente no Render
Configure em "Environment":

| Variável | Valor |
|---|---|
| FIREBASE_CREDENTIALS | JSON completo da conta de serviço Firebase |
| GROQ_API_KEY | Chave do Groq (grátis em console.groq.com) |
| EVOLUTION_URL | URL da sua VPS com Evolution API |
| EVOLUTION_KEY | Chave da Evolution API |
| EVOLUTION_INSTANCE | Nome da instância (ex: desentupi) |
| WEBHOOK_SECRET | Senha para o cron (qualquer string) |

### 3. Obter FIREBASE_CREDENTIALS
1. Firebase Console → Configurações → Contas de serviço
2. Clique em "Gerar nova chave privada"
3. Baixe o JSON
4. Cole o conteúdo completo como valor da variável FIREBASE_CREDENTIALS

### 4. Obter GROQ_API_KEY (grátis)
1. Acesse console.groq.com
2. Crie uma conta gratuita
3. Vá em "API Keys" → "Create API Key"
4. Copie a chave

### 5. Configurar webhook da Evolution
Após subir o backend no Render, configure o webhook na Evolution API:
URL: https://SEU-BACKEND.onrender.com/webhook/wpp

### 6. Cron de manutenção (grátis)
1. Acesse cron-job.org
2. Crie um job apontando para:
   GET https://SEU-BACKEND.onrender.com/api/cron/processar?key=SUA_WEBHOOK_SECRET
3. Frequência: a cada 5 minutos

## Painel Admin
Abra admin.html no navegador e troque a URL da API pela URL do Render.

## Rotas da API

| Método | Rota | Descrição |
|---|---|---|
| GET | / | Status do servidor |
| GET | /health | Health check |
| POST | /webhook/wpp | Webhook WhatsApp |
| GET | /api/calls | Lista chamados |
| POST | /api/calls | Cria chamado manualmente |
| GET | /api/calls/:id | Detalhe do chamado |
| POST | /api/calls/:id/dispatch | Redespacha chamado |
| GET | /api/partners | Lista parceiros |
| GET | /api/cron/processar | Manutenção automática |
