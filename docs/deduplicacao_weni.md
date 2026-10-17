# Deduplicação no NexoFlow Weni

A revisão de duplicados integra as cargas incrementais e completas do NexoFlow Weni. Use o executável ou os comandos habituais; não é necessário executar um script separado.

1. Execute o dry-run. O relatório da Weni inclui **DEDUPLICAÇÃO DE CONTATOS**, com os nomes e UUIDs a excluir e a manter, os pedidos comprovados e as pendências.
2. Revise o relatório e confirme a aplicação pela janela habitual.
3. A aplicação revalida a identidade nas APIs, interrompe os fluxos e exclui somente os duplicados comprovados. Limpa as fontes e confere o resultado. Falhas isoladas ficam pendentes; a atualização dos demais contatos continua.
4. Atualize os dados no Looker Studio para consultar as fontes corrigidas.

Antes da aplicação, encerre outras cargas de Analytics, Planilhas e Recompra. A rotina bloqueia outra execução simultânea da própria Weni; não inicia nem encerra os outros programas.

## Critérios

- O mesmo CPF matematicamente válido, inclusive após reposição comprovada de zeros à esquerda, deve identificar um único cliente na API.
- O telefone **nativo** da Weni deve identificar um único contato correto entre todos os contatos, incluindo o DDD. Campos copiados de telefone, nomes iguais, emojis e sobrenomes não decidem uma exclusão.
- Todos os pedidos referenciados pelo duplicado, inclusive nas planilhas e no histórico, precisam ter o mesmo proprietário no endpoint de pedido por ID. Essas consultas comprovam propriedade para a exclusão; não alteram status, produtos ou valores de pedidos antigos.
- Contatos sem pedido também podem ser deduplicados pelo CPF válido e pelo telefone nativo único.
- CPF inválido ou genérico, telefone ambíguo, pedido de outro cliente, histórico conflitante e falha de API permanecem pendentes. Compartilhar `0` não comprova duplicidade.
- Recusas de LGPD e marketing são preservadas no contato correto. Não há transferência automática de Opt-In. Bloqueio ou parada sem proteção equivalente no contato correto impede a exclusão.

Esses critérios para **apagar um contato** não alteram as regras para validar vendas: CPF válido continua dispensando comparação de nome, telefone e e-mail; CPF inválido mantém a validação por primeiro nome ou número local.

Nas cargas de 25%, a comparação considera a base global para identificar o contato correto, mas propõe excluir somente os duplicados pertencentes à parte selecionada. O contato correto pode estar em outra parte.

## Planilhas e retomada

Os pedidos e itens reais são vinculados ao contato correto, preservando seus IDs, produtos, status e valores. Registros dos contatos excluídos saem da Base, do histórico de alterações, dos tickets e eventos associados e das fontes BI. Apenas diagnósticos sem pedido, sem itens e sem qualquer valor financeiro podem ser descartados da tabela de pedidos.

A rotina descobre os arquivos de Operação, Histórico e dashboard pela configuração `NexoFlow_Arquivos`; também aceita instalações com um único arquivo. Não altera as fontes do Looker, os períodos dos indicadores nem os marcos incrementais do Analytics.

O snapshot complementar `*_duplicados.json` acompanha a revisão da Weni. Novas instalações exigem um novo dry-run: uma revisão antiga não autoriza exclusões.

Os comprovantes e backups ficam em `logs/duplicados/`, fora dos pacotes e do Git. Preserve-os durante uma recuperação. Lotes incompletos já revisados são retomados antes de novos lotes, sem repetir uma exclusão de contato confirmada. Resultados incertos de exclusões de linhas não são reenviados automaticamente: a rotina confere o estado e, se não conseguir comprová-lo, mantém apenas o lote pendente para nova conferência, sem cancelar a carga Weni. Não apague comprovantes ou arquivos de lock com uma execução ativa.

Sem duplicados comprovados, a etapa não grava alterações nas planilhas. Pendências aparecem no resumo e voltam à revisão nas próximas cargas.

## Falhas e próxima incremental

Falhas isoladas de consulta, preparação ou gravação não cancelam a carga Weni. Os contatos concluídos permanecem aplicados e o resumo informa as pendências. A próxima incremental revalida os contatos que falharam; quando necessário, recupera suas mensagens anteriores para não perder evidência após o avanço do checkpoint global.

Uma falha na deduplicação preserva seus comprovantes e não bloqueia a atualização normal dos demais contatos. Lotes incompletos são conferidos novamente na próxima execução. Nenhuma exclusão é presumida como concluída somente por ter sido enviada; a conferência do resultado continua obrigatória.

A opção legada `--require-complete` permanece aceita no comando Weni, mas falhas isoladas concluem com aviso e pendências em vez de cancelar a carga. Isso não modifica o comportamento dos comandos separados de Analytics e Recompra. Configuração inválida ou snapshot principal corrompido continua impedindo a aplicação de alterações não revisadas.
