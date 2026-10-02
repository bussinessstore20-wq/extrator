# Extrator — Mercado Livre + Shopee + Telegram

Coletor de ofertas do Mercado Livre Brasil e da Shopee com fila de aprovação no Telegram. O banco usado é o projeto Supabase **SentinelChat**; as tabelas do Extrator têm prefixo `extrator_` para ficarem separadas das tabelas do SentinelChat.

## Estado inicial seguro
- `COLLECTOR_ENABLED=false`: a coleta automática do Mercado Livre fica desligada até que API, credenciais e limites sejam validados.
- `SHOPEE_COLLECTOR_ENABLED=false`: a coleta automática da Shopee também fica desligada por padrão; primeiro valide a coleta manual com credenciais reais.
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
- `SHOPEE_AFFILIATE_APP_ID`, `SHOPEE_AFFILIATE_SECRET`: credenciais da Open API de Afiliados Shopee; configure apenas no Render, nunca no GitHub.
- `SHOPEE_COLLECTOR_ENABLED=false`: mantenha `false` até o teste manual trazer produtos reais.
- `SHOPEE_COLLECTOR_INTERVAL_SECONDS=1800`: intervalo mínimo de 5 minutos entre ciclos automáticos.
- `SHOPEE_CATEGORIES_PER_CYCLE=5`, `SHOPEE_MAX_ITEMS_PER_CATEGORY=20`: limite de categorias e itens por busca.

## OAuth
Cadastre exatamente o valor de `ML_REDIRECT_URI` na aplicação do Mercado Livre. Abra `/oauth/mercadolivre/start`, autorize a aplicação e retorne ao callback. O código troca os tokens no servidor e salva tokens na tabela privada por RLS `extrator_settings`; eles nunca são devolvidos pela API HTTP.

## Rotas
- `GET /health`: health check.
- `GET /status`: estado das configurações sem revelar segredos.
- `GET /oauth/mercadolivre/start`: inicia autorização quando configurada.
- `GET /oauth/mercadolivre/callback`: troca o código OAuth.
- `POST /admin/collector/run`: coleta manual do Mercado Livre, protegida por `X-Admin-Secret`.
- `POST /admin/shopee/collector/run`: testa manualmente a API oficial de Afiliados Shopee, também protegida por `X-Admin-Secret`; não ativa a coleta automática.

## Banco
O SQL inicial está em `supabase/schema.sql`. Ele cria tabelas `extrator_*` no projeto SentinelChat, habilita RLS e nega acesso às roles públicas; o backend usa a service-role key.

## Limitações importantes
A busca não garante varredura instantânea de todo o catálogo. O worker percorre categorias em lotes, preserva cursor e deve respeitar os limites e permissões reais da API. Acesso a recursos, preço, estoque e links de afiliado depende das permissões concedidas pelas plataformas. A Shopee usa a Open API de Afiliados via GraphQL com assinatura SHA-256; não fazemos scraping nem tentamos contornar bloqueios. O campo de link de afiliado é priorizado quando a API o retorna; nenhum link é fabricado.
