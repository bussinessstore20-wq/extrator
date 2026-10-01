# Extrator — Mercado Livre + Telegram

Coletor automático de produtos do Mercado Livre Brasil com fila de aprovação no Telegram. O banco usado é o projeto Supabase **SentinelChat**; as tabelas do Extrator têm prefixo `extrator_` para ficarem separadas das tabelas do SentinelChat.

## Estado inicial seguro
- `COLLECTOR_ENABLED=false`: a coleta fica desligada até que API, credenciais e limites sejam validados.
- A publicação exige aprovação explícita no Telegram.
- O backend usa a chave Supabase service role somente no servidor.
- Não coloque tokens no GitHub. Configure segredos em Render > Environment.

## Credenciais Render
- `SUPABASE_URL`: já definido para o projeto SentinelChat.
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase Dashboard > Project Settings > API Keys; segredo apenas no Render.
- `TELEGRAM_BOT_TOKEN`: token do @BotFather.
- `TELEGRAM_ADMIN_IDS`: IDs numéricos separados por vírgula autorizados a aprovar/reprovar.
- `TELEGRAM_CHANNEL_ID`: @canal ou ID numérico; o bot deve ser administrador do canal.
- `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REDIRECT_URI`: aplicação criada em https://developers.mercadolivre.com.br/
- `ML_ACCESS_TOKEN`, `ML_REFRESH_TOKEN`: tokens OAuth válidos da aplicação/conta, se exigidos pelos endpoints habilitados.
- `ML_OAUTH_STATE`: string aleatória longa usada para validar o callback OAuth.

## OAuth
Cadastre exatamente o valor de `ML_REDIRECT_URI` na aplicação do Mercado Livre. Abra `/oauth/mercadolivre/start`, autorize a aplicação e retorne ao callback. O código troca os tokens no servidor e salva tokens na tabela privada por RLS `extrator_settings`; eles nunca são devolvidos pela API HTTP.

## Rotas
- `GET /health`: health check.
- `GET /status`: estado das configurações sem revelar segredos.
- `GET /oauth/mercadolivre/start`: inicia autorização quando configurada.
- `GET /oauth/mercadolivre/callback`: troca o código OAuth.
- `POST /admin/collector/run`: solicita uma coleta manual protegida por `X-Admin-Secret` (configurar `ADMIN_API_SECRET`).

## Banco
O SQL inicial está em `supabase/schema.sql`. Ele cria tabelas `extrator_*` no projeto SentinelChat, habilita RLS e nega acesso às roles públicas; o backend usa a service-role key.

## Limitações importantes
A busca não garante varredura instantânea de todo o catálogo. O worker percorre categorias em lotes, preserva cursor e deve respeitar os limites e permissões reais da API. Acesso a recursos, preço, estoque e links de afiliado depende das permissões concedidas pelo Mercado Livre. Link de afiliado não é presumido nem fabricado.
