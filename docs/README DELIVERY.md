# NexoFlow 1.0

## Atendimentos Delivery com as tags Pedido e Pedido finalizado

A tag legada `Pedido is deleted` também é aceita como `Pedido`, por correspondência exata, desconsiderando maiúsculas e espaços duplicados. O formato de export `Pedido_is_deleted_<data e hora com fuso>` é aceito somente para a antiga tag Pedido, com timestamp válido. Ela seleciona atendimentos; não significa cancelamento do pedido na FarmaLíder.

O comando `app/run_delivery_tickets.py` reconcilia os clientes dos atendimentos humanos encerrados com essas tags. As tags são lidas na **API externa v2 do Weni Chats**, não nos tópicos/setores ou nas etiquetas de mensagens da API de Flows.

### Carga por planilhas — Weni e Analytics

Coloque os dois exports originais na pasta `C:\NexoFlow\importacoes`, com estes nomes:

| Arquivo | Export e estrutura original |
| --- | --- |
| `Conversas_Delivery.xlsx` | Conversas encerradas, aba `rooms`, com as tags Pedido, Pedido Finalizado ou a tag legada Pedido excluída. |
| `Contatos_Delivery.xlsx` | Contatos da mesma organização, abas `Contact` ou `Contact 1`, `Contact 2` etc., com UUID, nome, CPF, e-mail e URNs nativas. |

Renomeie apenas os arquivos. Preserve abas, cabeçalhos e dados originais. Os exports ficam fora do Git e dos pacotes; o Setup e o projeto completo incluem a pasta `importacoes` vazia. Na desinstalação, a pasta e seus arquivos são sempre preservados em `C:\NexoFlow Data\importacoes`, inclusive se você optar por remover configurações, credenciais e logs. O Setup restaura as importações na reinstalação. Cópias anteriores com o mesmo nome e conteúdo diferente ficam em `historico_preservado`; uma cópia vazia não apaga os arquivos preservados.

No NexoFlow Terminal:

```text
nexo weni planilhas
nexo analytics planilhas
```

O primeiro comando consulta a FarmaLíder, aplica os campos de pedido na Weni e remove uma referência antiga de Recompra quando necessário e revisado. O segundo consulta novamente a FarmaLíder e publica os contatos/pedidos/itens e indicadores no Google Sheets, sem gravar na Weni. Recomenda-se executar Weni primeiro e Analytics depois. Uma nova carga prepara um snapshot próprio e abre o relatório no Bloco de Notas; digite `PLANILHAS REVISADAS` na janela de confirmação somente depois de conferir os resultados. Cancelar, fechar a janela ou falhar na consulta/auditoria impede a aplicação. Fechar o Bloco de Notas não confirma a carga.

Se já existe uma revisão para o destino escolhido, o menu oferece **Retomar a revisão existente**, **Gerar nova revisão** ou **Cancelar**. Retomar reutiliza o mesmo JSON, abre novamente o relatório e exige confirmação humana. A aplicação relê os contatos e executa somente as operações ainda pendentes; alterações posteriores na Weni continuam protegidas. Gerar nova revisão preserva os arquivos anteriores, sem mudar seu conteúdo, em `logs/delivery_historico`. Se a consulta falhar, a revisão anterior permanece disponível no caminho original.

Um conflito isolado produz **aviso de carga parcial**, com código de saída 3, preservando os contatos aceitos e impedindo o avanço automático para as próximas etapas do fluxo completo. Consulte o JSON privado `logs/privado/delivery_weni_review_conflitos.json` ou o equivalente de Analytics: ele identifica UUID, motivo, campos divergentes e impressões digitais dos valores, sem copiar CPF, nome, telefone ou conteúdo das conversas. O console mostra somente uma referência protegida por contato e os totais. Retome o mesmo snapshot para conferir o estado atual; se a divergência persistir, gere e revise um novo snapshot antes de autorizar os novos dados.

Essa modalidade não precisa do token `project_auth` do Chats. Continua exigindo token de contatos da Weni, token FarmaLíder e, para Analytics, credencial/ID da planilha Google. O importador cruza o UUID interno do Chats com o UUID de contatos/Flows por identificador ou URN nativa única; não usa nome ou CPF sozinho para unir contatos. Mudanças no nome exibido, inclusive emojis, não impedem esse vínculo. URNs ambíguas, datas inválidas, conversas abertas, ausência de atendente e setores diferentes são excluídos com motivo.

Antes da consulta de pedidos, o vínculo UUID/URN é revalidado na base atual da Weni. CPF, nome, e-mail e telefone usados na FarmaLíder vêm desse contato atual, evitando atualizar a partir de dados pessoais desatualizados no export. Consultas priorizam CPFs válidos, depois inválidos preenchidos não zerados. Valores ausentes, inteiramente zerados ou malformados ficam como pendência cadastral, sem consulta por CPF. CPF válido dispensa comparação de nome, e-mail e telefone; CPF inválido preenchido exige primeiro nome correspondente ou número local do telefone correspondente do mesmo cliente da API, sem ambiguidade. Quando o CPF está ausente, somente os localizadores oficiais já existentes no contato podem ser revalidados. CPF zerado ou malformado bloqueia o reaproveitamento desses localizadores até a correção cadastral; não há endpoint documentado de busca por nome/e-mail/telefone.

Logs e revisão apresentam totais por grupo de CPF, pedidos validados e impedimentos, sem exibir dados pessoais no console. Os relatórios privados são `logs/delivery_weni_review.txt` e `logs/delivery_analytics_review.txt`. Falha de API não significa cliente sem pedido. A carga mantém a revisão congelada e não avança o checkpoint da carga geral.

As datas sem fuso do export de conversas são interpretadas em `America/Belem`, pois o export do Chats retira o fuso após converter para o horário do projeto. As vendas continuam usando a emissão real da API. O campo `custom_fields.pedido_id` do export pode ser um snapshot antigo do contato e **não comprova vínculo com a conversa**. Por isso, a modalidade por planilhas não o usa como prova de atribuição.

Um identificador local estável de atendimento é gerado a partir do UUID interno do contato do Chats e das datas de abertura/encerramento. Reimportar o mesmo atendimento não duplica sua linha; quando esses dados também estão presentes na API do Chats, as duas origens usam a mesma chave. `chats_room_uuid` guarda o UUID real da sala quando disponível; fica vazio no export sem essa coluna.

Para execução técnica sem o assistente do Terminal:

```powershell
python app/run_delivery_tickets.py --dry-run --planilhas --target weni --review-out logs/delivery_weni_review.json
python app/run_delivery_tickets.py --apply --target weni --require-review logs/delivery_weni_review.json
python app/run_delivery_tickets.py --dry-run --planilhas --target analytics --review-out logs/delivery_analytics_review.json
python app/run_delivery_tickets.py --apply --target analytics --require-review logs/delivery_analytics_review.json
```

O destino fica congelado na revisão. Não é permitido usar uma revisão de Weni para gravar no Analytics ou vice-versa. `--import-dir` permite selecionar outra pasta mantendo os mesmos nomes de arquivos. Uma exportação contendo somente Pedido Finalizado não recupera atendimentos marcados apenas com Pedido.

Um novo `--dry-run` não sobrescreve uma revisão existente sem `--archive-review`. Para preparar outra revisão preservando a anterior:

```powershell
python app/run_delivery_tickets.py --dry-run --planilhas --target weni --review-out logs/delivery_weni_review.json --archive-review
```

Para retomar, use o comando `--apply --require-review` com o JSON original, sem executar outro dry-run nem editar seus campos ou hashes.

### Configuração local

Preencha em `secrets_config.py`, que permanece fora do Git e dos pacotes:

```python
WENI_CHATS_API_BASE_URL = "https://chats-engine.weni.ai"
WENI_CHATS_API_TOKEN = "TOKEN_DE_INTEGRACAO_DO_PROJETO"
WENI_CHATS_ORDER_ID_FIELD = ""
```

O token de integração do Chats é um Bearer UUID do projeto **Delivery FarmaLíder**. Ele é diferente do `WENI_API_TOKEN` usado para contatos e mensagens. A origem deve ser a API, não a página do navegador, sem caminhos, parâmetros ou credenciais. O token restringe as consultas ao projeto: confirme essa organização antes de configurar. O comando lê `GET /v2/external/rooms_metrics/`, todas as páginas de atendimentos encerrados até o corte da execução, sem janela inicial de 90 dias. Redirecionamentos e paginação para outro destino são bloqueados.

**Como obter os dados:** no código oficial da Weni, o token de integração é chamado `project_auth` e fica na configuração do serviço de atendimento (`ticketer`) que conecta Chats e Flows. A API pública `/api/v2/ticketers.json` mostra UUID, nome, tipo e data de criação; não entrega esse segredo. Solicite ao administrador da integração ou ao suporte da Weni/VTEX o `project_auth` do projeto Delivery FarmaLíder e a origem HTTPS da API do Chats, com acesso de leitura a `GET /v2/external/rooms_metrics/`. Se a configuração administrativa já mostrar esse valor, copie-o diretamente para o arquivo local, sem publicar no GitHub.

Para identificar a origem da API, abra Atendimento/Chats no navegador, pressione F12, entre em Rede/Network e observe a URL de uma requisição de salas/atendimentos ao Chats. Use somente a origem (`https://host`), sem caminho ou parâmetros. O Bearer da sessão do navegador pode ser um JWT temporário de login; ele não substitui o UUID de integração exigido pela API externa.

Referências oficiais: [configuração `project_auth` da integração com Flows](https://github.com/weni-ai/chats-engine/blob/main/chats/apps/projects/usecases/integrate_ticketers.py) e [autenticação do projeto na API externa](https://github.com/weni-ai/chats-engine/blob/main/chats/apps/accounts/authentication/drf/authorization.py). O índice de API v2 de Flows enviado no chat não contém instruções para obter o token de Chats.

A API externa também documenta o filtro `tag_name`, por exemplo `GET /v2/external/rooms_metrics/?is_active=false&tag_name=Pedido`. Para a segunda tag, o valor é `Pedido%20finalizado`, respeitando o nome cadastrado. O NexoFlow lê todas as páginas encerradas e compara as tags e seus aliases legados localmente, para lidar com capitalização, tickets com múltiplas tags e diferenças de espaços sem duplicação.

O campo opcional `WENI_CHATS_ORDER_ID_FIELD` identifica um campo comprovadamente específico do ticket que contém o ID oficial do pedido. Deixe vazio até documentar esse vínculo. Não use automaticamente `pedido_id` herdado do contato: ele pode representar uma compra anterior. Sem evidência específica, o vínculo do pedido com o atendimento continua pendente.

### Preparação e revisão

No terminal do projeto:

```powershell
python app/run_delivery_tickets.py --dry-run
```

Revise `logs/delivery_review.txt`. O JSON correspondente contém o plano congelado, os pedidos, itens, tickets e hashes; ambos são privados. Nenhuma alteração na Weni ou no Google Sheets ocorre nessa etapa. O log exibe totais, não nomes, CPFs ou conteúdo de conversas.

Depois de revisar:

```powershell
python app/run_delivery_tickets.py --apply --require-review logs/delivery_review.json
```

O apply relê cada contato e bloqueia alterações nas informações usadas para identificar o cliente ou validar o pedido. Contatos em conflito não são gravados nem publicados como reconciliação bem-sucedida. Cada POST tem tentativa única; se uma integração falhar depois de outras gravações, retome com o mesmo snapshot, que verifica o estado atual antes de executar operações pendentes. Esse comando não altera o checkpoint da carga geral nem o consentimento LGPD. Ao gravar na Weni, remove primeiro a associação revisada ao grupo Recompra FarmaLíder quando a referência mudou ou ficou inelegível, para cancelar o evento antigo. Não inclui clientes no grupo nem dispara mensagens; a nova admissão exige a rotina própria de Recompra.

### Validação e limitações

- Seleciona exatamente as tags Pedido, Pedido is deleted (legada) e Pedido finalizado, desconsiderando maiúsculas e espaços duplicados. Exige encerramento, evidência de atendente e `contact.external_id` válido para o UUID de contato da Weni. IDs internos de contato do Chats não substituem esse vínculo.
- Deduplica tickets e clientes; consulta primeiro CPFs válidos e depois os inválidos preenchidos não zerados. Valores ausentes, zerados ou malformados não geram consulta por CPF. CPF inválido preenchido mantém a exigência de primeiro nome correspondente ou número local do telefone correspondente, do mesmo cliente. Ambiguidade, divergência e dados insuficientes bloqueiam o pedido.
- Usa a lógica oficial de confirmação FarmaLíder já existente, preservando proprietário, itens, descrição, quantidades, preços, status, pagamento e emissão. Uma tag não confirma venda. HTTP 500 não significa ausência de cliente ou pedido.
- Atualiza os campos `pedido_*`, usando `pedido_data` como referência temporal. A carga Delivery não escreve `ultima_compra`. Não substitui um pedido mais recente na Weni por outro mais antigo. Vendas são todos os estados documentados não cancelados: -2, -1, 0, 4, 1 e 2. Status 3 ou pagamento 0 excluem a venda.
- `cliente/cpf` informa **somente o último pedido**. Quando o ticket contém um ID estruturado de outro pedido, o comando consulta `pedido/{id}` e exige o mesmo cliente identificado nesta execução. O campo mais recente da Weni é preservado; o pedido histórico entra na planilha. Sem ID, a API não recupera automaticamente todos os pedidos históricos dos tickets. O comando não interpreta a última compra como compra gerada naquele atendimento.
- `CONFIRMADO_POR_ID`: o ID estruturado do ticket corresponde ao pedido confirmado pela API para o cliente. `COMPATIVEL_COM_PERIODO_SEM_ID`: emissão dentro do atendimento, apenas indício. `FORA_DO_PERIODO_DO_ATENDIMENTO` ou `OUTRO_PEDIDO_NO_TICKET`: o último pedido não comprova aquela compra. Sem ID e sem histórico consultável, registre a pendência.

### Google Sheets e Looker Studio

Após o apply, pedidos e itens entram em `Pedidos_FarmaLider` e `Itens_Pedidos_FarmaLider`; contatos entram em `Base_Contatos`. `BI_Vendas` e `BI_Pedidos` são reconstruídas preservando os demais pedidos históricos, com a data real de emissão da API. Datas operacionais usam `America/Belem` (UTC-3).

As métricas de vendas são recalculadas; o resumo e a conversão recebem um snapshot da base no corte revisado. Esses snapshots continuam distintos das datas de emissão: gráficos de vendas por período devem usar `BI_Vendas`, `BI_Pedidos` ou a fonte de pedidos de Delivery, não totais acumulados do resumo.

Duas fontes adicionais são criadas apenas ao aplicar essa reconciliação:

| Aba | Granularidade e uso |
| --- | --- |
| `Tickets_Delivery_Weni` | Uma linha por ticket, com tags, datas do atendimento, resultado da checagem e vínculo do pedido. Permite contar atendimentos marcados, pedidos encontrados e pendências. |
| `BI_Delivery_Pedidos` | Uma linha por pedido único confirmado de clientes atendidos com essas tags, com ID de cliente, data real de emissão, valor, status e indicador de venda. Não replica o valor por ticket. |

No Looker, conecte as novas abas na mesma planilha. Para **vendas vinculadas aos atendimentos**, filtre `venda_realizada = 1` e `vinculo_atendimento = CONFIRMADO_POR_ID`; use a dimensão de período `data`, some `pedido_valor` e conte pedidos distintos. Compradores do período são `COUNT_DISTINCT(cliente_id)`, sem somar compradores diários. Sem o filtro de vínculo, o título deve dizer “Pedidos confirmados de clientes com atendimentos marcados”, pois inclui pedidos cuja origem no atendimento não foi comprovada.

Após criar essas fontes, a carga normal do Analytics atualiza a BI de Delivery com os status e valores atuais dos pedidos já armazenados. Para buscar **novos tickets e alterações de tags**, repita a reconciliação Delivery com novo dry-run e revisão; a carga normal não lê as tags automaticamente.

Para indicadores de atendimento, use `encerrado_em` na fonte de tickets. Não use essa data para faturamento. Configure os campos ISO como Data/Data e hora conforme o conteúdo e exiba pt-BR. Acrescente cards de tickets sem pedido validado, divergência de identidade e vínculo pendente. Não some valores monetários da aba de tickets, pois um pedido pode aparecer em várias conversas.

O comando atualiza as fontes do Google Sheets. A conexão e configuração dos gráficos no Looker são feitas no editor do relatório; ele não altera permissões de compartilhamento.

Referência de schema e autenticação: [Weni Chats Engine, API externa v2](https://github.com/weni-ai/chats-engine/tree/main/chats/apps/api/v2/external/rooms).

A revisão das cargas Weni e Analytics por planilha aguarda uma janela separada de confirmação. Após conferir o relatório, digite `PLANILHAS REVISADAS` nessa janela. Fechar o Bloco de Notas não confirma nem cancela a carga. Cancelar a janela não aplica dados; uma falha ao abrir a confirmação também bloqueia a aplicação.
