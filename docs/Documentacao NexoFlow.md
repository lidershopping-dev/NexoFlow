# NexoFlow 1.0 — Documentação

## Consulta de CPFs válidos e inválidos

Nas cargas Weni e Analytics, o NexoFlow consulta primeiro os CPFs matematicamente válidos e aguarda essa etapa terminar antes de consultar os inválidos preenchidos. Antes de classificar um CPF curto, o NexoFlow testa a reposição de zeros à esquerda até 11 dígitos; aceita essa forma somente quando os dois dígitos verificadores são válidos. A classificação rejeita sequências repetidas e dígitos incorretos. Identificadores que continuam inválidos após essa verificação e valores como `0` ou `00000000000` também são consultados, na segunda etapa. Valores sem dígitos não geram GET. O CPF original permanece no cadastro; a consulta remove a formatação e usa zeros à esquerda somente quando a validação matemática comprova a forma de 11 dígitos. Não troca dígitos existentes.

O Analytics deduplica os identificadores por execução e separa os lotes das duas etapas. As cargas Weni em partes seguem essa prioridade dentro da parte selecionada, preservando o manifesto e a revisão humana. Evidência válida de CPF em mensagens entra na primeira etapa quando for o valor efetivo do plano. Toda nova carga reconsulta os identificadores no escopo selecionado; respostas duplicadas só são compartilhadas dentro da mesma execução. CPFs válidos dispensam comparação de nome, telefone e e-mail. CPFs inválidos exigem nome ou telefone correspondente.

`CPF_CONFIRMADO` identifica um CPF matematicamente válido encontrado na API; `CPF_INVALIDO_ENCONTRADO` identifica um CPF inválido cujo cliente foi confirmado também pelo primeiro nome ou pelo número local do telefone. Ambos alimentam o total de clientes encontrados, mas somente CPFs matematicamente válidos entram em `cpfs_validos`. HTTP 400 registra `CPF_REJEITADO_API`, sem retry e sem contar como falha 500; HTTP 404 registra `CPF_NAO_ENCONTRADO`; falhas de servidor/rede registram `ERRO_API`, sem autorizar um vínculo de cliente por CPF inválido. Os logs públicos mostram etapas e contagens, sem CPF. O diagnóstico privado de falhas 5xx aceita também identificadores curtos e zeros.

Para CPF inválido, a coincidência do CPF não confirma a pessoa. É obrigatório comparar **primeiro nome correspondente ou número local do telefone correspondente**. A comparação de nome usa somente o primeiro nome, desconsiderando acentos, maiúsculas e símbolos; sobrenomes não precisam coincidir. O e-mail não é obrigatório para essa validação. O telefone compara apenas o número local, ignorando o prefixo brasileiro +55 e o DDD, sem completar dígitos nem inserir o nono dígito. Números nativos da Weni têm precedência sobre campos antigos de telefone/WhatsApp.

A resposta pode conter um cliente, uma lista de clientes ou listas em `cliente`/`clientes`. A seleção e a validação acontecem **por contato Weni**, mesmo quando a resposta GET é compartilhada por CPF. Somente uma correspondência é aceita; dados insuficientes geram `CPF_IDENTIDADE_INSUFICIENTE`, nenhuma correspondência gera `CPF_IDENTIDADE_DIVERGENTE` e múltiplas correspondências geram `CPF_IDENTIDADE_AMBIGUA`. Nesses casos, nenhuma atualização de pedido é autorizada. Um pedido de outro candidato nunca é aproveitado; a capa e os itens continuam sujeitos ao mesmo ID do cliente. Se a API devolver apenas um cadastro diferente, o NexoFlow bloqueia o vínculo: não presume que outros cadastros existam nem inventa uma consulta alternativa.

O cache persistente de vínculos por CPF inválido não é reutilizado, incluindo vínculos antigos que usavam somente o CPF. Falha na identificação também não pode ser contornada por `pedido_id` ou `ultimo_pedido`. A revisão humana guarda a identidade usada na validação; alterações relevantes de nome, e-mail ou telefone/URN após a revisão bloqueiam o apply daquele contato. Snapshots antigos de pedidos com CPF inválido exigem novo dry-run.

Pedidos antigos com CPF inválido permanecem preservados nas tabelas de origem, mas não entram nas vendas, na atribuição de anúncios nem em `BI_Pedidos` até serem revalidados pela nova regra. A origem de um novo pedido validado com CPF inválido recebe o marcador técnico `_IDENTIDADE_VALIDADA`, usado para distinguir esses registros. Após reinstalar, execute **carga completa da Weni, com revisão humana, e depois carga completa do Analytics** para revalidar a base e atualizar os indicadores.

Para aproveitar um pedido, o CPF retornado precisa corresponder ao identificador consultado, o cliente deve ter ID positivo e o pedido deve pertencer ao mesmo cliente. CPF numérico no JSON pode perder zeros à esquerda: somente essa representação numérica equivalente é aceita, sem mudar a classificação nem o CPF na Weni. A consulta não comprova emissão oficial do CPF ou titularidade da pessoa; identificadores genéricos como zero exigem atenção na revisão humana. A extração de mensagens e a gravação de um novo CPF na Weni continuam exigindo validade matemática e contexto explícito.

Software interno de integração, atualização de contatos e analytics da FarmaLíder.


### Arquitetura atual de pedidos, fallback e frete
A FarmaLíder API é a fonte de verdade. O fluxo normal usa `CPF -> GET cliente/cpf -> ultimoPedido -> GET pedido/{id}`. Se o GET por CPF não puder ser usado, `pedido_id` da Weni é o primeiro localizador de recuperação; `ultimo_pedido`, alimentado por outra integração, é o último localizador de ID. Nenhum desses campos substitui os detalhes oficiais: produtos, valores, emissão, status e pagamento só são atualizados após validação em `GET pedido/{id}`.

`pedido_produtos` usa exclusivamente `itens[].descricao`; `codProduto` permanece apenas como código técnico. Na Weni, `pedido_valor` continua sendo o total dos produtos. No Google Sheets o NexoFlow mantém `farmalider_frete` separado e calcula `valor_total_faturamento = produtos + frete`. O Looker Studio recebe, em `BI_Pedidos`, `pedido_valor`, `pedido_frete` e `pedido_faturamento_total`, além da separação equivalente em `BI_Vendas` e `BI_Resumo_Diario`.

Os oito campos `pedido_*` do contrato são obrigatórios na Weni; o preflight bloqueia o apply se algum estiver ausente, evitando atualização parcial do snapshot.


### Camada BI para Looker Studio
O NexoFlow 1.0 transforma o NexoFlow Analytics em uma camada analítica pronta para o Looker Studio. O projeto passa a criar/manter automaticamente `BI_Resumo_Diario`, `BI_Canais`, `BI_Conversao`, `BI_Vendas`, `BI_Ads`, `BI_Pedidos` e `BI_Status`. O resumo diário usa uma linha por dia; canais e conversão mantêm snapshots diários; vendas são uma série por data; Ads mantém medidas vazias até existir atribuição; Status registra a saúde da última execução.

Para que os totais do dashboard representem a base Weni completa, `Base_Contatos` passa a preservar todos os contatos retornados pela Weni, mesmo sem CPF, pedido ou anúncio. Também entram no schema analítico `idade`, `maior_de_18_anos` e `opt_in_lgpd`. Após instalar o NexoFlow 1.0, recomenda-se uma única carga completa para preencher contatos históricos antes excluídos e inicializar a camada BI; depois, o incremental mantém tudo atualizado.

### Otimização de carga Analytics + Weni
O NexoFlow 1.0 reduz chamadas externas sem relaxar segurança. O Analytics lê `Base_Contatos` uma única vez por execução e reutiliza o snapshot para comparação, histórico e métricas. Upserts só gravam linhas com mudança real; `sincronizado_em`, `farmalider_cliente_consultado_em`, `capturado_em` e `farmalider_confirmado_em` não forçam escrita quando são a única diferença. Os batches do Sheets passam a ser adaptativos por quantidade e tamanho estimado do payload, com teto conservador de aproximadamente 1,5 MB. No incremental, resultados FarmaLíder recentes podem ser reutilizados quando o CPF permanece igual; a carga completa continua revalidando tudo. No Update Weni, grupos operacionais são resolvidos em uma única listagem, URNs sem UUID usam cache local e a auditoria JSONL reutiliza um handle durante a execução. O snapshot congelado, a revisão humana e a validação de conflito por contato permanecem iguais.

### Schema reutilizado no Google Sheets
Após uma carga real em que `Historico_Alteracoes` sofreu seis `ReadTimeout` consecutivos ao executar `row_values(1)`, o Analytics deixou de fazer essa leitura redundante no caminho crítico. O cabeçalho de `Historico_Alteracoes` e a posição de `contact_uuid` em `Base_Contatos` passam a reutilizar o schema já validado pelo fingerprint no início da execução. Leituras de cabeçalho usadas em diagnóstico/migração são limitadas a uma faixa A1 explícita, evitando solicitar a linha inteira `A1:1`. Timeout/retry e os logs de progresso do NexoFlow 1.0 permanecem ativos.

### Progresso visível no Google Sheets
Durante cargas grandes, o Analytics agora informa claramente em qual etapa está trabalhando. `Base_Contatos` mostra leitura, plano de upsert e progresso a cada 5.000 registros; `Historico_Alteracoes` mostra leitura do cabeçalho, quantidade de lotes e progresso a cada 5.000 linhas. O fluxo principal identifica as etapas 1/4 a 4/4. Quando uma leitura sofre timeout ou erro transitório, o log informa quanto tempo a tentativa aguardou e registra quando o retry se recupera. As escritas também repetem falhas de rede/timeout, além dos HTTP 429 e 5xx já protegidos.

### Resiliência do Google Sheets
O NexoFlow Analytics passa a configurar timeout explícito no cliente gspread (10 s para conexão e 60 s para leitura) e a repetir leituras transitórias com backoff. HTTP 429, erros 5xx e falhas de rede/timeout são tentados novamente antes de abortar. Cabeçalhos, UUIDs, Controle, Base_Contatos, pedidos, métricas e eventos usam a mesma proteção. O preload de UUIDs também registra início, quantidade e duração, evitando períodos silenciosos durante cargas grandes. Erros não transitórios continuam encerrando a execução normalmente.

### Check-up limpo
O `comandos/check-up.bat` passa a executar os testes por `app/run_checkup_tests.py`. Durante a suíte, logs e warnings esperados dos mocks são suprimidos e o `unittest` usa saída enxuta. Falhas reais, erros e tracebacks continuam visíveis e fazem o check-up retornar erro. A alteração afeta somente a apresentação dos testes: warnings e logs das rotinas reais do NexoFlow continuam funcionando normalmente.

### Check-up isolado do estado operacional
O `comandos/check-up.bat` mantém sintaxe, testes e auditoria, mas a suíte do NexoFlow Weni agora isola `_load_state` durante os testes unitários. Assim, `logs\weni_update_state.json` da instalação real — incluindo `pending_conflict_uuids` gerados por cargas completas — não interfere nos mocks nem provoca falsos erros de `contact_uuid duplicado no plano`. O estado real permanece preservado para a próxima execução incremental.

### Terminal amigável e consultas de API
O NexoFlow Terminal passa a expor o comando `nexo`, voltado a operadores que não precisam conhecer Python ou PowerShell. `nexo` abre um menu simples e `nexo ajuda` lista todos os comandos. A CLI inclui status, diagnóstico, Weni, Analytics, logs e consultas GET amigáveis da Weni/FarmaLíder. A família `nexo api` é somente leitura: não oferece POST/PATCH/DELETE e não exibe tokens.

Exemplos: `nexo api weni contato --uuid <UUID>`, `nexo api weni mensagens --uuid <UUID> --limite 10`, `nexo api farmalider cliente --cpf <CPF>` e `nexo api farmalider pedido <CODIGO>`. A referência completa está em `docs/README TERMINAL.md`.

### Atendimento 24h — snapshot congelado

A carga completa não recalcula mais todo o histórico depois da revisão humana. O dry-run gera um snapshot schema 6 com SHA-256 e corte temporal. Após `CARGA COMPLETA REVISADA`, o apply usa exatamente esse conjunto de planos. Novas mensagens e novos contatos continuam entrando normalmente, mas ficam para a próxima incremental. Antes de cada gravação, o contato é relido na Weni; se os dados relevantes mudaram, somente esse contato é ignorado como conflito. O checkpoint final usa o corte do dry-run, evitando perder alterações que ocorreram enquanto a carga estava sendo revisada ou aplicada.

### Google Ads multicanal
Quando a Weni recebe evidência explícita do clique, o NexoFlow pode atribuir o contato a **Google Ads** e manter separado o canal em que a conversa efetivamente ocorreu. São aceitos `gclid`, `gbraid`, `wbraid`, UTM paga do Google e metadados equivalentes quando chegam em campos/metadados estruturados da Weni. Links colados pelo cliente no texto livre não são usados como prova de origem. Performance Max, Search, Display/Banner, YouTube, Demand Gen e Shopping só são classificados quando há evidência suficiente. O primeiro anúncio válido é preservado e o último só muda quando a evidência é mais recente.

O canal de destino é derivado da URN da mensagem (`whatsapp`, `instagram`, `facebook`/Messenger ou `outro`). O anúncio nunca fabrica uma URN. Se a informação de clique for perdida ao abrir diretamente o aplicativo de mensagens, a origem Google Ads permanece desconhecida.

### Identificação multicanal
A Weni é a fonte de verdade das URNs. O NexoFlow agora separa `contact_urn_whatsapp`, `contact_urn_instagram` e `contact_urn_facebook`, mantém `contact_urn` como principal e registra `contact_canal_principal`. A prioridade do principal é WhatsApp, depois Instagram, Facebook/Messenger e, por último, outra URN nativa. Origem de anúncio continua separada e nunca é usada para fabricar uma URN.

Como proteção operacional, os quatro campos multicanal precisam existir previamente na Weni como campos de **Texto**. O teste de conexões consulta a API de fields da Weni e, se algum estiver ausente, o NexoFlow Weni bloqueia a execução antes de qualquer escrita.

### Compatibilidade dos BATs com CMD
Os arquivos `.bat` operacionais são distribuídos sem UTF-8 BOM inicial. Em algumas instalações do Windows, o BOM era exibido pelo `cmd.exe` como `´╗┐` e fazia a primeira diretiva `@echo off` ser tratada como comando inválido. Testes e auditoria bloqueiam a publicação do NexoFlow 1.0 se houver BOM em BATs.


## Versão atual

A versão atual é a **NexoFlow 1.0**.

Ela mantém o **NexoFlow Analytics**, o **NexoFlow Weni**, o fluxo 24h com snapshot congelado, a resolução multicanal de URNs, a atribuição conservadora de Google Ads e a CLI amigável do **NexoFlow Terminal**. O NexoFlow 1.0 adiciona a camada BI/Looker Studio com as otimizações de eficiência consolidadas, mantendo carga incremental, batches adaptativos e as proteções do Update Weni.

## Visão geral

```text
Weni -> NexoFlow Analytics -> FarmaLíder API -> Google Sheets -> Looker Studio
Weni -> NexoFlow Weni -> Weni
```

O Analytics e o NexoFlow Weni possuem responsabilidades separadas. O NexoFlow Weni atualiza dados autorizados diretamente na Weni; o Analytics mantém a camada analítica e a sincronização para Google Sheets/Looker Studio.

## Componentes principais

- `app/main.py`: sincronização analítica;
- `app/weni_client.py`: leitura e escrita controlada na API da Weni;
- `app/contact_enrichment.py`: interpretação conservadora de idade, maioridade e LGPD;
- `app/ad_attribution.py`: detecção conservadora de Google Ads a partir de gclid/gbraid/wbraid, UTM e metadados explícitos;
- `app/urn_utils.py`: resolução determinística de URNs e classificação de WhatsApp/Instagram/Facebook;
- `app/weni_update.py`: planejamento, auditoria, confirmação de pedidos, grupos e write-back;
- `app/run_update_weni.py`: CLI do NexoFlow Weni;
- `app/farmalider_client.py`: acesso à API FarmaLíder;
- `app/farmalider_integration.py`: confirmação/enriquecimento de pedidos;
- `app/google_sheets.py`: camada analítica;
- `app/auditar_consistencia.py`: auditoria técnica obrigatória da publicação;
- `app/run_checkup_tests.py`: executa a suíte do check-up em modo enxuto, suprimindo somente ruído esperado dos testes.

## Executáveis Windows

```text
NexoFlow Analytics.exe
NexoFlow Weni.exe
NexoFlow Recompra.exe
NexoFlow Planilha.exe
NexoFlow Uninstall.exe (opcional)
desinstalar_nexoflow.cmd / installer\uninstall.ps1
NexoFlow Terminal.exe
```

O mutex `Local\NexoFlowOperationLock` é compartilhado entre os launchers para impedir instalação, desinstalação, Analytics e NexoFlow Weni concorrentes.

### Analytics

```text
check-up -> conexões -> sincronização incremental
```

### NexoFlow Weni

```text
check-up -> teste de acesso -> dry-run -> auditoria -> confirmação -> apply
```

O apply reconsulta os dados e executa novamente a auditoria obrigatória antes do primeiro POST.


## NexoFlow Terminal — comandos amigáveis

Ao abrir `NexoFlow Terminal.exe`, a pasta do projeto e a `.venv` entram no `PATH` da sessão e o comando `nexo` fica disponível diretamente.

```text
nexo                         -> menu simples
nexo ajuda                   -> lista de comandos
nexo status                  -> ambiente + conexões
nexo diagnostico             -> check-up + teste de conexões
nexo weni testar             -> teste sem escrita
nexo weni dry-run            -> simulação sem escrita
nexo weni atualizar          -> incremental auditada
nexo weni carga-completa     -> carga completa revisada
nexo analytics               -> Analytics
nexo logs weni --seguir      -> log Weni ao vivo
```

Consultas GET amigáveis:

```text
nexo api weni status
nexo api weni contato --uuid <UUID>
nexo api weni contato --urn <URN>
nexo api weni mensagens --uuid <UUID> --limite 10
nexo api weni grupos
nexo api weni campos
nexo api farmalider status
nexo api farmalider cliente --cpf <CPF>
nexo api farmalider pedido <CODIGO>
```

`--json`/`--raw` mostra a resposta completa e `--salvar arquivo.json` grava o JSON. Tokens permanecem ocultos. Não existe comando genérico de escrita em `nexo api`; mudanças na Weni continuam obrigatoriamente usando o fluxo auditado do NexoFlow Weni.

Consulte `docs/README TERMINAL.md` para explicações, exemplos e referência rápida.

## Campos do NexoFlow Weni

A escrita é limitada a uma allowlist explícita:

```text
contact_urn
contact_urn_whatsapp
contact_urn_instagram
contact_urn_facebook
contact_canal_principal
cpf
idade
maior_de_18_anos
opt_in_lgpd
pedido_id
pedido_data
pedido_produtos
pedido_quantidade_itens
pedido_valor
ultima_compra
primeiro_anuncio_*
ultimo_anuncio_*
```

`opt_in_lgpd_status` continua controlado pelos flows/agentes da Weni. `ultimo_pedido` é preservado apenas para compatibilidade; com a FarmaLíder API disponível, os campos `pedido_*` são alimentados pelo GET oficial por CPF/pedido.

## URNs e canais

A resolução de identidade usa somente URNs retornadas pela Weni. A prioridade da URN principal é:

```text
WhatsApp > Instagram > Facebook/Messenger > outra URN nativa
```

`contact_urn` guarda a principal. Os campos `contact_urn_whatsapp`, `contact_urn_instagram` e `contact_urn_facebook` preservam cada canal separadamente. `contact_canal_principal` registra `whatsapp`, `instagram`, `facebook` ou `outro`. O NexoFlow não transforma origem de Meta Ads ou Google Ads em URN: os campos de anúncio continuam independentes. No NexoFlow 1.0, `primeiro_anuncio_tipo_campanha`, `primeiro_anuncio_canal_destino`, `ultimo_anuncio_tipo_campanha` e `ultimo_anuncio_canal_destino` podem registrar o contexto do Google Ads quando os campos existirem na Weni.

O Analytics expõe esses quatro campos de identificação na base de contatos. IDs de Instagram/Facebook não são colocados na coluna `telefone`; telefone é obtido apenas de URN `tel:` ou WhatsApp.

## LGPD e idade

A rotina não trata qualquer `Sim`, `Não` ou número isolado como dado cadastral. Respostas curtas só são aceitas quando existe contexto explícito imediatamente anterior. Declarações diretas como `Tenho 27 anos` podem preencher idade e maioridade.

O valor de `opt_in_lgpd` é a fonte de verdade para os grupos. Com `ativo`, o contato deve estar em **Opt-In LGPD** e fora de **Opt-Out LGPD**. Com `recusado`, deve estar em **Opt-Out LGPD** e fora de **Opt-In LGPD**. O NexoFlow Weni corrige associações incompatíveis mesmo quando o campo já estava preenchido antes da execução.

## Pedidos e última compra

A rotina usa a FarmaLíder API como fonte de verdade: consulta o cliente por CPF, obtém `ultimoPedido` e `itens`, confirma o mesmo pedido por código e então escreve os campos `pedido_*`. `pedido_produtos` usa `descricao`; quantidades e valores unitários são listas posicionais separadas por vírgula; `pedido_valor` é a soma de `itens[].total`. No Analytics/Looker, todos os estados documentados não cancelados de pedidos validados pela API contam como venda, incluindo Em processamento (0). A mesma regra vale para `ultima_compra` e Recompra, mantendo as demais validações. Cada nova carga consulta os CPFs no escopo selecionado, mesmo sem alteração do contato na Weni. No Analytics, a reconsulta de status dos pedidos já registrados abrange somente os últimos 20 dias; os mais antigos permanecem preservados. Pagamento 0 grava `pedido_forma_pagamento=Cancelado` e força status efetivo 3 na Weni/BI; `Pedidos_FarmaLider` mantém o status e o pagamento brutos da API. Os estados -2, Em aberto, -1, Pendente de pagamento, 0, Em processamento, 4, Pronto para rota, e 1, Em trânsito, permanecem em rechecagem até Concluído ou Cancelado; a rechecagem permanece ativa mesmo quando o pedido já conta como venda no dashboard.

## Auditoria, idempotência e checkpoint

Antes de qualquer escrita, `audit_plans` valida todos os planos. A execução é bloqueada se houver campo não autorizado, vazio inválido, inconsistência entre idade/maioridade, campos de pedido sem confirmação oficial ou ação de grupo LGPD sem o grupo necessário disponível.

Arquivos operacionais:

```text
logs\nexoflow_update_weni.log
logs\weni_update_audit.jsonl
logs\weni_update_state.json
```

Falhas isoladas não cancelam a carga Weni. Os contatos concluídos permanecem aplicados; os demais ficam registrados para revalidação na próxima incremental, recuperando mensagens anteriores quando necessário. Valores iguais não são regravados.

A deduplicação integra a revisão e a aplicação da Weni: identifica o contato correto pelo CPF oficial e telefone nativo único, verifica a propriedade dos pedidos e exclui somente duplicados comprovados. Pedidos reais e recusas são preservados, com limpeza das fontes do Sheets e do Looker. [Regras e retomada da deduplicação](deduplicacao_weni.md).

## BATs operacionais

```text
comandos/testar_update_weni.bat
comandos/atualizar_weni_dry_run.bat
comandos/atualizar_weni.bat
comandos/atualizar_weni_carga_completa.bat
```

Na primeira implantação/backfill, use `comandos/atualizar_weni_carga_completa.bat`. No uso rotineiro, use o atalho **NexoFlow Weni** antes do **NexoFlow Analytics**.


### Correção do ParserError do Setup
O `installer\install.ps1` passa a delimitar `${exePath}` quando o texto imediatamente seguinte contém `:`. Isso evita que o PowerShell interprete `$exePath:` como uma referência de variável com escopo/drive e bloqueie o script durante o parsing. O Setup mantém log persistente e janela aberta quando ocorre uma falha.

### Confiabilidade do Setup
O `NexoFlow Setup.exe` não encerra mais silenciosamente quando a etapa PowerShell retorna erro: a janela permanece aberta, o código de saída é mostrado e a execução também é registrada em `%TEMP%\NexoFlow-Setup.log`. Em atualizações, o instalador encerra somente processos executados a partir de `C:\NexoFlow` antes de substituir a pasta e utiliza retry para aguardar a liberação de arquivos pelo Windows.

### Nomes e ícone do Terminal
A interface Windows usa os nomes **NexoFlow Weni** e **NexoFlow Terminal**. O Terminal é distribuído como `NexoFlow Terminal.exe`, inicia diretamente `app/run_terminal.py` com o Python da `.venv` quando disponível e incorpora `assets\NexoFlow_Terminal.ico`. Os atalhos de Área de Trabalho e Menu Iniciar usam o mesmo ícone. Durante atualização, o Setup remove atalhos com os nomes anteriores e solicita ao Explorer a releitura dos ícones.

### Hardening de distribuição e NexoFlow Terminal
Os launchers Windows deixam de usar `system()` e iniciam processos filhos por API nativa do Windows através de `CreateProcessW`. Os PE também passam a conter informações de versão/produto. O `NexoFlow Uninstall.exe` é distribuído como launcher nativo opcional, enquanto o registro de desinstalação do Windows continua apontando diretamente para `installer\uninstall.ps1`. Assim, a remoção segue funcional mesmo se um antivírus/EDR bloquear o EXE opcional. O Stage2 não é ocultado e o desinstalador restringe o encerramento de processos externos a Python/PythonW que referenciem explicitamente arquivos do NexoFlow.

`NexoFlow Terminal.exe` abre uma PowerShell em `C:\NexoFlow` com a `.venv` priorizada. A mesma lógica de `app/proxy_config.py` é executada antes do terminal e a configuração de proxy é herdada apenas por aquela sessão. A senha não é exibida. Tokens Weni/FarmaLíder e credenciais Google continuam nos arquivos locais e não são exportados para o ambiente.

### Atualização do ícone no Explorer
O executável `NexoFlow Weni.exe` incorpora o ícone dedicado `assets\NexoFlow_Weni.ico`. Durante uma atualização, o instalador também atualiza o timestamp do executável, notifica o Shell com `SHChangeNotify` e solicita a recarga do cache de ícones do Windows. Isso evita que o Explorer continue exibindo o ícone antigo do Analytics para o mesmo caminho.

### Sincronização dos grupos LGPD
O campo `@fields.opt_in_lgpd` controla de forma determinística os grupos de consentimento:

```text
ativo    -> adicionar Opt-In LGPD; remover Opt-Out LGPD
recusado -> adicionar Opt-Out LGPD; remover Opt-In LGPD
```

A regra também funciona como reconciliação: se um contato já estiver em grupo incompatível com o campo, o NexoFlow Weni cria as ações necessárias mesmo sem uma nova mensagem de LGPD. Quando há troca, o grupo correto é adicionado antes da remoção do incompatível. O teste de acesso e a auditoria exigem a disponibilidade dos grupos necessários antes de qualquer escrita.

### Captura de CPF pelas mensagens
O NexoFlow Weni pode preencher ou corrigir `@fields.cpf` a partir de mensagens recebidas do próprio cliente. O documento é normalizado para 11 dígitos e validado pelos dígitos verificadores antes de entrar no plano.

São aceitos dois cenários seguros:

```text
Meu CPF é 529.982.247-25

ou

NexoFlow/flow: Informe seu CPF
Cliente: 52998224725
```

Se o campo estiver vazio, ele pode ser preenchido. Se existir um CPF diferente e o cliente informar posteriormente outro CPF válido, prevalece a evidência válida mais recente. Um mesmo CPF com pontuação diferente não gera regravação. Mensagens ambíguas, CPF inválido ou um número isolado sem contexto de CPF são ignorados. A evidência (ID/data/motivo) aparece na auditoria e no relatório da carga completa.

### Revisão humana obrigatória da carga completa
O dry-run completo gera dois artefatos locais:

```text
logs\weni_update_review_full.txt
logs\weni_update_review_full.json
```

O TXT é aberto automaticamente e apresenta resumo e detalhes por contato: UUID/URN, valor anterior, valor proposto, evidência técnica, ações nos grupos Opt-In/Opt-Out LGPD e confirmação de pedido. O operador só deve prosseguir após revisar o arquivo e digitar `CARGA COMPLETA REVISADA`.

O JSON é um snapshot verificável e congelado (schema 6). O SHA-256 protege a integridade do plano revisado e o arquivo registra o corte temporal do dry-run. No apply, o NexoFlow não recalcula todo o histórico. Ele carrega exatamente os planos revisados e relê cada contato imediatamente antes da gravação. Se os valores relevantes daquele contato mudaram, somente ele é marcado como `conflict_skipped`; se o resultado já foi aplicado por outra automação, fica como `already_satisfied`. Os demais contatos continuam. Ao final, o checkpoint usa o corte do dry-run, garantindo que mudanças posteriores sejam elegíveis para a próxima incremental.

## Instalação

`NexoFlow Setup.exe` instala em `C:\NexoFlow`, valida o payload por SHA-256, aceita Python 3.11+ x64, oferece o Python 3.14.8 x64 incluído quando necessário, cria `.venv`, preserva dados privados de uma instalação anterior e cria os atalhos **NexoFlow Analytics**, **NexoFlow Weni**, **NexoFlow Recompra**, **NexoFlow Planilha** e **NexoFlow Terminal** na Área de Trabalho e no Menu Iniciar.

Consulte `docs/README INSTALACAO.md` para o procedimento detalhado.

## Desinstalação resiliente
O `NexoFlow Uninstall.exe`, quando disponível, apenas inicia o mesmo fluxo de `installer\uninstall.ps1`. O `desinstalar_nexoflow.cmd / installer\uninstall.ps1` permanece o mecanismo independente de remoção de Analytics, NexoFlow Weni, NexoFlow Terminal, BATs, módulos, atalhos, registro do aplicativo e demais componentes instalados.

A presença do `NexoFlow Uninstall.exe` **não é requisito do Setup nem do check-up**. O Setup tenta copiá-lo e apenas avisa quando um antivírus/EDR corporativo bloqueia ou remove o arquivo.

Para impedir que o próprio desinstalador mantenha `C:\NexoFlow` bloqueado, a remoção final funciona em duas etapas:

1. o primeiro estágio coleta a opção de preservação e prepara a remoção;
2. o Stage2 é copiado para `%TEMP%`;
3. o Stage2 é iniciado com `WorkingDirectory=%TEMP%` e executa `Set-Location` para `%TEMP%`;
4. processos relacionados ao NexoFlow que ainda usem arquivos da instalação são encerrados de forma controlada;
5. atributos `ReadOnly`, `System` e `Hidden` são limpos quando necessário;
6. a exclusão de `C:\NexoFlow` é repetida por até **60 ciclos** enquanto o Windows libera handles;
7. atalhos e chaves de registro do NexoFlow são removidos.

O registro de desinstalação do Windows não usa mais `cd /d C:\NexoFlow`, evitando que a própria pasta instalada permaneça como diretório de trabalho do processo pai.

Se o usuário escolher preservar dados, os seguintes itens podem ser mantidos:

```text
secrets_config.py
credentials\
logs\
```

Eles são armazenados em:

```text
C:\NexoFlow Data
```

O desinstalador **não remove o Python global do Windows**. Ele remove somente componentes pertencentes ao NexoFlow.

## Segurança

Nunca distribuir credenciais reais:

```text
secrets_config.py
credentials\google_credentials.json
tokens
senhas do proxy
```

Os pacotes oficiais não devem conter `__pycache__`, `.pyc` ou credenciais reais.

## Validação do NexoFlow 1.0

Toda mudança relevante deve executar:

```text
1. compilação/sintaxe
2. suíte de testes automatizados
3. app/auditar_consistencia.py
4. auditoria dos executáveis/launchers
5. validação do payload manifest
6. validação dos checksums externos do Setup
7. teste de integridade dos ZIPs finais
8. verificação de ausência de caches e credenciais reais
```

Consulte também `README.md`, `docs/README INSTALACAO.md`, `docs/README WENI.md` e `CHANGELOG.md`.

## Recompra FarmaLíder D+25

`NexoFlow Recompra.exe` inicia `comandos/atualizar_recompra.bat`, com diagnóstico, dry-run, revisão humana e aplicação. Grupo sincronizado com a última compra confirmada entre 0 e 25 dias, remoção depois de 25 dias e por recusa. O envio continua somente no D+25 pela campanha Weni. Execute `nexo weni recompra auditoria`, `dry-run` e `aplicar` com revisão, ou `nexo weni recompra atualizar`. Confira [implementação, configuração e limites da recompra](README%20RECOMPRA.md). A rotina não altera marketing_opt_in, não dispara WhatsApp e não modifica a campanha.

## NexoFlow Planilha

`NexoFlow Planilha.exe` abre o menu das cargas por planilha para Weni e Analytics e da pasta `importacoes`. Use `Conversas_Delivery.xlsx` e `Contatos_Delivery.xlsx`; cada destino exige revisão humana antes da aplicação. Consulte [Delivery e carga por planilhas](README%20DELIVERY.md).


### Dados comerciais e histórico observado

O Analytics preserva os pedidos e itens já identificados no Google Sheets; cada chave de pedido é atualizada sem duplicar a compra nem apagar os pedidos anteriores. A API fornece apenas o último pedido por cliente. Coletas regulares acumulam histórico observado, mas não recuperam compras antigas nunca capturadas nem garantem que todas as compras entre duas coletas tenham sido registradas.

Cada carga do Analytics também reconsulta por código os pedidos oficiais já observados dos contatos selecionados com até 20 dias, pela data do pedido em America/Belem (dias 0 a 20, inclusive), incluindo concluídos e cancelados. Os mais antigos permanecem no histórico e nos indicadores sem reconsulta automática de status. A consulta por CPF continua identificando novas compras; pedidos ainda não registrados exigem validação inicial, inclusive antigos. O vínculo com o cliente precisa ter sido comprovado; registros legados sem essa comprovação continuam pendentes. Falhas preservam o snapshot anterior e deixam a carga parcial, sem avançar o checkpoint.

As fontes comerciais `BI_Publico`, `BI_Clientes`, `BI_Clientes_Vendas`, `BI_Itens`, `BI_Produtos_ABC`, `BI_Produtos_Juntos`, `BI_KPIs_Periodo` e `BI_Qualidade` agrupam pessoas por chave estável e fornecem medidas numéricas, datas, rankings de produtos e combinações por pedido. As fontes novas não expõem CPF, telefone, e-mail, número original de pedido, status ou pagamento. Gênero e categoria ficam vazios quando ausentes; não são deduzidos.

`BI_Clientes_Vendas` e `BI_Itens` aceitam filtros livres por data no Looker. ABC, combinações e KPIs consolidados usam um período explícito de 30 dias por padrão; outro período pode ser recalculado com `nexo looker --inicio AAAA-MM-DD --fim AAAA-MM-DD`, usando apenas o histórico do Sheets. Recompra completa e comparações ficam indisponíveis enquanto não houver cobertura comprovada. Confira as [fontes, fórmulas e orientações do Looker](../README.md#dados-comerciais-para-o-looker-studio).

As tabelas comerciais e as tabelas BI anteriores preservam números, datas e identificadores como texto literal. A migração financeira aplica valores e formatos em uma única transação. A Curva ABC exige cobertura dos itens do período e sinaliza ausência ou divergência em `BI_Qualidade`; a rotina Delivery também atualiza as fontes comerciais.
