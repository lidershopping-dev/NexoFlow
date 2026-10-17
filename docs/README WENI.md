# NexoFlow 1.0 — Weni

## Objetivo

A rotina **NexoFlow Weni** elimina o processo manual de exportar mensagens/contatos da Weni, tratar planilhas e importar uma nova planilha. Ela consulta a API da Weni, monta um plano de alterações, audita obrigatoriamente esse plano e só então permite gravar os campos.

## Acompanhamento no CMD

Durante consultas, preparação, dry-run e aplicação, o CMD e o log recebem
atualizações a cada 2 segundos, inclusive durante espera pela API:

```text
Aplicação Weni [##########----------]  50% | 9500/19000 contatos | 30.0 contatos/s | decorrido=00:05:17 | restante estimado=00:05:17 | aplicados=9400; conflitos=50; já corretos=50; falhas=0
```

A barra vai de 0% a 100% **em cada etapa com total conhecido**. Um contato
avança a contagem depois de seu processamento e do registro de auditoria,
inclusive se for ignorado por conflito ou já estiver correto. Aplicados são
mostrados separadamente: percentual processado não significa percentual gravado.
Falhas isoladas deixam apenas o contato ou lote pendente; os demais continuam.

A velocidade de contatos/registros usa o trabalho concluído nos últimos dez
segundos; o tempo restante usa a média desde o início da etapa. Esperas e
repetições de API entram no tempo medido. A estimativa pode mudar conforme a API
fica mais rápida ou mais lenta; sem processamento concluído, aparece “calculando”.
Na auditoria JSONL, a velocidade adicional usa os bytes UTF-8 efetivamente
escritos pelo programa, sem contar conteúdo de execuções anteriores. B/s, KB/s e
MB/s usam unidades decimais; não representam velocidade de rede nem fsync do disco.

Consultas paginadas cujo total ainda é desconhecido mostram registros recebidos,
registros/s e tempo decorrido, sem inventar porcentagem ou previsão. O 100% da
aplicação indica o término da etapa; a execução completa termina com o resumo e
a atualização do checkpoint.

A atualização do código passa a valer nas próximas execuções. Uma execução já
aberta continua usando o código carregado no início.

## Execução recomendada

### Uso diário

Abra o atalho:

```text
NexoFlow Weni
```

O executável chama `comandos/atualizar_weni.bat` e executa:

```text
1. comandos/check-up.bat
2. comandos/testar_update_weni.bat
3. dry-run incremental
4. auditoria obrigatória do plano
5. confirmação manual: ATUALIZAR
6. apply incremental
```

O apply faz uma nova leitura e uma nova auditoria antes de qualquer POST.

### Primeira carga / reprocessamento integral

Use:

```text
comandos/atualizar_weni_carga_completa.bat
```

A rotina faz dry-run completo, gera um relatório detalhado para revisão humana e exige a confirmação literal `CARGA COMPLETA REVISADA` antes do apply. Use para primeira implantação, backfill ou revalidação integral.

Na carga de 100% ou em partes de 25%, o Bloco de Notas abre o relatório e o CMD aguarda a janela de confirmação. Depois de revisar, digite `CARGA COMPLETA REVISADA` nessa janela. Fechar o editor não autoriza o apply. Cancelar ou fechar a confirmação impede a gravação e mantém o CMD aberto até você pressionar uma tecla. Se a confirmação falhar, o código de erro também permanece visível.

O componente `scripts\confirmar_revisao_weni.ps1` faz parte do projeto completo e do Setup. Se ele estiver ausente, a rotina interrompe a carga antes da revisão e solicita a reinstalação do Setup atualizado.

Depois de atualizar ou reinstalar o NexoFlow 1.0, gere um novo dry-run completo com a política atual de pagamento/status e revise o novo relatório. Em seguida, execute a carga completa do Analytics para atualizar Google Sheets e BI. Snapshots anteriores à nova política de cancelamento por pagamento 0 devem ser regenerados.

Arquivos gerados:

```text
logs\weni_update_review_full.txt   relatório legível para o operador
logs\weni_update_review_full.json  snapshot verificável do plano revisado
```

O TXT é aberto automaticamente no Bloco de Notas. Ele mostra, por contato, valores anteriores e propostos, evidência técnica, ações de grupo e confirmação FarmaLíder. No NexoFlow 1.0, o plano revisado fica **congelado**. Depois da confirmação, o NexoFlow não recalcula mensagens nem inclui novos contatos no mesmo apply. Em vez disso, relê cada contato imediatamente antes de gravá-lo. Se os campos/URNs/fontes relevantes daquele contato mudaram, somente aquele contato é marcado como conflito e ignorado. Os demais continuam normalmente. O checkpoint usa o corte temporal do dry-run, permitindo que mensagens, contatos e conflitos posteriores sejam reprocessados na próxima incremental.

### Carga completa fragmentada em quatro partes

Ao abrir `comandos/atualizar_weni_carga_completa.bat`, escolha entre a carga tradicional de 100% ou as Partes 1/4, 2/4, 3/4 e 4/4. Cada parte continua usando o mesmo fluxo seguro de dry-run, auditoria, relatório humano, confirmação e apply do snapshot congelado.

A Parte 1 cria `logs\full_load_25_manifest.json` com a lista congelada de UUIDs da campanha. A segmentação usa ordem canônica por UUID, e não a ordem mutável de `modified_on` da API. Por isso, alterações ou novos contatos entre uma parte e outra não mudam os limites. A Parte 2 exige a Parte 1 concluída; a Parte 3 exige 1 e 2; a Parte 4 exige 1, 2 e 3.

Na carga Weni de 25%, o processamento pesado de contatos, pedidos e FarmaLíder fica restrito à parte selecionada. A listagem paginada global de mensagens pode continuar sendo lida para preservar o contexto com menos chamadas do que consultar milhares de contatos individualmente; somente mensagens/contatos pertencentes ao fragmento entram no plano e na validação de pedidos.

Se uma parte falhar, o manifesto não avança. Execute novamente a mesma parte; contatos já satisfeitos continuam protegidos pelas verificações `already_satisfied` e pelo snapshot revisado.

### Somente simulação

Use:

```text
comandos/atualizar_weni_dry_run.bat
```

Nenhuma alteração é gravada e o checkpoint não é avançado.

### Teste de acesso

Use:

```text
comandos/testar_update_weni.bat
```

O teste é somente leitura e valida acesso a contatos, mensagens, grupos Opt-In LGPD e Opt-Out LGPD e disponibilidade da FarmaLíder para confirmação de pedidos.

## Campos que a rotina pode escrever

A lista é fechada e auditada:

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
pedido_status
pedido_forma_pagamento
pedido_produtos
pedido_quantidade_itens
pedido_valor_produtos
pedido_valor
ultima_compra
primeiro_anuncio_origem
primeiro_anuncio_id
primeiro_anuncio_titulo
primeiro_anuncio_url
primeiro_anuncio_clique_id
primeiro_anuncio_data
primeiro_anuncio_tipo_campanha
primeiro_anuncio_canal_destino
ultimo_anuncio_origem
ultimo_anuncio_id
ultimo_anuncio_titulo
ultimo_anuncio_url
ultimo_anuncio_clique_id
ultimo_anuncio_data
ultimo_anuncio_tipo_campanha
ultimo_anuncio_canal_destino
```

Qualquer tentativa de escrever outro campo bloqueia a execução antes do primeiro POST.

## Regras de URN e canais

A Weni continua sendo a fonte de verdade das URNs. O NexoFlow apenas classifica as identidades que a API realmente retorna, sem fabricar endereço de canal.

Prioridade da URN principal quando existem vários canais:

```text
WhatsApp > Instagram > Facebook/Messenger > outra URN nativa
```

Campos mantidos pela rotina:

```text
contact_urn            URN principal
contact_urn_whatsapp   URN oficial de WhatsApp
contact_urn_instagram  URN oficial de Instagram
contact_urn_facebook   URN oficial de Facebook/Messenger
contact_canal_principal  whatsapp | instagram | facebook | outro
```

Antes do dry-run ou apply, o NexoFlow consulta a API de campos da Weni e exige que os quatro campos multicanal específicos existam. Eles devem ser criados como **Texto** na Weni. Se algum estiver ausente, a execução é interrompida antes de qualquer escrita em contatos.

Regras:

- a URN é preservada exatamente como recebida da Weni;
- `messenger:` e variantes reconhecidas de Facebook são classificadas como `facebook`, mas o valor original da URN não é reescrito;
- se o contato tiver WhatsApp e Instagram, `contact_urn` usa WhatsApp e os dois canais permanecem registrados nos campos específicos;
- se houver apenas Instagram, o Instagram vira a URN principal;
- se houver apenas Facebook/Messenger, esse canal vira o principal;
- sem URN nativa reconhecida, o NexoFlow pode manter a `contact_urn` já existente ou usar outra URN nativa como `outro`;
- um campo específico não é apagado por ausência temporária da URN numa resposta da API;
- clique/anúncio não é URN. `primeiro_anuncio_*` e `ultimo_anuncio_*` continuam registrando atribuição de mídia separadamente.

A revisão humana mostra a URN e o canal principal, contabiliza alterações de identificação de canal e também informa quantos contatos receberam atribuição Google Ads. O snapshot de revisão do NexoFlow 1.0 usa schema 6, inclui guardas de concorrência e o corte temporal do dry-run. Snapshots incompatíveis são rejeitados para evitar apply com semântica incorreta.



## Atendimento 24h e concorrência
A carga completa foi adaptada para ambientes em que a Weni continua recebendo mensagens e contatos durante toda a revisão. O dry-run fixa um corte temporal (`captured_at_utc`) e gera o snapshot schema 6. O apply usa os planos armazenados nesse JSON, valida seu SHA-256 e não volta a montar o plano a partir do histórico.

Antes de cada contato ser gravado, o NexoFlow consulta novamente aquele contato na Weni. Para cada campo planejado, o valor atual precisa continuar igual ao valor anterior revisado ou já ter chegado ao próprio valor proposto. Também são protegidas as URNs nativas, `opt_in_lgpd` quando governa grupos e os campos `pedido_*` usados como snapshot operacional. Se houver divergência, o contato recebe status de auditoria `conflict_skipped` e a carga continua para os demais. Se outra automação já aplicou exatamente o mesmo resultado, o status é `already_satisfied` e não há regravação.

Ao final, o checkpoint avança para o **corte do dry-run**, não para o horário final da aplicação. Assim, tudo que chegou ou mudou depois do snapshot é elegível para a próxima execução incremental, inclusive os contatos que foram isolados por conflito. Uma falha de POST é registrada para nova conferência na próxima incremental; os demais contatos continuam.

## Google Ads e canal de destino

O NexoFlow 1.0 pode reconhecer um contato como vindo de **Google Ads** quando a evidência do clique chega à Weni em campos/metadados estruturados. São aceitos marcadores explícitos como `gclid`, `gbraid`, `wbraid`, `utm_source=google` com meio pago ou metadados equivalentes de Google Ads. Um URL comum do Google é ignorado. Para evitar falso positivo, um link com `gclid` simplesmente colado pelo cliente no texto livre da conversa também não é usado como prova de origem.

O anúncio e o canal continuam separados. Exemplos:

```text
Google Ads Performance Max -> WhatsApp
  primeiro_anuncio_origem = Google Ads
  primeiro_anuncio_tipo_campanha = Performance Max
  primeiro_anuncio_canal_destino = whatsapp
  contact_urn = whatsapp:...

Google Ads Search -> Instagram Direct
  ultimo_anuncio_origem = Google Ads
  ultimo_anuncio_tipo_campanha = Search
  ultimo_anuncio_canal_destino = instagram
  contact_urn_instagram = <URN oficial da Weni>

Google Ads YouTube -> Messenger
  ultimo_anuncio_origem = Google Ads
  ultimo_anuncio_tipo_campanha = YouTube
  ultimo_anuncio_canal_destino = facebook
  contact_urn_facebook = <URN Facebook/Messenger da Weni>
```

Tipos reconhecidos quando a evidência é explícita: **Performance Max**, **Search**, **Display/Banner**, **YouTube**, **Demand Gen** e **Shopping**. `utm_medium=cpc` sozinho não distingue Search de Performance Max, por isso o NexoFlow não adivinha o tipo nesse caso.

O primeiro anúncio válido é preservado. O último anúncio só é substituído por uma evidência realmente mais recente. Os campos `primeiro_anuncio_tipo_campanha`, `primeiro_anuncio_canal_destino`, `ultimo_anuncio_tipo_campanha` e `ultimo_anuncio_canal_destino` são opcionais: se não existirem na Weni, a rotina avisa e continua com os campos genéricos de anúncio disponíveis.

**Limitação importante:** se o Google Ads abrir diretamente WhatsApp, Instagram ou Messenger e nenhum `gclid`/`gbraid`/`wbraid`/UTM/metadado chegar à Weni, o NexoFlow não consegue provar a origem e não marca o contato como Google Ads. Para atribuição garantida nesses casos é necessário preservar a informação do clique em um redirecionador/landing de rastreamento ou integração equivalente.

## Regras de CPF

- o campo autorizado é `cpf` (`@fields.cpf` na Weni);
- o NexoFlow aceita CPF com ou sem pontuação, mas grava sempre 11 dígitos sem pontuação;
- o documento precisa passar pela validação oficial dos dois dígitos verificadores;
- em uma mensagem comum, o número só é considerado CPF quando o próprio texto o identifica como CPF, por exemplo `Meu CPF é 529.982.247-25`;
- um CPF isolado, por exemplo `52998224725`, só é aceito quando existe uma solicitação contextual anterior de CPF;
- se uma mesma mensagem contiver mais de um CPF válido, ela é tratada como ambígua e não altera o cadastro;
- se o campo `cpf` estiver vazio, o CPF válido mais recente encontrado pode preenchê-lo;
- se o cliente informar depois um CPF válido diferente do que está salvo, o CPF mais recente substitui o anterior;
- o mesmo CPF em outra formatação não é regravado;
- nenhuma mensagem sem CPF válido limpa ou substitui um valor já existente.

## Regras de idade e maioridade

- idade explícita, como `Tenho 27 anos`, pode preencher `idade = 27` e `maior_de_18_anos = sim`;
- idade abaixo de 18 gera `maior_de_18_anos = não`;
- declaração explícita de maioridade pode atualizar apenas `maior_de_18_anos`;
- respostas curtas como `27`, `Sim` e `Não` só são aceitas quando existe uma pergunta contextual clara imediatamente anterior;
- mensagens ambíguas são ignoradas.

## Regras de LGPD

- Decisões usam mensagens recebidas, com data válida. Respostas compostas são aceitas quando o sentido está claro; respostas contextuais exigem uma pergunta compatível na janela de 24 horas.
- Continuar expressamente o atendimento após uma pergunta única de autorização dos dados pode indicar aceite operacional, preservando recusas já registradas. Isso não altera `marketing_opt_in`.
- Uma pergunta que pede explicitamente `SIM` para confirmar maioridade e autorizar os dados para pedido/entrega permite registrar ambas as confirmações. Uma resposta negativa isolada não identifica qual requisito foi recusado.
- aceite inequívoco em contexto explícito de LGPD/consentimento pode definir `opt_in_lgpd = ativo`;
- recusa inequívoca pode definir `opt_in_lgpd = recusado`;
- `Sim`/`Não` sem contexto de LGPD nunca altera consentimento;
- `opt_in_lgpd_status` não é escrito pelo NexoFlow;
- `ativo` -> garantir associação ao grupo **Opt-In LGPD** e remover do **Opt-Out LGPD**, caso esteja nele;
- `recusado` -> garantir associação ao grupo **Opt-Out LGPD** e remover do **Opt-In LGPD**, caso esteja nele;
- o valor de `opt_in_lgpd` é a fonte de verdade: a rotina também corrige contatos já existentes com grupos incompatíveis;
- quando precisar trocar de grupo, o NexoFlow tenta primeiro adicionar o grupo correto e depois remover o incompatível.

## Regras de pedido e última compra

A FarmaLíder API é a fonte prioritária dos dados de pedido. A Weni recebe um snapshot operacional simples, legível e alinhado por posição.

Fluxo:

```text
CPF
-> GET cliente/cpf/{cpf}
-> ultimoPedido + itens
-> GET pedido/{pedido_id}
-> campos pedido_* na Weni
```

O endpoint por CPF já possui dados suficientes para montar o pedido. O endpoint por código é preferido como confirmação final do mesmo pedido e fornece também `formaPagamento`.

Campos atualizados:

```text
pedido_id                 340793
pedido_produtos           Produto A, Produto B
pedido_quantidade_itens   1, 1
pedido_valor_produtos     53.9, 58.9
pedido_valor              112.8
pedido_data               05/06/26 15:45:27
pedido_status             3
pedido_forma_pagamento    2
```

Regras:

- `pedido_produtos` usa somente `itens[].descricao`; `codProduto` não é usado como nome;
- produtos, quantidades e valores unitários são listas separadas por vírgula e mantêm a mesma ordem;
- `pedido_valor` é a soma de `itens[].total`, sem adicionar frete;
- o frete não é gravado nos campos Weni de pedido; ele é preservado no Google Sheets como `farmalider_frete` e usado pelo Analytics para calcular `valor_total_faturamento = pedido_valor + frete`;
- `pedido_data` é gravada no formato `DD/MM/YY HH:MM:SS`, usando America/Belem;
- `pedido_status` guarda o status efetivo: -2 Em aberto, -1 Pendente de pagamento, 0 Em processamento, 4 Pronto para rota, 1 Em trânsito, 2 Concluído, 3 Cancelado;
- `pedido_forma_pagamento` guarda o código Int32 não negativo normalizado da API: 1 Dinheiro, 2 Débito, 3 Crédito, 4 Ticket, 5 Pix por Link de Pagamento, 6 Pix no POS; a exceção é pagamento 0 ou `_`, que grava o texto `Cancelado` e força `pedido_status=3`;
- os normalizadores aceitam os nomes oficiais dos enums, como `PendenteDePagamento`, `EmProcessamento`, `Concluido`, `PixPorLinkDePagamento` e `PixNoPOS`; pagamento `_` é normalizado para 0;
- `Pedidos_FarmaLider` preserva status e pagamento brutos da API; Weni e `BI_Pedidos` usam o status efetivo Cancelado quando o pagamento é 0;
- os rótulos amigáveis de status e pagamento são derivados no Analytics/BI; pagamento válido sem de-para a partir de 7 aparece como `Não identificado (código X)`;
- `pedido_id` é o primeiro localizador de fallback quando o GET por CPF não puder ser usado;
- `ultimo_pedido` é o **último localizador**: ele pode vir de outra integração e serve somente para recuperar um ID de pedido que depois será confirmado em `GET pedido/{id}`;
- `ultimo_pedido` nunca substitui a API como fonte de produtos, valores, status, data ou pagamento; se a FarmaLíder estiver indisponível, os campos `pedido_*` existentes são preservados e não são reconstruídos a partir do snapshot legado;
- `ultima_compra`, quando o campo existe, avança nas rotinas convencionais somente para pedido confirmado pela FarmaLíder com status efetivo -2, -1, 0, 4, 1 ou 2, sem cancelamento explícito e sem pagamento 0; somente esses pedidos entram nas métricas de venda.

Os campos `pedido_id`, `pedido_data`, `pedido_status`, `pedido_produtos`, `pedido_quantidade_itens`, `pedido_valor_produtos`, `pedido_valor` e `pedido_forma_pagamento` são obrigatórios na Weni. Se qualquer um estiver ausente, o preflight bloqueia a execução antes de qualquer atualização de pedido.

`pedido_quantidade_itens` e `pedido_forma_pagamento` devem ser **Texto**. O primeiro guarda uma lista alinhada aos produtos, como `2, 1`; o segundo guarda os códigos de pagamento como texto e o valor `Cancelado`. Em campos Numéricos, esses valores podem aparecer vazios ou perder a lista. O preflight bloqueia tipos incompatíveis informados pela API; o NexoFlow não altera o cadastro dos campos automaticamente. Antes de migrar uma instalação existente, confira os fluxos e filtros que usam esses campos e preserve `pedido_data` como Data e hora. Quando a resposta da gravação contém os campos do contato, o NexoFlow também verifica se os valores propostos foram preservados.

O campo opcional de Texto `pedido_revisao_pendente` sinaliza uma compra posterior ainda não confirmada pela API. Ele guarda a data da evidência, sem alterar `pedido_data` ou inventar número e produtos. Com esse campo, o bloqueio da referência antiga de recompra permanece entre instalações. Uma confirmação oficial no mesmo dia ou depois resolve a pendência; o NexoFlow Weni grava `resolvido`. No Sheets, o campo aparece em `Base_Contatos`, com indicadores em `BI_Publico`, `BI_Clientes` e `BI_Qualidade`.

Pedidos com status -2, -1, 0, 1 ou 4 permanecem na fila de rechecagem até chegarem a 2, Concluído, ou 3, Cancelado. Todos os estados documentados não cancelados contam como vendas. A cada nova carga, os pedidos no escopo selecionado são reconsultados, mesmo sem nova mensagem ou alteração na Weni.

## Incremental e checkpoint

O estado fica em:

```text
logs\weni_update_state.json
```

A rotina usa uma sobreposição de segurança no período consultado e é idempotente: valores idênticos não são regravados. O checkpoint avança após processar os contatos disponíveis; os que falharam ficam na fila de revalidação da próxima incremental, com recuperação do histórico de mensagens quando necessário.

## Auditoria e logs

Log operacional:

```text
logs\nexoflow_update_weni.log
```

Trilha de auditoria estruturada:

```text
logs\weni_update_audit.jsonl
```

A auditoria registra contato, campos anteriores/novos, evidência técnica, ações de grupo e confirmação de pedido. O corpo completo das mensagens do cliente não é salvo nessa trilha.

Preparação e auditoria do plano acontecem antes das gravações. O total de contatos com pedido validado pela API inclui os que não precisam de alteração; o total de contatos com alterações é informado separadamente.

Pagamento sem de-para é informativo: cada código aparece em `INFO` uma vez por execução, com totais por código no resumo. O código é preservado e esse registro não bloqueia a atualização.

## Princípios de segurança

- nunca inventar dados;
- nunca gravar vazio sobre um valor válido;
- preferir API/flow oficial a interpretação de conversa;
- pedido só atualiza campos de compra com confirmação FarmaLíder;
- nenhuma escrita acontece antes de `audit_plans`;
- carga completa aplica exatamente o snapshot congelado revisado pelo operador, sem recalcular todo o histórico;
- falha isolada de escrita mantém o contato pendente, sem cancelar a carga;
- nenhuma credencial é incorporada ao executável.


## Atalho

O executável/atalho **NexoFlow Weni** usa o ícone exclusivo `assets\NexoFlow_Weni.ico`.

## Uso pelo NexoFlow Terminal

No NexoFlow 1.0, as rotinas desta documentação também podem ser chamadas por comandos amigáveis:

```text
nexo weni testar
nexo weni dry-run
nexo weni atualizar
nexo weni carga-completa
```

Para investigar um contato sem alterar a Weni:

```text
nexo api weni contato --uuid <UUID>
nexo api weni mensagens --uuid <UUID> --limite 10
nexo api weni grupos
nexo api weni campos
```

As consultas `nexo api` são somente leitura. Consulte `docs/README TERMINAL.md` para a referência completa.

## Recompra FarmaLíder D+25

Grupo sincronizado com a última compra confirmada entre 0 e 25 dias, remoção depois de 25 dias e por recusa. O envio continua somente no D+25 pela campanha Weni. Execute `nexo weni recompra auditoria`, `dry-run` e `aplicar` com revisão, ou `nexo weni recompra atualizar`. Confira [implementação, configuração e limites da recompra](README%20RECOMPRA.md). A rotina não altera marketing_opt_in, não dispara WhatsApp e não modifica a campanha.

## NexoFlow Planilha

O Setup instala **NexoFlow Planilha.exe** com ícone próprio e atalho na Área de Trabalho. O menu permite executar as cargas por planilha para Weni ou Analytics/Google Sheets e abrir `importacoes`. Use `Conversas_Delivery.xlsx` e `Contatos_Delivery.xlsx`; cada carga mantém a revisão humana antes de aplicar.

Na carga por planilha, UUIDs idênticos repetidos pela paginação são consolidados. Versões diferentes exigem GET individual por UUID; o vínculo UUID/URN é revalidado e falha da releitura não autoriza gravação. Mudanças no nome exibido não impedem esse vínculo; a validação do cliente na API segue a regra de CPF válido ou inválido. O log mostra contagens agregadas, sem CPF.

### Retomar uma aplicação por planilha

Se a aplicação falhar, preserve `logs/delivery_weni_review.json` e o relatório já revisado. No NexoFlow Terminal, após atualizar o Setup, retome com:

```text
python app/run_delivery_tickets.py --apply --target weni --require-review logs/delivery_weni_review.json
```

A retomada relê os contatos, preserva o pedido histórico congelado e não regrava valores já aplicados. Não gere outro dry-run para retomar uma aplicação parcial sem antes conferir o snapshot. A carga por planilha usa `pedido_data`; escritas legadas de `ultima_compra` são omitidas, com aviso, mantendo intacto o arquivo e seu hash. Outros campos ausentes bloqueiam antes de qualquer POST. Erros Weni informam releitura ou gravação e diagnóstico HTTP seguro, sem CPF, token ou corpo bruto da resposta.

### Janela da carga completa

Ao abrir `comandos/atualizar_weni_carga_completa.bat` por duplo clique, a sessão CMD permanece aberta após o término, inclusive diante de erro. Depois de conferir o resultado, digite `exit` para fechar. A revisão humana continua obrigatória e a aplicação aguarda o Python terminar. Uma janela de resultado só aparece depois da conclusão, cancelamento ou erro; fechar essa janela não autoriza outra aplicação.

`logs/weni_full_launcher.log` registra `APPLY_INICIADO`, `APPLY_RETORNOU` com o código do Python e o resultado final. Um início sem retorno significa ausência de confirmação do término; consulte também `logs/nexoflow_update_weni.log` antes de retomar. Preserve o snapshot revisado, principalmente em caso de aplicação parcial. No NexoFlow Terminal, use o snapshot 100% já revisado:

```text
python app/run_update_weni.py --apply --full --require-review logs/weni_update_review_full.json
```

Para uma parte, use `--quarter N` e o arquivo `logs/weni_update_review_full_25_parte_N.json` correspondente. Não gere um novo dry-run para retomar uma aplicação parcial sem antes conferir o snapshot e os logs.

## Deduplicação integrada

O relatório do dry-run também apresenta os contatos duplicados propostos para exclusão. A aplicação revalida a propriedade nas APIs, preserva os pedidos reais e as recusas e limpa as fontes do Sheets e do Looker. Falhas isoladas não cancelam a carga: os comprovantes permanecem para retomada na próxima incremental. Consulte [Deduplicação na rotina Weni](deduplicacao_weni.md).
