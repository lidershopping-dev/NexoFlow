# NexoFlow 1.0 — Changelog

- Frete lançado como item de código 0 e descrição vazia é separado dos produtos quando corresponde ao frete oficial do cabeçalho, sem duplicar o faturamento ou bloquear pedidos válidos.

- Google Sheets: novas linhas gravadas em posições explícitas, sem deslocamento de colunas ou expansão automática de tabelas, com retentativas idempotentes e bloqueio de dados fora do cabeçalho.

- Weni, Analytics e Recompra: pedidos oficialmente sem itens são tratados como cancelados e ignorados nas vendas e produtos do dashboard, sem bloquear a carga. Falhas de consulta continuam sendo erros.

- Analytics: cancelamentos oficialmente verificados não bloqueiam a carga por falta de itens; diagnóstico de pedidos por motivo e verificação antecipada da sequência das partes.

- Arquivos organizados em `app`, `comandos`, `docs` e `configuracao`, com caminhos dos executáveis e atualização de instalações anteriores adaptados.

- Antes de classificar um CPF curto como inválido, restaurar zeros à esquerda somente quando o resultado de 11 dígitos passar nos verificadores. Valores como 0 e identificadores errados permanecem em validação por identidade.
- CPF válido dispensa comparação de nome, telefone e e-mail. CPF inválido pode ser validado pelo primeiro nome, normalizando acentos e símbolos, ou pelo número local do telefone, ignorando +55 e DDD. E-mail não é obrigatório; cache não compartilha identidade entre contatos e respostas ambíguas continuam pendentes.
- Analytics/Looker contabilizam pedidos API validados em estados documentados não cancelados, incluindo Em processamento; regra alinhada com Weni e Recompra e reconsulta em cada nova carga no escopo selecionado.
- Inserções no Sheets delimitam a tabela desde a coluna A para evitar deslocamento de pedidos e histórico.
- Analytics reconsulta por código os pedidos oficiais já observados no escopo da carga somente nos últimos 20 dias, incluindo concluídos e cancelados. Pedidos mais antigos permanecem preservados; a consulta por CPF continua descobrindo compras novas. Mudanças de status e itens atualizam o histórico; falhas preservam o snapshot anterior e o checkpoint.
- Orientações do Looker usam os indicadores de venda validada, incluindo os estados -2, -1 e 0; validação do primeiro nome também aceita símbolos internos sem resolver identidades ambíguas.
- Planilhas vincula conversas por UUID/URN, sem exigir igualdade do nome completo do canal. A validação do CPF e do pedido na API permanece obrigatória.
- Desinstalação remove os atalhos oficiais de todos os componentes, incluindo Recompra, preservando atalhos de outros programas.
- Ícone da Planilha redesenhado no padrão arredondado azul e ciano, com tamanho visual alinhado aos demais componentes.

## NexoFlow 1.0

- Ativação da divisão cria a aba de configuração e seus valores em um único batch atômico. Uma configuração vazia de uma interrupção anterior só pode ser reparada pela migração quando o registro privado comprova todas as cópias verificadas.

- Transferência nativa usa blocos menores e grava apenas os campos necessários, com repetição idempotente em timeouts. Progresso retomado separa linhas antigas da medição de velocidade e tempo restante.

- Migração do Sheets recupera erros internos da cópia integral de abas grandes com transferência nativa em blocos, preservando tipos de células, formatos e dimensões. Cada bloco confirmado registra a retomada; os originais só são retirados após nova comparação integral.

- Operação, histórico e dashboard podem usar arquivos separados, com descoberta automática pela planilha original. A migração copia abas nativas, confere o conteúdo em blocos e só retira os originais após a ativação e nova conferência. As fontes BI do Looker mantêm arquivo e IDs das abas.

- Sheets reserva exatamente as linhas necessárias antes de iniciar o upsert de cada tabela. Falta de capacidade nessa reserva interrompe a tabela antes de alterar registros existentes, com orientação para liberar espaço sem apagar dados automaticamente.

- Sheets recupera falhas de rede durante a autenticação Google, revalida a rota configurada e repete a operação dentro dos limites existentes. Recusas de autenticação e falhas de certificado continuam interrompendo a execução.

- Padronização de cidades, estados por extenso e bairros nas revisões da Weni, no Sheets e nas fontes do Looker. Referências locais corrigem grafias e distritos redundantes, mantendo conjuntos e subdivisões. Alterações de endereço respeitam a conferência de conflito antes da aplicação.
- Cidade `Pa` interpretada como Belém conforme confirmação operacional. Grafias de Manaus com diferenças de caixa, espaços nas extremidades ou caracteres invisíveis usam o mesmo nome oficial.

- Catálogo pet amplia a higienização por código de referência e nome exato, reconhecendo também abreviações verificadas da API. A atualização reprocessa o histórico e os indicadores do Sheets e exige nova revisão da Recompra; o catálogo original permanece fora dos pacotes públicos.

- Recompra e BI da farmácia excluem produtos pet, de uso animal e veterinários. Pedidos mistos mantêm apenas os itens humanos na mensagem e nos dados comerciais, com o valor desses itens sem frete. Cargas saneiam dados antigos, preservam contatos e proteções de CPF/LGPD, e exigem nova revisão da campanha. Snapshots antigos não classificam uma compra nova.

- Analytics informa barra, velocidade e tempo restante por etapa nas consultas de CPF, validação de pedidos e gravações do Sheets. O indicador continua ativo durante respostas lentas e retries; apenas linhas confirmadas avançam a gravação, e falhas não exibem conclusão de 100%.

- `comandos/atualizar_tudo.bat` executa as cargas completas, Planilhas, Recompra e sincronização incremental em ordem, mantendo as revisões humanas e interrompendo o fluxo diante de erros, conflitos ou consultas parciais. Looker Studio recebe orientação para atualização manual ao final.

- Compra posterior ainda não confirmada pela API pode ser sinalizada na Weni e no Sheets, com bloqueio da referência antiga de recompra e indicadores de pendência no BI.

- Revalidação de pedidos: a Recompra não recupera um pedido antigo quando o último pedido oficial está incompleto ou cancelado. Falhas de itens preservam os sinais oficiais de cancelamento e permanecem bloqueadas para venda e agendamento.
- Weni corrige data e status do mesmo pedido confirmado, mantendo a proteção contra pedidos diferentes mais antigos. Datas brasileiras com barras ou hífens e horários sem segundos são reconhecidas.
- Delivery reconhece a antiga tag Pedido nos exports com marca de exclusão e remove a referência anterior da Recompra antes de atualizar campos, sem repetir POSTs automaticamente.
- LGPD reconhece respostas compostas e continuidade inequívoca do atendimento, com contexto e evidência da mensagem do cliente. O aceite conjunto de maioridade e dados para entrega respeita a pergunta explícita; recusas e consentimento de Marketing permanecem separados.

- Auditoria geral: validações explícitas de datas e snapshots, tipos revisados, conexões fechadas e paginação validada. Escritas no Sheets reconciliam inclusões após timeout e preservam a tabela anterior até concluir os novos lotes.
- BI legado mantém números, datas e identificadores literais; migração financeira grava valores e formatos em uma única transação. Curva ABC sinaliza itens ausentes/divergentes e a conversão considera a base atual. Delivery atualiza também as fontes comerciais.
- CI bloqueia publicação quando lint, análise de tipos, auditoria de dependências ou testes Windows falham. A suíte Windows roda com e sem o modo UTF-8 do Python.

- Check-up com leitura UTF-8 explícita nos testes de pacotes, corrigindo a falha de acentuação no Windows. CI também valida esses testes sem forçar o modo UTF-8 do Python.

- Camada comercial para Looker com histórico acumulado, clientes por chave estável, itens com tipos numéricos, KPIs por período, Curva ABC e produtos comprados juntos. Migração do faturamento interpretado como data e campos opcionais de gênero/categoria sem inferência; cobertura histórica e ausência de anúncios sinalizadas.

- Weni com ícone azul e ciano, contato e balão de conversa, seguindo a estética do Analytics e do Recompra. Executável, imagem e atalhos atualizados.
- Recompra com ícone próprio de sacola e setas circulares; Planilha com ícone azul alinhado à identidade do NexoFlow. Executáveis e atalhos atualizados.
- CMD da Weni permanece aberto após a conclusão, erro ou cancelamento, inclusive quando iniciado por outro launcher. Fechar o aviso final ou pressionar ENTER não encerra a sessão; use EXIT para fechar.
- Testes usam configuração fictícia em memória e bloqueiam conexões reais; check-up, unittest e pytest independem de credenciais locais. Compatibilidade testada no Windows com Python 3.11, 3.13 e 3.14.
- Webhooks opcionais com comparação segura do segredo, validação explícita do schema e serialização das gravações entre threads do mesmo processo. Pedidos e itens reutilizam os snapshots dos upserts, eliminando duas releituras completas; métricas continuam reparáveis após falhas.
- Parser de pedidos separado em validações de itens e totais, com testes de caracterização. Tratamento de erros periféricos especifica falhas esperadas e mantém os bloqueios de aplicação, checkpoint e auditoria.
- Documentação Markdown na raiz como fonte única do HTML, com geração e verificação de integridade. Exemplos de proxy usam domínio fictício; metadados do projeto preservam NexoFlow 1.0 e Python >=3.11.
- Resumos da Recompra e da aplicação a cada 1.000 contatos, com resumo final inclusive para o último lote parcial.

- Progresso das consultas e atualizações Weni a cada 2 segundos: barra por etapa, contatos/registros por segundo, tempo decorrido e previsão de término. Auditoria JSONL informa bytes efetivamente gravados por segundo; conflitos e contatos já corretos também avançam o progresso.

- Requests atualizado para 2.33.0 ou superior, mantendo a compatibilidade com a série 2.
- Aviso de segurança simplificado: o NexoFlow não inclui credenciais; cada usuário deve configurar suas próprias chaves e tokens.

- Identificação e documentação padronizadas como NexoFlow 1.0, com listas dos executáveis Planilha e Recompra, índices de operação e instruções de assinatura e desinstalação alinhadas à distribuição atual. Atualizações seguem na main, com commits NexoFlow 1.0 e release v1.0 de nomes estáveis.
- Publicação automática bloqueada fora da main ou quando a mensagem do commit não corresponde exatamente a NexoFlow 1.0.

- Recompra identifica revisão expirada antes das gravações, renova o dry-run uma vez no launcher e exige confirmação do novo relatório. A data é conferida também durante a aplicação; operações já iniciadas preservam o estado e interrompem sem renovação automática.
- POSTs da recompra usam tentativa única, preservando intenção pendente após resultado incerto; GETs mantêm as retentativas existentes. Relatório gerado durante a virada do dia exige novo cálculo.

- Recompra processa contatos durante a paginação, consulta FarmaLíder com até quatro workers e cache limitado de payloads brutos por execução. CPFs válidos concluem antes dos inválidos; seleção de identidade permanece por contato.
- Aplicação da recompra pula planos sem trabalho antes de consultar as APIs, evita gravações locais redundantes e mantém revalidação, revisão e persistência de intenção/confirmação nas inclusões. Logs mostram velocidade e reutilizações de consultas.

- Carga completa por duplo clique mantém a sessão CMD aberta após o resultado; confirmação final independente de stdin e registro de início/retorno da aplicação em logs/weni_full_launcher.log.

- Carga Delivery/planilhas usa pedido_data e elimina escrita legada de ultima_compra, inclusive na retomada do snapshot sem alterar seu arquivo; preflight de campos antes de POST e diagnóstico seguro por etapa/HTTP.

- Cargas por planilha consolidam UUIDs repetidos idênticos na paginação Weni; versões divergentes exigem releitura individual, mantendo os bloqueios por identidade/URN ambígua.

- NexoFlow Planilha: executável nativo com ícone próprio, menu Weni/Analytics e atalhos instalados; reutiliza as cargas por planilhas e a revisão humana.

- Recompra FarmaLíder: referência exclusiva em `pedido_data`, sem dependência ou escrita de `ultima_compra`; grupo de 0 a 25 dias, envio D+25 pela Weni, recusas prioritárias, revisão obrigatória, nova validação por GET e ledger persistente por contato/pedido.
- Remoção preventiva do agendamento anterior antes de atualizar campos de pedido; integração opcional aos launchers e comandos próprios de auditoria, dry-run e aplicação.
- Diagnóstico da campanha/fluxo/fuso e bloqueio por campos obrigatórios ausentes, conflito, estado corrompido e inclusão indeterminada.

- Analytics exporta cidade, estado, CEP, bairro e idade do perfil Weni para a base e BI_Pedidos, preservando CEP como texto, ausência de informação e a data original do pedido.

- Cargas por planilha Weni e Analytics aguardam confirmação gráfica explícita, inclusive quando o terminal não fornece entrada padrão. Fechar o Bloco de Notas não encerra a espera. Cancelamento ou falha da janela impede a aplicação.

### Pasta de importações nos pacotes e na desinstalação

- `importacoes` vazia incluída no Setup e no projeto completo, sem exports privados;
- preservação obrigatória da pasta e seus arquivos em `C:\NexoFlow Data\importacoes`, independente da opção de manter configurações, credenciais e logs;
- validação SHA-256 antes de remover a instalação, versões conflitantes arquivadas em `historico_preservado` e restauração sem perda de arquivos por backups vazios.

### Carga por exports de conversas e contatos

- arquivos padronizados `importacoes/Conversas_Delivery.xlsx` e `importacoes/Contatos_Delivery.xlsx`, sem exigir token do Chats;
- comandos `nexo weni planilhas` e `nexo analytics planilhas`, com destinos separados, revisão congelada e aplicação idempotente;
- UUIDs de Chats/Flows cruzados por URN nativa única e nome, revalidados na Weni atual antes da consulta oficial;
- CPFs válidos primeiro; inválidos exigem nome + e-mail ou nome + telefone, preservando bloqueios de identidade;
- relatórios por grupo de CPF e motivo, emissão real dos pedidos e chave estável de atendimento compartilhada entre XLSX e API;
- campos de pedido herdados do contato não são considerados prova automática de atribuição ao atendimento;
- exports privados ignorados no Git, bloqueados no empacotamento e preservados pelo instalador.

### Atendimentos Delivery com tags de pedido

- leitura paginada da API externa v2 do Weni Chats, com autenticação própria do projeto e seleção de atendimentos humanos encerrados com Pedido ou Pedido finalizado;
- consulta por cliente, com prioridade para CPFs válidos e validação de identidade preservada nos inválidos;
- revisão congelada antes da atualização dos campos de pedido na Weni e publicação das informações oficiais no Google Sheets;
- fontes opcionais `Tickets_Delivery_Weni` e `BI_Delivery_Pedidos`, sem duplicar valores por conversa e com emissão real da API;
- existência de pedido do cliente e vínculo comprovado com o atendimento apresentados separadamente; último pedido não substitui histórico de pedidos;
- token de Chats ausente bloqueia a consulta dessas tags; nenhum pedido é atribuído a atendimentos por suposição.

### Resumo da checagem de CPFs inválidos

- resultados exibidos por lotes de até 500 contatos, com o último lote parcial e total da preparação;
- contagens separam pedido encontrado, pedido validado, divergência, identidade ambígua, dados insuficientes e outros impedimentos;
- avisos individuais de identidade ficam em DEBUG; totais permanecem no console, no log e no relatório de revisão;
- contagens incluem contatos sem alterações e pedidos cancelados; não representam vendas, pedidos únicos ou gravações realizadas;
- o resumo do dry-run é preservado ao aplicar o snapshot revisado, sem repetir consultas.

### Identidade de clientes com CPF inválido

- nome completo e e-mail, ou nome completo e telefone, são obrigatórios para confirmar o cliente;
- respostas com múltiplos candidatos são selecionadas por contato Weni, preservando o pedido e os itens do mesmo cliente;
- falta de dados, divergência ou ambiguidade bloqueiam o pedido, inclusive no fallback e no cache;
- revisão humana bloqueia mudanças relevantes na identidade após o dry-run;
- pedidos antigos confirmados somente pelo CPF inválido ficam fora dos indicadores até nova validação, preservando os dados de origem.

### Consulta de CPFs na FarmaLíder

- CPFs válidos são consultados primeiro; inválidos preenchidos, inclusive zeros e números curtos, entram depois;
- consulta preserva o identificador e mantém a validade matemática separada do cliente encontrado;
- HTTP 400, 404 e 500 têm diagnósticos distintos; proteção de identidade e revisão humana permanecem obrigatórias;
- consulta manual no terminal e diagnóstico privado aceitam também identificadores inválidos.

### Português brasileiro

- acentos e cedilha corrigidos na revisão humana, nos scripts, no instalador e nos launchers;
- CMD seleciona UTF-8 e PowerShell usa UTF-8 com BOM;
- HTML declara pt-BR e os pacotes recompilam executáveis com metadados pt-BR;
- teste no Windows verifica os caracteres recebidos pela janela de confirmação.

### Revisão humana da carga completa Weni

- confirmação síncrona mantém o CMD aguardando a revisão do relatório, tanto na carga de 100% quanto nas quatro partes;
- cancelamento ou falha da confirmação não inicia o apply e mantém o diagnóstico visível antes de encerrar;
- componente PowerShell de confirmação incluído e validado nos dois ZIPs e na instalação;
- testes Windows exercitam o fluxo do CMD com dependências simuladas, sem gravações na Weni.

### Carga completa 25%

- Weni e Analytics podem ser executados em quatro cargas completas de 25%, além do modo tradicional de 100%;
- a Parte 1 congela a lista de contatos em um manifesto SHA-256 compartilhado, e as Partes 2–4 reutilizam a mesma base;
- a divisão é determinística por UUID, sem sobreposição, e exige execução em ordem por componente;
- partes com erro não são marcadas como concluídas e podem ser repetidas com segurança;
- o cursor incremental só avança quando a Parte 4 do componente termina, usando o corte inicial da campanha para não perder mudanças ocorridas durante as quatro execuções.

### Status de pedido e vendas

- de-para atualizado: `EmAberto=-2`, `PendenteDePagamento=-1`, `EmProcessamento=0`, `ProntoParaRota=4`, `EmTransito=1`, `Concluido=2` e `Cancelado=3`;
- pedidos validados pela API em 4, 1 ou 2 contam como venda e atualizam `ultima_compra`; cancelamento ou pagamento 0 prevalece;
- estados -2, -1, 0, 4 e 1 permanecem em rechecagem; o alias legado `PendentePagamento` continua aceito.

### Diagnóstico privado dos CPFs com HTTP 500

- arquivo JSONL privado por execução identifica CPFs com falhas 5xx persistentes e correlaciona as referências do log;
- registro inclui horário de Belém, último HTTP 5xx, tentativas, motivo de encerramento e campos sanitizados da resposta;
- captura também consultas interrompidas pelo circuito enquanto aguardam nova tentativa;
- consultas recuperadas e testes com CPF fictício não criam registros privados de falha;
- Windows usa a pasta privada do usuário em LocalAppData, separada da instalação;
- logs normais mantêm referências anônimas; CPF e corpo diagnóstico não são impressos;
- Git e build protegem os registros contra publicação; falha de disco não substitui o erro original da API.

### Continuidade após troca de rede

- cargas longas agora redetectam conectividade após falha recuperável e alternam automaticamente entre internet direta e proxy corporativo;
- Weni repete a requisição/página em andamento após a troca;
- FarmaLíder atualiza também a URL operacional, alternando porta externa `:3200` e rota via proxy sem `:3200`, e repete o mesmo CPF/pedido;
- Google Sheets redetecta a rota antes dos retries de leitura e escrita;
- a redetecção é serializada entre workers e mantém o progresso já concluído pela execução.

### Diagnóstico de rede e proxy

- diagnóstico sanitizado da Weni distingue HTTP 407/403 no proxy, certificado TLS, DNS, timeout e conexão recusada, incluindo exceções aninhadas;
- causas explícitas não transitórias encerram o retry da Weni sem repetir a mesma falha;
- diagnóstico mostra a rota efetiva da Weni, respeitando exceções de proxy;
- logs do diagnóstico usam America/Belem (UTC-3) e persistência local;
- avisos de rede do Google Sheets incluem categoria segura, sem texto bruto da exceção;
- HTTP 400/404 no CPF fictício da FarmaLíder é apresentado como resposta de rede, com autenticação não confirmada;
- diagnóstico não desativa TLS nem altera automaticamente a rota escolhida.
- preflight da FarmaLíder agora repete timeout/falha transitória, HTTP 429 e 5xx antes de interromper a carga; proxy 407/TLS inválido continuam falhando imediatamente com diagnóstico sanitizado.

### Auditoria dos campos de pedido

- listas separadas por vírgula usam aspas CSV quando o nome contém vírgula, aspas ou quebra de linha, preservando o nome completo e a correspondência com quantidade e valor;
- Weni e Analytics compartilham a mesma leitura das listas, incluindo nomes como `Produto 0,5 MG`;
- auditoria verifica também atualizações parciais das listas e informa as contagens sem expor produtos;
- pagamento 5 corresponde a **Pix por Link de Pagamento** e pagamento 6 a **Pix no POS**;
- pagamento 0 é tratado como cancelamento: Weni recebe `pedido_forma_pagamento=Cancelado` e `pedido_status=3`, sem venda nem atualização de `ultima_compra`;
- normalizadores aceitam os nomes oficiais dos enums de status e pagamento; o nome de pagamento `_` equivale a 0 e aplica o cancelamento;
- códigos numéricos de pagamento sem de-para a partir de 7 são preservados como Int32 não negativo na Weni e apresentados no Google Sheets/BI como `Não identificado (código X)`, sem inventar o de-para;
- pagamentos sem de-para geram `INFO` uma vez por código em cada execução, com totais por código no resumo;
- resumo distingue contatos com pedido validado pela API, inclusive sem alterações, de contatos com alterações; preparação e auditoria acontecem antes das gravações;
- listas realmente desalinhadas, CSV malformado, elementos vazios e códigos inválidos continuam bloqueados antes de qualquer gravação.

### Recuperação e diagnóstico da FarmaLíder

- pausas globais de 5, 10 e 20 segundos após falhas 5xx, inclusive proteção após a última tentativa;
- recuperação com uma requisição por vez e retorno gradual para duas e quatro;
- intervalo adaptativo reduzido após respostas saudáveis, evitando manter toda a carga em ritmo lento;
- circuito por rota interrompe falhas persistentes em várias consultas sem respostas válidas;
- diagnóstico por referência HMAC, categoria segura da resposta, latência, limite e quantidade em voo;
- identificação de rastreamento do servidor registrada somente quando é um UUID válido;
- horários de log sempre em America/Belem (UTC-3);
- falhas individuais preservam dados de cliente anteriores, registram `ERRO_API` e evitam GET duplicado por CPF;
- execução parcial retorna código 3 e mantém o marco incremental para nova tentativa;
- empacotamento bloqueia arquivos privados rastreados antes da publicação.

### Setup

- Setup validado em Windows com primeira instalação e atualização real;
- atualização com rollback automático para a instalação anterior em caso de falha;
- elevação UAC automática quando necessária;
- modo não interativo para smoke tests e automação;
- `NexoFlow Uninstall.exe` realmente opcional inclusive na validação de integridade do payload;
- cenário de antivírus/EDR removendo o Uninstall opcional coberto por smoke test;
- preservação de `secrets_config.py`, `credentials` e `logs` verificada em atualização;
- publicação do NexoFlow 1.0 bloqueada se testes, auditoria, build ou smoke test falharem.


### Pedidos e status na Weni

- a FarmaLíder API é a fonte de verdade dos pedidos; `pedido_id` e `ultimo_pedido` da Weni são localizadores para recuperação;
- novo campo `pedido_status` é preenchido pelo NexoFlow Weni após consultar o mesmo `pedido_id` na FarmaLíder API;
- valores atuais de `pedido_status`: **-2 Em aberto**, **-1 Pendente de pagamento**, **0 Em processamento**, **4 Pronto para rota**, **1 Em trânsito**, **2 Concluído** e **3 Cancelado**;
- cancelamento explícito ou pagamento 0 prevalece e grava o status efetivo **3, Cancelado** na Weni/BI;
- `Pedidos_FarmaLider` preserva os valores brutos de status e pagamento retornados pela API para auditoria;
- status efetivo 4 (Pronto para rota), 1 (Em trânsito) ou 2 (Concluído), validado pela API e sem cancelamento entra nos indicadores de venda e atualiza `ultima_compra`;
- `pedido_status` também é preservado na `Base_Contatos` para Google Sheets/Looker Studio;
- pedidos em **Em aberto (-2)**, **Pendente de pagamento (-1)**, **Em processamento (0)**, **Pronto para rota (4)** ou **Em trânsito (1)** ficam numa fila persistente de rechecagem até chegarem a **Concluído** ou **Cancelado**, mesmo sem nova mensagem na Weni.

Data de consolidação: 05/10/2026.

Esta documentação descreve exclusivamente o **NexoFlow 1.0**.

### Analytics e BI

- sincronização Weni → FarmaLíder → Google Sheets com carga completa e incremental;
- preservação de todos os contatos retornados pela Weni na `Base_Contatos`;
- validação e enriquecimento de CPF/cliente pela API FarmaLíder;
- processamento de pedidos e itens confirmados;
- camada BI mantida pelo Analytics com `BI_Resumo_Diario`, `BI_Canais`, `BI_Conversao`, `BI_Vendas`, `BI_Ads` e `BI_Status`;
- batches adaptativos no Google Sheets, redução de leituras/escritas redundantes, retry/backoff e progresso operacional;
- cache seguro da FarmaLíder no incremental e revalidação integral na carga completa.

### NexoFlow Weni

- atualização auditada de campos de contato diretamente pela API Weni;
- identificação multicanal por WhatsApp, Instagram, Facebook/Messenger e outras URNs nativas;
- captura e validação de CPF, idade, maioridade e LGPD com contexto;
- sincronização de grupos Opt-In/Opt-Out LGPD;
- confirmação de pedido e última compra via FarmaLíder antes da escrita transacional;
- atribuição conservadora de Meta/Google Ads com first touch e last touch;
- carga completa com dry-run, revisão humana, snapshot congelado, SHA-256, releitura por contato, conflitos isolados e checkpoint seguro.

### Operação e distribuição

- `NexoFlow Analytics.exe`, `NexoFlow Weni.exe` e `NexoFlow Terminal.exe`;
- CLI `nexo` para status, diagnóstico, rotinas Weni, Analytics, APIs somente leitura e logs;
- Setup com validação SHA-256 do payload, Python 3.11+ x64, `.venv`, preservação de dados privados e atalhos Windows;
- desinstalação via `installer\uninstall.ps1` com wrapper `desinstalar_nexoflow.cmd`;
- `NexoFlow Uninstall.exe` incluído como launcher opcional, sem se tornar requisito do Setup ou do check-up; em ambientes onde antivírus/EDR o bloqueiem, a instalação e a desinstalação por script continuam funcionando normalmente;
- launchers nativos sem `system()`, sem packers/ofuscação e sem credenciais embutidas;
- check-up, testes automatizados e auditoria de consistência.

### Identidade do NexoFlow 1.0

- versão pública e lógica: **NexoFlow 1.0**;
- metadados de instalação e comandos `nexo versao`/`nexo status`: **1.0**;
- recursos de versão dos executáveis Windows: **1.0.0.0** (representação de quatro componentes exigida pelo formato VERSIONINFO do Windows).
