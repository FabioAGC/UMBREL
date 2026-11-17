# fabrica-apps

Todas as aplicações da fábrica servidas pelo mini PC (Umbrel), num endereço só:

```
https://apps-fabrica.<dominio>/
├── /                portal
├── /quiz-ma/        quiz de MA          → Apps Script → Google Sheets (sem mudança)
├── /quiz-5s/        quiz de 5S          → Apps Script → Google Sheets (sem mudança)
├── /historian/      2 painéis           → historian-api (cache) → Historian REST + NTLM
├── /mes/            últimos 7 dias      → mes-api (cache)       → MES REST + OAuth2
├── /ocr/            validação de laudo  → ocr (laudo-check)     → Ollama local
├── /admin/          🔒 ETL + base do OCR
├── /etl/api/        🔒 etl-mes (agendada) → Snowflake
├── /dimep/          🔒 dashboard DIMEP (dados de RH)
├── /mcp             🔑 MCP do MES (token Bearer)
└── /ca.crt          certificado raiz para instalar nos dispositivos
```

Só o **proxy (Caddy)** publica porta, a **443**. Nenhum backend fica exposto, e os navegadores nunca falam
direto com o Historian ou com o MES: os serviços consultam em segundo plano e servem um cache.
Com isso, 1 tela ou 100 telas abertas geram a mesma carga nos sistemas da planta.

🔒 = usuário e senha no proxy. 🔑 = token no gateway do MCP.

---

## 1. O que precisa antes

| Item | Com quem |
|---|---|
| IP fixo + nome DNS para o mini PC (ex.: `apps-fabrica.genmills.local`) | TI |
| Conta de serviço AD com leitura no Historian e no MES (não use a sua) | TI |
| Usuário de serviço no Snowflake com **key-pair** e permissão de escrita no schema destino | time de dados |
| Login SQL de leitura no banco do DIMEP | TI / RH |
| Aprovação para rodar o servidor na rede da planta | TI / OT |

> **MCP-MES:** o `config/config.json` do repo público tinha credenciais reais. Antes de subir, torne o repo
> privado e troque o client secret e a senha. Aqui elas vêm só do ambiente (`MES_*`).

## 2. Publicar as imagens (uma vez)

1. Crie um repositório **privado** `fabrica-apps` no GitHub e suba esta pasta.
2. O workflow `.github/workflows/build.yml` constrói as 7 imagens e publica no GHCR a cada push na `main`
   (`ghcr.io/fabioagc/fabrica-*`). Construir no GitHub poupa o mini PC (o OCR compila com OpenCV e Tesseract).
3. Crie um token clássico do GitHub com `read:packages` (para o Umbrel baixar as imagens) e outro
   com `repo` (para o Portainer ler o repo). Pode ser o mesmo token com os dois escopos.

## 3. Subir no Umbrel

1. **App Store do Umbrel → Portainer → Install.** Abra o Portainer e troque a senha padrão.
2. **Registries → Add registry → Custom:** URL `ghcr.io`, usuário `FabioAGC`, senha = token com `read:packages`.
3. **Stacks → Add stack → Repository:**
   - Repository URL: `https://github.com/FabioAGC/fabrica-apps`, com autenticação (usuário + token)
   - Compose path: `docker-compose.yml`
   - **Environment variables:** cadastre cada linha do `.env.example` com os valores reais
   - (Opcional) **GitOps updates → Polling a cada 5 min:** todo push na `main` reimplanta sozinho
4. **Deploy the stack.** Na primeira vez, o `ollama-pull` baixa o modelo (~2 GB) e sai. Isso é esperado.

Para gerar os valores:

```bash
# hash da senha de admin (cole o resultado em ADMIN_PASSWORD_HASH)
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'SENHA_FORTE'

# token do MCP
openssl rand -hex 32

# chave do Snowflake (registre a pública no usuário: ALTER USER ... SET RSA_PUBLIC_KEY='...')
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
base64 -w0 rsa_key.p8      # -> SNOWFLAKE_PRIVATE_KEY_B64
```

### Regras do Umbrel que este stack já respeita
- **Só volumes nomeados.** No Portainer do Umbrel, bind mounts somem quando ele reinicia. Configurações e
  páginas vão dentro das imagens; dados (`etl_data`, `ocr_data`, `dimep_data`, `ollama`, `caddy_data`) ficam em volumes.
- **Porta 80 é do Umbrel.** O Caddy só atende HTTPS (443). Divulgue o link sempre com `https://`; quem digitar
  `http://` cai no painel do Umbrel.
- **Nada de porta extra:** os backends não publicam porta. Para depurar um, use o console do container no Portainer.

## 4. Primeiro acesso nos dispositivos

O certificado é emitido pela CA interna do Caddy (`tls internal`). Em cada PC, tablet ou celular:

1. Abra `https://apps-fabrica.<dominio>/ca.crt` e baixe o certificado.
2. Instale como **Autoridade de Certificação Raiz Confiável** (no Windows, dá para a TI distribuir por GPO).

Se a TI preferir, ela emite um certificado da CA corporativa. Nesse caso, troque `tls internal` no `Caddyfile`
por `tls /data/certs/cert.pem /data/certs/key.pem` e coloque os dois arquivos no volume `caddy_data`.

**Não perca o volume `caddy_data`:** ele guarda a CA. Se ele for apagado, todos os dispositivos precisam do certificado novo.

## 5. Onde ajustar cada app

| App | Arquivo | O que colocar |
|---|---|---|
| Quiz MA / 5S | `proxy/site/quiz-ma/index.html`, `proxy/site/quiz-5s/index.html` | O HTML final de cada quiz (com a URL `/exec` atual do Apps Script) |
| Painéis do Historian | `services/historian-api/paineis.yaml` | As tags dos seus 2 painéis (já vêm as do balanço de oleína) |
| Painéis (visual) | `proxy/site/historian/index.html` | Se quiser reaproveitar o HTML atual, troque a leitura por `fetch('/historian/api/paineis/<id>')` |
| MES 7 dias + ETL | `services/shared/mes_client.py` → `buscar_registros()` | A consulta real do MES. As duas apps usam essa mesma função |
| ETL | variáveis `ETL_*` e `MES_COLUNA_DATA` | Intervalo, janela inicial, sobreposição e tabela destino |
| OCR | tela `/admin/` | Envie as guidelines (PDF por material). Elas ficam no volume, fora do git |
| DIMEP | — | Na 1ª vez, rode a carga histórica (abaixo) |

### Carga inicial do DIMEP
O scheduler atualiza só o mês corrente. Os anos anteriores saem de uma carga única. No Portainer, abra
**Containers → fabrica-apps-dimep-1 → Console** e rode:

```bash
python etl_log_acesso.py
```

## 6. Como funciona cada parte

- **historian-api:** uma thread por painel lê o valor atual de cada tag a cada `intervalo_s` e o histórico
  curto a cada 60 s, com NTLM e a conta de serviço (a mesma API dos scripts `historian_*.py`).
- **mes-api:** relê os últimos 7 dias a cada `MES_REFRESH_MIN`. Se o MES falhar, continua servindo o último
  dado bom e mostra o erro na tela.
- **etl-mes:** roda a cada `ETL_INTERVAL_MIN`. A janela começa na última carga menos `ETL_OVERLAP_HOURS`.
  Grava num staging e, numa transação, apaga a janela no destino e insere de novo. Reprocessar a mesma janela não duplica
  linhas e corrige registros editados no MES depois de lidos. A tabela destino é criada na 1ª carga a partir das colunas
  que vierem. Se o MES passar a mandar coluna nova, adicione a coluna no destino (`ALTER TABLE ... ADD COLUMN`).
- **ocr:** é o `laudo-check` com um backend novo, `ollama` (API compatível com OpenAI). O Tesseract e o LLM rodam
  no mini PC, e nada sai da rede. O Ollama descarrega o modelo após 5 min parado para devolver a RAM.
- **mcp-mes:** é o `server.js` original (stdio) atrás do `supergateway`, que o publica como Streamable HTTP em `/mcp`.
  Todas as ferramentas, incluindo as de escrita, ficam disponíveis, então o token dá poder de alterar o MES. Para conectar:
  ```bash
  claude mcp add --transport http mes https://apps-fabrica.<dominio>/mcp --header "Authorization: Bearer <MCP_API_KEY>"
  ```
- **dimep:** é o `scheduler_pausas.py` original. O servidor HTTP dele serve a pasta inteira, então o proxy só deixa
  passar o dashboard e os JSONs que ele usa. O código e os parquets brutos ficam bloqueados.

## 7. Limites com 8 GB de RAM

| Serviço | Limite |
|---|---|
| Umbrel + Portainer | ~1,5 GB (fora do stack) |
| ollama | 3,5 GB (só enquanto valida um laudo) |
| dimep | 1,5 GB |
| ocr | 1 GB |
| etl-mes | 512 MB |
| demais | ≤ 256 MB cada |

O stack cabe, mas sem folga. Com 2 validações de laudo e a validação diária do DIMEP ao mesmo tempo, algo
pode ser morto por falta de memória. **Um pente extra de 8 GB (16 GB no total) é o melhor investimento aqui.**

**Qualidade do OCR com modelo de 3B:** a leitura (Tesseract) é a mesma, mas a decisão sim/não de um modelo de 3B
é bem mais fraca que a do Groq 70B. Rode `laudo-check calibrar` com laudos rotulados **usando este modelo**
(a calibração do Groq não vale aqui) e espere mais casos em revisão humana. Com 16 GB, troque para
`OLLAMA_MODEL=qwen2.5:7b`.

## 8. Rodar fora do Umbrel (teste no notebook)

```bash
cp .env.example .env    # preencha
docker compose up -d --build
```
