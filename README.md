# NexoFlow 1.0

Os executáveis permanecem na raiz para acesso direto. Os demais arquivos estão organizados em:

- `app/`: módulos Python do NexoFlow.
- `comandos/`: rotinas BAT, incluindo cargas completas e o fluxo `atualizar_tudo.bat`.
- `docs/`: manuais e documentação.
- `configuracao/`: exemplos para configurar suas próprias credenciais.
- `installer/`, `scripts/` e `tests/`: instalação, manutenção e testes.

`instalar.bat`, `nexo.cmd` e `desinstalar_nexoflow.cmd` continuam na raiz. A atualização pelo Setup substitui a estrutura antiga e preserva `secrets_config.py`, `credentials/`, `logs/` e `importacoes/`.

**Carga por exports do Delivery:** coloque `Conversas_Delivery.xlsx` e `Contatos_Delivery.xlsx` em `C:\NexoFlow\importacoes` e execute `nexo weni planilhas` ou `nexo analytics planilhas`. Os destinos são separados e exigem revisão antes da aplicação. Consulte [Delivery e carga por planilhas](docs/README%20DELIVERY.md).

## Arquivos Google Sheets separados

O NexoFlow pode manter operação, histórico de alterações e dashboard em três arquivos. As abas `BI_*` permanecem na planilha original, preservando os IDs das fontes do Looker. Execute `comandos/dividir_planilhas.bat` com todas as rotinas encerradas e os dois destinos compartilhados como Editor com sua conta de serviço. A cópia é conferida antes da ativação e da retirada das abas originais. Consulte [Divisão dos arquivos](docs/planilhas_separadas.md).

## Padronização do endereço

Weni, Analytics e as fontes do Looker padronizam cidades, estados por extenso e bairros. Grafias como `Belem` e `Belenm` recebem **Belém**; `PA` recebe **Pará**. Conjuntos e subdivisões permanecem identificados, como **Cidade Nova VI**. As alterações de endereço da Weni passam pela revisão e pela conferência dos campos atuais antes da aplicação.

As [referências e regras de endereço](docs/geografia.md) acompanham os pacotes. Casos sem referência confiável permanecem para conferência do cadastro.

## Consulta de CPFs válidos e inválidos

Nas cargas Weni e Analytics, o NexoFlow consulta primeiro os CPFs matematicamente válidos e aguarda essa etapa terminar antes de consultar os inválidos preenchidos. Antes de classificar um CPF curto, o NexoFlow testa a reposição de zeros à esquerda até 11 dígitos; aceita essa forma somente quando os dois dígitos verificadores são válidos. A classificação rejeita sequências repetidas e dígitos incorretos. Identificadores que continuam inválidos após essa verificação e valores como `0` ou `00000000000` também são consultados, na segunda etapa. Valores sem dígitos não geram GET. O CPF original permanece no cadastro; a consulta remove a formatação e usa zeros à esquerda somente quando a validação matemática comprova a forma de 11 dígitos. Não troca dígitos existentes.

O Analytics deduplica os identificadores por execução e separa os lotes das duas etapas. As cargas Weni em partes seguem essa prioridade dentro da parte selecionada, preservando o manifesto e a revisão humana. Evidência válida de CPF em mensagens entra na primeira etapa quando for o valor efetivo do plano. Toda nova carga reconsulta os identificadores no escopo selecionado; respostas duplicadas só são compartilhadas dentro da mesma execução. CPFs válidos dispensam comparação de nome, telefone e e-mail. CPFs inválidos exigem nome ou telefone correspondente.

`CPF_CONFIRMADO` identifica um CPF matematicamente válido encontrado na API; `CPF_INVALIDO_ENCONTRADO` identifica um CPF inválido cujo cliente foi confirmado também pelo primeiro nome ou pelo número local do telefone. Ambos alimentam o total de clientes encontrados, mas somente CPFs matematicamente válidos entram em `cpfs_validos`. HTTP 400 registra `CPF_REJEITADO_API`, sem retry e sem contar como falha 500; HTTP 404 registra `CPF_NAO_ENCONTRADO`; falhas de servidor/rede registram `ERRO_API`, sem autorizar um vínculo de cliente por CPF inválido. Os logs públicos mostram etapas e contagens, sem CPF. O diagnóstico privado de falhas 5xx aceita também identificadores curtos e zeros.

Para CPF inválido, a coincidência do CPF não confirma a pessoa. É obrigatório comparar **primeiro nome correspondente ou número local do telefone correspondente**. A comparação de nome usa somente o primeiro nome, desconsiderando acentos, maiúsculas e símbolos; sobrenomes não precisam coincidir. O e-mail não é obrigatório para essa validação. O telefone compara apenas o número local, ignorando o prefixo brasileiro +55 e o DDD, sem completar dígitos nem inserir o nono dígito. Números nativos da Weni têm precedência sobre campos antigos de telefone/WhatsApp.

A resposta pode conter um cliente, uma lista de clientes ou listas em `cliente`/`clientes`. A seleção e a validação acontecem **por contato Weni**, mesmo quando a resposta GET é compartilhada por CPF. Somente uma correspondência é aceita; dados insuficientes geram `CPF_IDENTIDADE_INSUFICIENTE`, nenhuma correspondência gera `CPF_IDENTIDADE_DIVERGENTE` e múltiplas correspondências geram `CPF_IDENTIDADE_AMBIGUA`. Nesses casos, nenhuma atualização de pedido é autorizada. Um pedido de outro candidato nunca é aproveitado; a capa e os itens continuam sujeitos ao mesmo ID do cliente. Se a API devolver apenas um cadastro diferente, o NexoFlow bloqueia o vínculo: não presume que outros cadastros existam nem inventa uma consulta alternativa.

O cache persistente de vínculos por CPF inválido não é reutilizado, incluindo vínculos antigos que usavam somente o CPF. Falha na identificação também não pode ser contornada por `pedido_id` ou `ultimo_pedido`. A revisão humana guarda a identidade usada na validação; alterações relevantes de nome, e-mail ou telefone/URN após a revisão bloqueiam o apply daquele contato. Snapshots antigos de pedidos com CPF inválido exigem novo dry-run.

Pedidos antigos com CPF inválido permanecem preservados nas tabelas de origem, mas não entram nas vendas, na atribuição de anúncios nem em `BI_Pedidos` até serem revalidados pela nova regra. A origem de um novo pedido validado com CPF inválido recebe o marcador técnico `_IDENTIDADE_VALIDADA`, usado para distinguir esses registros. Após reinstalar, execute **carga completa da Weni, com revisão humana, e depois carga completa do Analytics** para revalidar a base e atualizar os indicadores.

Para aproveitar um pedido, o CPF retornado precisa corresponder ao identificador consultado, o cliente deve ter ID positivo e o pedido deve pertencer ao mesmo cliente. CPF numérico no JSON pode perder zeros à esquerda: somente essa representação numérica equivalente é aceita, sem mudar a classificação nem o CPF na Weni. A consulta não comprova emissão oficial do CPF ou titularidade da pessoa; identificadores genéricos como zero exigem atenção na revisão humana. A extração de mensagens e a gravação de um novo CPF na Weni continuam exigindo validade matemática e contexto explícito.

O **NexoFlow 1.0** é a camada de integração, atualização de dados e analytics que conecta **Weni**, **FarmaLíder API**, **Google Sheets** e **Looker Studio** em um fluxo único, auditável e preparado para operação contínua.

Ele possui duas responsabilidades principais:

```text
Weni -> NexoFlow Weni -> Weni
Weni -> NexoFlow Analytics -> FarmaLíder API -> Google Sheets -> Looker Studio
```

O **NexoFlow Weni** enriquece e atualiza contatos com regras controladas.  
O **NexoFlow Analytics** organiza os dados operacionais, pedidos, atribuição de anúncios e indicadores usados no Google Sheets e no Looker Studio.

Ao terminar uma carga Weni, a janela permanece aberta para conferir o resultado. Fechar o aviso final não fecha o CMD; digite `EXIT` quando quiser encerrar a sessão.

## Downloads do NexoFlow 1.0

- [Setup instalável](https://github.com/felipetelesbarbosa/NexoFlow/releases/download/v1.0/NexoFlow-Setup-1.0.zip)
- [Projeto completo](https://github.com/felipetelesbarbosa/NexoFlow/releases/download/v1.0/NexoFlow-1.0.zip)
- [Checksums SHA-256](https://github.com/felipetelesbarbosa/NexoFlow/releases/download/v1.0/SHA256SUMS-1.0.txt)

Os pacotes são atualizados no [release NexoFlow 1.0](https://github.com/felipetelesbarbosa/NexoFlow/releases/tag/v1.0) após testes, auditoria e validação do Setup no Windows. A versão permanece **1.0**.

O NexoFlow não inclui credenciais. Configure suas próprias chaves e tokens conforme o [aviso de segurança](SECURITY.md).

## Idioma e codificação

A interface e a documentação do NexoFlow usam português brasileiro, com acentos e cedilha. Os scripts de CMD selecionam UTF-8; os scripts PowerShell usam UTF-8 com BOM para compatibilidade com Windows PowerShell 5.1. Os executáveis são recompilados com metadados pt-BR na geração dos pacotes. Nomes técnicos de arquivos, comandos, campos e endpoints preservam o contrato de integração. Botões e mensagens do Windows ou do instalador Python seguem o idioma desses componentes.

## Integrações do NexoFlow 1.0

| Integração | Uso no NexoFlow |
|---|---|
| **Weni** | Contatos, mensagens, URNs, campos personalizados, grupos LGPD e atualização auditada de contatos. |
| **FarmaLíder API** | Validação de cliente por CPF, confirmação de pedido por ID, status, itens, valores, frete e última compra. |
| **Google Sheets** | Base operacional, histórico, pedidos, itens, métricas e camada BI. |
| **Looker Studio** | Visualização de Marketing, Vendas, conversão, canais, pedidos, compradores, Ads e saúde da integração. |
| **Google Ads** | Atribuição quando a Weni recebe evidência explícita como `gclid`, `gbraid`, `wbraid`, UTM paga ou metadado equivalente. |
| **Meta Ads / referrals Weni** | Campos de primeiro/último anúncio e estrutura para eventos de anúncios. O webhook opcional está preparado; integração direta com Meta Marketing API não é tratada como ativa. |
| **Windows / PowerShell / Python** | Setup, executáveis, ambiente virtual, diagnóstico, operação e desinstalação. |
| **Proxy corporativo** | Detecção automática de proxy e seleção da rota correta para a FarmaLíder API. |

## Componentes

### NexoFlow Weni

Atualiza contatos diretamente pela API da Weni sem depender de exportação/importação manual de planilhas.

Fluxo protegido:

```text
check-up
-> teste de acesso e conexões
-> dry-run
-> auditoria
-> confirmação humana
-> apply
```

Na carga completa, o plano é congelado em JSON, protegido por SHA-256 e revisado antes da aplicação. Cada contato é relido imediatamente antes da gravação para evitar sobrescrever alterações ocorridas durante a revisão.

Os logs de preparação e auditoria aparecem antes das gravações. O total de contatos com pedido validado pela API inclui os que já estavam atualizados; ele é diferente do total de contatos com alterações.

### NexoFlow Analytics

Executa a sincronização analítica e mantém as estruturas usadas por Google Sheets e Looker Studio.

Responsabilidades principais:

- preservar os contatos retornados pela Weni;
- normalizar CPF, identidade e canais;
- enriquecer clientes pela FarmaLíder API;
- resolver e confirmar pedidos;
- reconciliar itens;
- manter métricas de vendas;
- consolidar atribuição de anúncios;
- manter as abas BI prontas para o Looker Studio;
- registrar telemetria da execução.

### NexoFlow Terminal

Interface de linha de comando para operação e diagnóstico:

```text
nexo
nexo ajuda
nexo status
nexo diagnostico
nexo versao

nexo weni testar
nexo weni dry-run
nexo weni atualizar
nexo weni carga-completa

nexo analytics

nexo api weni status
nexo api weni contato --uuid <UUID>
nexo api weni contato --urn <URN>
nexo api weni mensagens --uuid <UUID> --limite 10
nexo api weni grupos
nexo api weni campos

nexo api farmalider status
nexo api farmalider cliente --cpf <CPF>
nexo api farmalider pedido <CODIGO>

nexo logs weni
nexo logs weni --seguir
nexo logs analytics
```

A família `nexo api` é **somente leitura**. Atualizações da Weni continuam obrigatoriamente usando o fluxo auditado do NexoFlow Weni.

## Identidade multicanal

A Weni é a fonte de verdade para URNs. O NexoFlow não fabrica identificadores de canal.

Prioridade da URN principal:

```text
WhatsApp > Instagram > Facebook/Messenger > outra URN nativa
```

Campos mantidos:

```text
contact_urn
contact_urn_whatsapp
contact_urn_instagram
contact_urn_facebook
contact_canal_principal
```

`contact_canal_principal` pode receber:

```text
whatsapp
instagram
facebook
outro
```

Origem de anúncio e canal de conversa permanecem separados. Um anúncio pode ter origem Google Ads ou Meta e terminar em WhatsApp, Instagram ou Messenger sem alterar a identidade real do contato.

## Campos que o NexoFlow Weni pode escrever

A escrita usa uma allowlist fechada:

### Identidade e cadastro

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
```

### Pedido e compra

```text
pedido_id
pedido_data
pedido_status
pedido_forma_pagamento
pedido_produtos
pedido_quantidade_itens
pedido_valor_produtos
pedido_valor
ultima_compra
```

### Primeiro anúncio

```text
primeiro_anuncio_origem
primeiro_anuncio_id
primeiro_anuncio_titulo
primeiro_anuncio_url
primeiro_anuncio_clique_id
primeiro_anuncio_data
primeiro_anuncio_tipo_campanha
primeiro_anuncio_canal_destino
```

### Último anúncio

```text
ultimo_anuncio_origem
ultimo_anuncio_id
ultimo_anuncio_titulo
ultimo_anuncio_url
ultimo_anuncio_clique_id
ultimo_anuncio_data
ultimo_anuncio_tipo_campanha
ultimo_anuncio_canal_destino
```

Qualquer tentativa de escrever fora da allowlist bloqueia a execução antes do primeiro POST.

### Campos preservados

`ultimo_pedido` continua preservado por compatibilidade, mas não é mais a fonte prioritária do pedido quando a FarmaLíder API está disponível.

`opt_in_lgpd_status` permanece sob controle dos flows/agentes da Weni.

## CPF, idade e LGPD

O NexoFlow usa interpretação conservadora:

- CPF extraído de mensagens para atualizar o cadastro precisa ser válido pelos dígitos verificadores;
- CPF em mensagem livre precisa estar explicitamente identificado ou responder a uma solicitação contextual;
- mensagens ambíguas não atualizam cadastro;
- idade explícita pode preencher `idade` e `maior_de_18_anos`;
- respostas como “Sim”, “Não” ou um número isolado só são usadas quando existe contexto claro;
- `opt_in_lgpd = ativo` sincroniza o contato com **Opt-In LGPD**;
- `opt_in_lgpd = recusado` sincroniza o contato com **Opt-Out LGPD**;
- os dois grupos são mutuamente exclusivos.

## Pedidos e FarmaLíder API

A preparação Weni resume a checagem de CPFs inválidos em lotes de até **500 contatos**, incluindo o último lote parcial e um total final. O resumo mostra pedidos encontrados e validados, divergências, identidade ambígua, dados insuficientes, cliente não encontrado e outros impedimentos. O total também aparece no relatório da revisão humana e no resumo do terminal. Os avisos individuais de identidade ficam no nível DEBUG.

Esses lotes organizam o acompanhamento: o endpoint continua recebendo uma consulta por CPF. As contagens são por contato Weni, incluem pedidos cancelados e contatos sem alterações e não representam vendas, pedidos únicos ou gravações. A validação pelo primeiro nome ou pelo número local do telefone continua obrigatória para CPFs inválidos, sem exigir e-mail.

A **FarmaLíder API é a fonte de verdade dos pedidos**. Os campos de pedido da Weni funcionam como destino/cache operacional e são atualizados pela rotina auditada.

Fluxo principal:

```text
CPF
-> GET /api/Delivery/cliente/cpf/{cpf}
-> valida cliente e obtém ultimoPedido + itens
-> GET /api/Delivery/pedido/{pedido_id}
-> confirma o snapshot mais atual do mesmo pedido
-> atualiza Weni
-> alimenta Google Sheets / Looker Studio
```

O retorno de `cliente/cpf` já contém `ultimoPedido` e `itens`, portanto pode servir como fallback completo quando a segunda consulta sofre falha transitória. O endpoint de pedido é preferido como snapshot final.

Se o GET por CPF não puder ser usado, o NexoFlow tenta primeiro o `pedido_id` já gravado pelo próprio NexoFlow. Somente se esse localizador não estiver disponível ou não puder ser validado, `ultimo_pedido` é usado como **último localizador de ID**. Como `ultimo_pedido` é alimentado por outra integração, ele ajuda a recuperar um pedido criado enquanto o NexoFlow esteve indisponível. Mesmo nesse fallback, os produtos, valores, status, data e pagamento só são aceitos após um GET oficial de `pedido/{id}`; `ultimo_pedido` não substitui a FarmaLíder API como fonte de verdade.

Os oito campos `pedido_*` abaixo são obrigatórios na Weni. Se qualquer um estiver ausente, o NexoFlow Weni bloqueia o apply para evitar um snapshot parcial de pedido.

### Contrato dos campos Weni

| Campo Weni | Origem FarmaLíder | Formato |
|---|---|---|
| `pedido_id` | `ultimoPedido.id` / `capa.id` | código do pedido |
| `pedido_produtos` | `itens[].descricao` | nomes em lista CSV; aspas preservam vírgulas no nome |
| `pedido_quantidade_itens` | `itens[].quantidade` | quantidades na mesma ordem, separadas por vírgula |
| `pedido_valor_produtos` | `itens[].valor` | valores unitários na mesma ordem, separados por vírgula |
| `pedido_valor` | soma de `itens[].total` | valor total dos itens do pedido |
| `pedido_data` | `emissao` | `DD/MM/YY HH:MM:SS` |
| `pedido_status` | `status` e regra de cancelamento | status efetivo de -2 a 4 |
| `pedido_forma_pagamento` | `formaPagamento` | código Int32 não negativo normalizado; 0 grava `Cancelado` |

`codProduto` não é usado como nome do produto. O nome exibido vem exclusivamente de `descricao`. O código do produto pode continuar armazenado internamente para auditoria, sem alimentar `pedido_produtos`.

Nomes como `Produto 0,5 MG` são escritos entre aspas na lista: `"Produto 0,5 MG", Produto B`. A Weni e o Analytics interpretam isso como dois produtos, preservando a ordem das quantidades e dos valores. A auditoria informa as contagens e continua bloqueando desalinhamentos reais, inclusive em alterações parciais.

Exemplo de um pedido com dois itens:

```text
pedido_id: 340793
pedido_produtos: PROLOPA BD 100+25MG C/30CAP, TP PRAMIPEXOL PRATI 1MG C/30 CPR (G)
pedido_quantidade_itens: 1, 1
pedido_valor_produtos: 53.9, 58.9
pedido_valor: 112.8
pedido_data: 05/06/26 15:45:27
pedido_status: 3
pedido_forma_pagamento: 2
```

As três listas de item são posicionais: o primeiro produto corresponde à primeira quantidade e ao primeiro valor, o segundo produto corresponde à segunda quantidade e ao segundo valor, e assim por diante.

### Status do pedido

| Código | Status |
|---:|---|
| -2 | Em aberto |
| -1 | Pendente de pagamento |
| 0 | Em processamento |
| 4 | Pronto para rota |
| 1 | Em trânsito |
| 2 | Concluído |
| 3 | Cancelado |

### Formas de pagamento

| Código | Forma de pagamento |
|---:|---|
| 0 | Cancelado |
| 1 | Dinheiro |
| 2 | Débito |
| 3 | Crédito |
| 4 | Ticket |
| 5 | Pix por Link de Pagamento |
| 6 | Pix no POS |

A Weni guarda os códigos de pagamento normalizados, exceto `formaPagamento=0` ou `_`, que grava o texto `Cancelado` em `pedido_forma_pagamento` e o status efetivo `3`, Cancelado. O Analytics cria os rótulos amigáveis para o Google Sheets e o Looker Studio. `Pedidos_FarmaLider` preserva os valores brutos de pagamento e status da API; Weni e `BI_Pedidos` usam o status efetivo, inclusive o cancelamento por pagamento 0.

Os normalizadores aceitam códigos e nomes dos enums oficiais, como `PendenteDePagamento`, `EmProcessamento`, `Concluido`, `PixPorLinkDePagamento` e `PixNoPOS`. O nome antigo `PendentePagamento` permanece aceito como alias de `PendenteDePagamento` para leitura de registros existentes. O nome de pagamento `_` corresponde ao código 0 e aplica a mesma regra de cancelamento.

Códigos numéricos de pagamento válidos sem de-para a partir de 7 continuam sendo preservados como Int32 na Weni e exibidos no Google Sheets/BI como `Não identificado (código X)`, sem impedir a atualização dos demais campos válidos. Pagamento ausente ou inválido mantém a descrição vazia.

Cada código sem de-para aparece em `INFO` uma vez por execução, seguido dos totais por código; esse registro informa a falta de descrição documentada e não indica falha na atualização.

### Frete e faturamento

`pedido_valor` continua representando **somente os produtos**, conforme o contrato da Weni. O campo `frete` retornado pela FarmaLíder é armazenado no Google Sheets em `farmalider_frete` e o Analytics calcula também `valor_total_faturamento = produtos + frete`.

No BI, a separação fica explícita:

```text
pedido_valor            = total dos produtos
pedido_frete            = frete
pedido_faturamento_total = produtos + frete
```

As abas `BI_Resumo_Diario` e `BI_Vendas` também separam faturamento de produtos, faturamento de frete e faturamento total. O campo de faturamento usado nos indicadores gerais passa a considerar **produtos + frete**, para bater com o valor total cobrado do pedido.

Em todo o NexoFlow, pedidos validados pela API em qualquer estado documentado não cancelado entram nas métricas de venda, incluindo **Em processamento (0)**. Cancelamento explícito, status 3 ou pagamento 0 excluem a venda; status desconhecido permanece pendente. A mesma regra atualiza `ultima_compra` e a validação de venda da Recompra, preservando identidade, itens, janela D+25 e bloqueios de marketing. No Analytics, a reconsulta de status dos pedidos já registrados fica limitada aos últimos 20 dias, pela data do pedido no fuso de Belém, inclusive concluídos e cancelados nesse intervalo. A consulta por CPF continua identificando compras novas.

Pedidos abertos em **Em aberto (-2)**, **Pendente de pagamento (-1)**, **Em processamento (0)**, **Pronto para rota (4)** ou **Em trânsito (1)** permanecem em fila de rechecagem até chegarem a **Concluído** ou **Cancelado**.

Datas operacionais de pedidos usam **America/Belem (UTC-3)**.

## Google Ads

A atribuição é propositalmente conservadora.

O NexoFlow aceita evidência explícita em dados estruturados recebidos pela Weni, como:

```text
gclid
gbraid
wbraid
utm_source=google + contexto pago
metadado explícito Google Ads / AdWords
```

Tipos reconhecidos quando a evidência permite:

```text
Performance Max
Search
Display/Banner
YouTube
Demand Gen
Shopping
```

Um domínio do Google, um link comum ou um URL com `gclid` apenas colado pelo cliente no texto livre não são suficientes para atribuição.

O primeiro anúncio válido é preservado. O último anúncio só muda quando existe evidência mais recente.

**Importante:** o NexoFlow 1.0 faz atribuição a partir dos dados recebidos pela Weni. Custos, impressões e cliques do Google Ads exigem uma integração própria com a API do Google Ads e não são inferidos.

## Meta Ads e eventos de anúncio

Os campos genéricos `primeiro_anuncio_*` e `ultimo_anuncio_*` também preservam dados de anúncio disponibilizados pela Weni.

O projeto inclui estrutura para captura exata de referrals:

```text
POST /weni/meta-event
GET /health
```

O módulo opcional aceita dados como:

```text
source_id
headline
source_url
ctwa_clid
contact_uuid
contact_urn
received_at
```

A aba `Campanhas_Meta` já possui schema para anúncio, conjunto, campanha, investimento, impressões e cliques.

A **Meta Marketing API direta** é tratada como evolução preparada, não como integração ativa do NexoFlow 1.0.

## Google Sheets

### Bases operacionais

```text
Base_Contatos
Historico_Alteracoes
Metricas_Diarias
Eventos_Anuncios
Campanhas_Meta
Controle
Pedidos_FarmaLider
Itens_Pedidos_FarmaLider
Metricas_Vendas
```

### Camada BI

```text
BI_Resumo_Diario
BI_Canais
BI_Conversao
BI_Vendas
BI_Ads
BI_Pedidos
BI_Status
```

A camada BI concentra as regras no NexoFlow para que o Looker Studio fique principalmente responsável pela visualização.

### Base_Contatos

Além dos campos Weni, a base preserva dados como:

```text
contact_uuid
telefone
weni_name
nome
cpf
cpf_normalizado
email
idade
maior_de_18_anos
opt_in_lgpd
farmalider_cliente_id
farmalider_cliente_nome
farmalider_cliente_status
farmalider_cliente_consultado_em
ultimo_pedido
pedido_*
campos de anúncio
weni_created_on
weni_modified_on
sincronizado_em
```

## Looker Studio

O dashboard pode trabalhar diretamente sobre as abas BI para priorizar Marketing e Vendas:

- faturamento;
- pedidos;
- compradores;
- ticket médio;
- unidades vendidas;
- conversão;
- canais;
- origem e atribuição de anúncios;
- compradores do dia;
- detalhe do pedido, produtos, quantidades e valores;
- status dos pedidos com código e descrição;
- forma de pagamento com código e descrição;
- saúde da sincronização.

`BI_Status` é indicada para uma página técnica opcional.

## Performance e resiliência

### FarmaLíder API

- concorrência controlada;
- até 4 workers;
- lotes de até 500 CPFs;
- sessão HTTP por thread;
- rate limiter global;
- até quatro tentativas para falhas transitórias;
- cooldown global após HTTP 429, falhas 5xx e erros de conexão;
- recuperação serial e retorno gradual da concorrência;
- intervalo adaptativo limitado, com redução após a recuperação;
- circuit breaker por rota e por taxa de erros do lote;
- telemetria de latência.

Após uma resposta `5xx`, a espera global segue **5, 10 e 20 segundos** nas
tentativas seguintes e permanece ativa mesmo se a última tentativa falhar.
`Retry-After`, quando informado, pode aumentar essa espera. Novas requisições
passam a entrar uma por vez; as que já estavam em andamento podem concluir.
Depois de **6 respostas saudáveis consecutivas iniciadas após a falha**, o
limite passa de 1 para 2. Outras **12 respostas saudáveis** permitem voltar
a 4. Uma nova falha reinicia a recuperação. O intervalo entre requisições
durante a recuperação de `5xx` chega a, no máximo, **1 segundo** e diminui
gradualmente até o valor normal quando a API se recupera.

`404` é tratado como cliente ou pedido não encontrado, sem retry. `500`
continua sendo falha da API e nunca é convertido em ausência de cadastro.
O circuito da rota abre após **12 respostas `5xx` envolvendo pelo menos
3 consultas distintas**, sem uma resposta válida `200` ou `404` entre elas,
e interrompe o Analytics antes de gravar contatos ou pedidos no Google Sheets.

O log usa referências HMAC com chave aleatória por execução e categorias
seguras da resposta, sem copiar CPF, UUID de contato, token ou texto livre do
servidor. Um identificador de correlação enviado pela API só é registrado se
estiver no formato UUID estrito. Datas e horários operacionais e de log usam
**America/Belem (UTC-3)**.

Se restarem consultas de cliente ou pedido com erro, a execução termina como
`parcial` (código `3`) e mantém o marco incremental para nova tentativa. A
próxima execução pode repetir consultas à FarmaLíder, pois o cache não guarda
o conteúdo completo dos pedidos. Essas proteções mitigam sobrecarga e ajudam
no diagnóstico; a causa do erro interno só pode ser confirmada com os logs
do backend. Consulte [o diagnóstico da API](docs/FARMALIDER%20API.md).

### Google Sheets

- timeout explícito;
- retry de leitura e escrita;
- proteção para HTTP 429/5xx;
- batches adaptativos;
- limite conservador de payload;
- redução de leituras redundantes;
- upsert somente quando há mudança real;
- fingerprint de schema;
- progresso operacional em cargas grandes.

## Rede e proxy

### Identificar CPFs com falhas persistentes da FarmaLíder

Após esgotar tentativas por CPF com respostas 5xx, o NexoFlow registra um diagnóstico **privado local**, separado do log normal. Consultas com 5xx interrompidas pelo circuito também são registradas. Uma consulta recuperada por retry não cria registro de falha persistente.

No Windows, abra no Explorador:

```text
%LOCALAPPDATA%\NexoFlow\diagnosticos_privados
```

Cada execução cria `farmalider_falhas_<execucao_id>.jsonl`. Cada linha é um JSON com `cpf`, `consulta_ref`, horário em America/Belem, ambiente, último HTTP 5xx, tentativas, motivo do encerramento, assinatura e campos diagnósticos sanitizados da resposta. O log normal mostra a referência e o caminho do arquivo, sem CPF nem corpo da resposta. A mesma referência é gravada uma única vez por execução.

O arquivo contém CPF e deve permanecer privado. URLs, credenciais, objetos de cliente e identificadores no texto da resposta são removidos; somente campos diagnósticos conhecidos são preservados. Corpos maiores que 4 KiB, HTML, formatos desconhecidos e conteúdo sensível não são copiados integralmente. Em Linux, o diretório é `logs/privado`, criado com permissões restritas.

Os arquivos começam a ser criados nas próximas execuções; referências de logs antigos não permitem recuperar CPFs. A gravação do diagnóstico não altera o tratamento de HTTP 500 como falha da API nem os dados existentes na Weni. O Git ignora esses arquivos, e o empacotamento bloqueia sua inclusão mesmo se forem adicionados ao índice por engano. A pasta Windows fica fora da instalação e não é enviada automaticamente para nenhum serviço.

O NexoFlow pode operar em rede corporativa ou conexão direta.

Com proxy corporativo:

```text
https://api.farmalider.net.br/farmalider
```

Sem proxy:

```text
https://api.farmalider.net.br:3200/farmalider
```

O modo `auto` detecta o ambiente e seleciona a rota adequada.

O teste TCP do proxy confirma somente que sua porta responde. `nexo diagnostico` testa cada serviço separadamente e mostra a rota efetiva para a Weni. Os logs usam America/Belem (UTC-3).

| Diagnóstico | Significado e ação |
|---|---|
| `PROXY_AUTENTICACAO` / HTTP 407 | Proxy exige autenticação ou recusou as credenciais; conferir usuário, senha e método de autenticação. |
| `PROXY_ACESSO_NEGADO` / HTTP 403 no túnel | Proxy negou HTTPS; conferir liberação do domínio com a equipe de rede. |
| `TLS_CERTIFICADO` | Conferir certificados, CA corporativa e relógio; não desativar validação TLS. |
| `DNS` | Conferir resolução dos endereços na rota. |
| `TIMEOUT` | Conexão ou resposta excedeu o limite. |
| `CONEXAO_RECUSADA` | Conferir host, porta, firewall e disponibilidade. |
| `PROXY_CONEXAO` | Falha do proxy sem detalhe suficiente para identificar a causa. |

A classificação analisa erros aninhados sem imprimir mensagens originais, URLs autenticadas, senhas ou tokens. A Weni evita repetir falhas explícitas de autenticação do proxy, bloqueio do túnel e certificado; falhas transitórias continuam com retry. O diagnóstico não muda a rota nem tenta contornar o proxy automaticamente. HTTP 400/404 da FarmaLíder no CPF fictício confirma uma resposta do servidor, mas não confirma autenticação e aparece como aviso.

## Instalação

O pacote **NexoFlow Setup** instala o projeto em:

```text
C:\NexoFlow
```

Requisitos:

- Windows x64;
- Python 3.11 ou superior, 64 bits;
- credenciais e tokens configurados localmente.

Se nenhum Python compatível for encontrado, o Setup pode oferecer o Python 3.14.8 x64 incluído no pacote.

Executáveis:

```text
NexoFlow Analytics.exe
NexoFlow Weni.exe
NexoFlow Terminal.exe
NexoFlow Uninstall.exe
```

O `NexoFlow Uninstall.exe` é uma conveniência. A desinstalação também permanece disponível por:

```text
desinstalar_nexoflow.cmd
installer\uninstall.ps1
```

## Segurança e auditoria

O NexoFlow 1.0 não deve versionar:

```text
secrets_config.py
credentials/google_credentials.json
tokens
senhas de proxy
```

Proteções principais:

- allowlist explícita de campos;
- dry-run antes de escrita;
- auditoria obrigatória;
- confirmação humana;
- snapshot congelado na carga completa;
- SHA-256;
- releitura por contato;
- isolamento de conflitos;
- checkpoint somente após sucesso;
- logs operacionais;
- trilha JSONL de auditoria;
- executáveis sem credenciais embutidas.

## Operação recomendada

### Atualizar tudo em ordem

Execute `comandos/atualizar_tudo.bat` na pasta instalada do NexoFlow, após colocar `Conversas_Delivery.xlsx` e `Contatos_Delivery.xlsx` em `importacoes`.

O fluxo executa Weni completa (100%), Planilhas → Weni, Planilhas → Analytics/Google Sheets, Analytics completo (100%), Recompra e Analytics incremental, nessa ordem. Revise os relatórios e confirme cada aplicação quando solicitado. A Recompra ocorre somente na quarta etapa, mesmo quando habilitada para execução após a carga Weni individual.

Erro, cancelamento, conflito ou consulta parcial interrompe a sequência. Confira os logs antes de retomar; uma aplicação parcial precisa ser reconciliada com o mesmo snapshot revisado. Ao concluir, atualize manualmente os dados e os campos das fontes alteradas no Looker Studio. O terminal permanece aberto para mostrar o resultado final.

### Carga completa fragmentada em 25%

Para reduzir o tempo e o risco operacional de uma carga integral longa, `comandos/atualizar_weni_carga_completa.bat` e `comandos/executar_carga_completa.bat` permitem escolher **100%** ou uma das quatro partes de **25%**.

As partes 1/4 a 4/4 usam um manifesto compartilhado em `logs\full_load_25_manifest.json`. A base é congelada na primeira parte, ordenada por UUID e dividida por limites inteiros determinísticos. Assim, as quatro partes cobrem a base uma única vez, sem sobreposição. Novos contatos após o corte não entram no meio da campanha e permanecem elegíveis para a incremental posterior.

Cada componente mantém sua própria sequência de conclusão: Weni 1→2→3→4 e Analytics 1→2→3→4. Se uma parte falhar, ela não é marcada como concluída e pode ser executada novamente. No Analytics, o BI intermediário reflete a base acumulada até aquele ponto; a visão final equivalente à carga integral ocorre após a Parte 4.

### Primeira carga ou revalidação após atualização

```text
1. Instalar NexoFlow 1.0
2. comandos/testar_conexoes.bat
3. comandos/atualizar_weni_carga_completa.bat
4. revisar logs\weni_update_review_full.txt
5. confirmar CARGA COMPLETA REVISADA
6. executar carga completa do Analytics
```

Após atualizar ou reinstalar, essa sequência gera um novo dry-run com o de-para atual antes do apply. Refaça a revisão do plano e execute o Analytics completo; snapshots antigos não devem aplicar a política anterior de pagamento 0.

### Rotina

```text
1. NexoFlow Weni.exe
2. NexoFlow Analytics.exe
```

## Documentação

- [NexoFlow Weni](docs/README%20WENI.md)
- [NexoFlow Recompra](docs/README%20RECOMPRA.md)
- [NexoFlow Planilha](docs/README%20DELIVERY.md)
- [Reconciliação dos atendimentos Delivery com tags de pedido](docs/README%20DELIVERY.md)
- [NexoFlow Terminal](docs/README%20TERMINAL.md) — arquivo `docs/README TERMINAL.md`
- [Instalação](docs/README%20INSTALACAO.md)
- [Pedidos](docs/PEDIDOS.md)
- [FarmaLíder API](docs/FARMALIDER%20API.md)
- [Google Ads](docs/GOOGLE%20ADS.md)
- [Google Sheets / Service Account](docs/GOOGLE%20SETUP.md)
- [Looker Studio](docs/LOOKER%20STUDIO.md)
- [Proxy e rede](docs/README%20PROXY.md)
- [VS Code](docs/README%20VSCODE.md)
- [Antivírus e confiança](docs/ANTIVIRUS%20E%20CONFIANCA.md)
- [Histórico de mudanças](CHANGELOG.md)
- [Processo de mudanças](docs/PROCESSO%20DE%20MUDANCAS.md)

## NexoFlow 1.0

O repositório, a documentação, os executáveis, o Setup, a CLI e a distribuição oficial usam a identificação **NexoFlow 1.0**.

## Localização e idade dos clientes

O Analytics envia `cidade`, `estado`, `cep`, `bairro` e `idade` dos campos personalizados da Weni para `Base_Contatos` e para a camada `BI_Pedidos`. O histórico de alterações também preserva a localização. Os pedidos recebem o perfil do contato vinculado pelo `contact_uuid`, sem criar novas linhas por produto nem multiplicar os valores da venda.

| Campo | Campo Weni | Tipo no Looker Studio |
|---|---|---|
| `cidade` | `@fields.cidade` | Texto; cidade para mapas quando a localização puder ser identificada |
| `estado` | `@fields.estado` | Texto; UF para contextualizar a cidade brasileira |
| `cep` | `@fields.cep` | Texto, preservando zeros iniciais e a formatação recebida |
| `bairro` | `@fields.bairro` | Texto |
| `idade` | `@fields.idade` | Número; dimensão, sem somar idades |

A localização e a idade correspondem ao **cadastro atual na Weni**, inclusive para pedidos antigos. Não representam necessariamente endereço de entrega ou idade na data da compra. Valores ausentes continuam vazios; idade inválida na camada BI também fica vazia, sem virar zero. A idade zero informada é preservada. A data real do pedido continua em `pedido_data` e `data`, separada de `atualizado_em`.

Depois de instalar a distribuição atualizada, execute uma **carga completa do Analytics** para preencher esses campos nos contatos já existentes. A carga Analytics por planilhas também exporta os campos dos contatos selecionados e relidos na Weni, após revisão humana. As colunas novas são acrescentadas sem deslocar os cabeçalhos existentes.

No Looker Studio, atualize os campos das fontes `Base_Contatos` e `BI_Pedidos` uma vez após a migração. Use cidade, bairro, CEP e faixa etária como dimensões e filtros; para pedidos e faturamento por região, use `BI_Pedidos`, filtre vendas por `venda_valida = 1` e utilize `data` como dimensão de período. Não some snapshots da base ou idades. Para contagem de clientes, use pessoas únicas no contexto da fonte, não a quantidade de linhas de pedidos. O relatório não recebe automaticamente componentes novos apenas porque as colunas foram adicionadas.

## Recompra FarmaLíder D+25

A campanha é destinada a produtos para humanos. Itens identificados como pet, de uso animal ou veterinário pelo nome, categoria ou código de referência do catálogo pet ficam fora da mensagem. Pedidos somente animais não entram no grupo; pedidos mistos mantêm apenas os produtos humanos. As revisões anteriores a esse filtro precisam ser geradas novamente. Remover um agendamento mantém a proteção contra repetir a admissão do mesmo pedido.

Grupo sincronizado com a última compra confirmada entre 0 e 25 dias, remoção depois de 25 dias e por recusa. O envio continua somente no D+25 pela campanha Weni. Execute `nexo weni recompra auditoria`, `dry-run` e `aplicar` com revisão, ou `nexo weni recompra atualizar`. Confira [implementação, configuração e limites da recompra](docs/README%20RECOMPRA.md). A rotina não altera marketing_opt_in, não dispara WhatsApp e não modifica a campanha.

## NexoFlow Planilha

O Setup instala **NexoFlow Planilha.exe** com ícone próprio e atalho na Área de Trabalho. O menu permite executar as cargas por planilha para Weni ou Analytics/Google Sheets e abrir `importacoes`. Use `Conversas_Delivery.xlsx` e `Contatos_Delivery.xlsx`; cada carga mantém a revisão humana antes de aplicar.

## Dados comerciais para o Looker Studio

O Sheets e o dashboard usam o recorte da farmácia: pedidos somente animais são excluídos, e pedidos mistos preservam os itens humanos e o valor desses itens, sem frete. A primeira execução atualizada também saneia os campos de pedido da Base_Contatos, o histórico e os indicadores já gravados. A identificação cruza nomes e categorias com códigos de referência e nomes exatos do catálogo pet. IDs de produto/SKU do e-commerce não são tratados como códigos da API. O catálogo parcial não comprova uso humano dos produtos ausentes. Sua atualização reprocessa o histórico e exige novo dry-run da Recompra. O índice distribuído contém apenas fingerprints para correspondência; o arquivo original permanece privado. Embalagens PET e nomes de medicamentos humanos compartilhados com a veterinária não são classificados como animais por mera semelhança. Confira os relatórios quando o catálogo não identificar claramente o produto.

O Analytics preserva os pedidos identificados em `Pedidos_FarmaLider` e seus itens em `Itens_Pedidos_FarmaLider`. Uma nova compra acrescenta uma chave de pedido; uma nova coleta do mesmo pedido atualiza essa chave, sem duplicar a venda nem apagar outros pedidos. Cancelamentos e mudanças de status permanecem no histórico operacional e atualizam os resultados comerciais. Execute as coletas regularmente: a API retorna apenas o último pedido, portanto várias compras entre duas coletas podem não ser capturadas.

Em cada carga do Analytics, os pedidos oficiais já observados dos contatos selecionados são reconsultados por código quando têm até 20 dias, pela data do pedido no fuso de Belém (dias 0 a 20, inclusive). Isso inclui concluídos e cancelados. Pedidos mais antigos permanecem no histórico e nos indicadores, sem reconsulta automática de status. A consulta por CPF continua identificando novas compras; pedidos ainda não registrados precisam da validação inicial mesmo quando antigos. Falhas de consulta preservam o registro anterior e deixam a carga parcial, sem avançar o checkpoint. Registros antigos sem vínculo de identidade comprovado continuam pendentes de validação.

As novas abas comerciais são reconstruídas a partir de **todo o histórico já gravado**, não apenas dos contatos processados na execução atual. Elas mantêm nomes completos e chaves opacas para relacionamentos internos, sem CPF, telefone, e-mail, número original de pedido, status ou pagamento.

| Fonte | Uso no Looker |
| --- | --- |
| `BI_Publico` | Uma linha por contato atual: canal principal, cidade, estado, gênero, idade e compra identificada. Use contagem de contatos, soma de `compra_identificada`, diferença e percentual para os quatro cards. Não aplique o período de vendas à base atual; `criado_em` é cadastro na Weni, sem comprovação de aquisição por Marketing. |
| `BI_Clientes` | Uma linha por pessoa: pedidos e faturamento identificados e última compra identificada no histórico disponível. Não use esta tabela para métricas filtradas por data de compra. |
| `BI_Clientes_Vendas` | Uma linha por venda válida: cliente, chave estável, data, receita dos produtos, frete e faturamento. Fonte de Meus clientes, Vendas, Top 10 e indicadores do período escolhido no relatório. |
| `BI_Itens` | Uma linha por item de venda válida: SKU, nome, categoria quando informada, quantidade, preço unitário e receita do item. Fonte dos rankings e filtros livres por data, sem multiplicar o total do pedido entre produtos. |
| `BI_Produtos_ABC` | Todos os produtos do período indicado em `periodo_inicio` e `periodo_fim`, com rankings, participação, participação acumulada e classes. |
| `BI_Produtos_Juntos` | Pares de SKUs do período indicado, contando pedidos distintos; várias linhas do mesmo SKU num pedido não multiplicam uma combinação. |
| `BI_KPIs_Periodo` | Indicadores já calculados para o período explícito: clientes, pedidos, faturamento, ticket, pedidos e receita por cliente, SKUs distintos e participação dos cinco produtos de maior receita. |
| `BI_Qualidade` | Cobertura de idade e cadastro, início das novas coletas, pedidos sem itens ou com itens divergentes e disponibilidade dos indicadores de histórico, ABC e anúncios. Use fora das páginas comerciais, na configuração ou em Atualização dos dados. |

Em `BI_Clientes_Vendas`, agrupe **somente por `cliente_chave`**, usando `cliente` como nome de apresentação. Não agrupe pela data ou pelo nome. Pedidos = `SUM(pedidos)`; clientes = `COUNT_DISTINCT(cliente_chave)`; pedidos por cliente = `SUM(pedidos) / COUNT_DISTINCT(cliente_chave)`; receita média por cliente = `SUM(faturamento) / COUNT_DISTINCT(cliente_chave)`; ticket = `SUM(faturamento) / SUM(pedidos)`. A última compra do período é `MAX(data)`; a última identificada em todo o histórico vem de `BI_Clientes`. Não some compradores diários nem tire média dos tickets diários. Top 10 por pedidos pode desempatar por faturamento; quantidades empatadas não indicam um líder exclusivo.

A Curva ABC utiliza a receita de **todos os itens** do período, sem frete. Produtos são ordenados por receita; um produto que cruza o limite permanece na classe iniciada antes dele (A até 80%, B até 95%, C depois). Produtos empatados em receita mantêm o mesmo ranking e a mesma classe; um grupo empatado pode ultrapassar o limite. Sem receita positiva ou SKU para todos os itens do período, ou quando faltar algum item/pedido ou a soma dos itens divergir do valor dos produtos em mais de R$ 0,01, `abc_disponivel=0` e as classes ficam vazias. Sem cobertura completa de itens do período, a participação dos cinco produtos também fica vazia. Confira `pedidos_sem_itens`, `pedidos_com_itens_divergentes` e `abc_periodo_disponivel` em `BI_Qualidade`. Receita não representa lucro. Quantidades fracionárias são preservadas quando a origem as fornece; formatar como inteiro não deve ocultar uma fração real.

**Períodos:** as tabelas de fatos (`BI_Clientes_Vendas` e `BI_Itens`) aceitam o filtro de datas do relatório. ABC, combinações e `BI_KPIs_Periodo` são calculados por padrão para os últimos 30 dias, inclusive hoje; não podem ser refiltrados para outro período como se os cálculos fossem refeitos automaticamente. Para outro intervalo, execute no Terminal:

```text
nexo looker --inicio 2026-10-01 --fim 2026-10-11
```

Esse comando utiliza o histórico do Sheets e não consulta Weni ou FarmaLíder. A carga normal também aceita `NEXOFLOW_BI_INICIO` e `NEXOFLOW_BI_FIM` no ambiente. Confira as datas das tabelas de período antes de apresentar os resultados. No Looker, mantenha essas tabelas com o intervalo explícito e evite misturá-las a um filtro de outro período.

**Cobertura histórica:** pedidos antigos retornados como último pedido não comprovam todas as compras antigas. O início das novas coletas é registrado em `Controle.historico_observado_desde`. O histórico é acumulado, mas não declarado completo; recompra completa, clientes sem compras recentes e variações comparativas permanecem indisponíveis enquanto a origem não comprovar cobertura suficiente. `clientes_com_multiplas_compras_identificadas` conta apenas as compras registradas e não é uma taxa completa de recompra.

**Campos opcionais:** `genero` é lido somente de `@fields.genero`; não há dedução pelo nome nem preenchimento automático na Weni. Quando o campo não existir ou estiver vazio, o valor fica vazio. `ultima_compra` da Weni também é preservada na base operacional. Categoria é exportada somente quando a resposta oficial de cada item trouxer `categoria` como texto; ausência permanece vazia. Investimento e atribuição compatível continuam necessários para ROAS e custos de anúncios; ausência não significa custo ou receita iguais a zero.

**Tipos e atualização:** a migração corrige valores numéricos de `BI_Resumo_Diario` que haviam sido interpretados como datas e preserva as linhas anteriores. As novas tabelas gravam datas como datas e medidas como números; identificadores de produto e nomes permanecem texto literal. Depois de instalar o Setup atualizado, execute uma carga completa do Analytics para preencher os campos atuais da Weni. Adicione as novas abas como fontes no Looker, atualize os campos de `Base_Contatos`/`BI_Pedidos` e configure moeda com duas casas decimais. Ocultar as chaves internas na apresentação não interfere no agrupamento.


## Verificação de qualidade

Para verificar o código, instale `requirements-dev.txt` em um ambiente de desenvolvimento e execute `ruff check .`, `pyright` e `pip-audit -r requirements.txt -r requirements-webhook.txt`. Essas ferramentas não são necessárias para usar o programa instalado. O fluxo de publicação também verifica os testes no Windows com Python 3.11 e 3.14, com e sem o modo UTF-8 do Python.

## Contatos duplicados

O NexoFlow Weni inclui a deduplicação no dry-run e na aplicação. Somente contatos comprovados nas APIs são excluídos; pedidos reais e recusas são preservados, com limpeza das fontes do Sheets e do Looker. Consulte [Deduplicação na rotina Weni](docs/deduplicacao_weni.md).
