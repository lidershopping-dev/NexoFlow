# NexoFlow 1.0: Curva ABC por período

## Estado

Conector privado ativo na página Curva ABC. As tabelas de produtos, resumo e compras conjuntas e os seis cartões usam o período recebido do Looker. As abas existentes e a rotina Python continuam intactas.

## Registro de validação e pendências (17/10/2026)

- 11 testes do cálculo JavaScript aprovados, incluindo limites inclusivos, empates, reconciliação, pares, intervalos vazios e campos nativos dos cartões.
- 29 testes existentes de `test_looker_bi.py` aprovados com configuração fictícia isolada.
- Autorização Google concluída pelo titular. Adaptador usa o serviço avançado Sheets v4 com escopo somente leitura.
- Funcionamento validado no relatório real em dois intervalos, um único dia e um intervalo sem dados. Classes, receita, produtos, Top 5 e pares respondem às datas escolhidas.
- Componentes legados com referências calculadas incompatíveis foram substituídos por campos nativos. Período padrão restaurado após os testes.
- Manutenção: acompanhar o limite explícito de linhas e revalidar depois de mudanças de esquema ou carga BI. Não há amostragem silenciosa.
- O PDF é uma fotografia do período de exportação. Filtros interativos permanecem no relatório online.

## Como funciona

O conector usa Google Apps Script, solicita `dateRangeRequired` e recebe `request.dateRange.startDate/endDate` em cada consulta. Lê somente `BI_Itens` e `BI_Clientes_Vendas`, filtra as vendas no intervalo inclusivo, agrupa por código de produto, ordena por receita e só então calcula participação, acumulado, classes e pares. A tabela `BI_Produtos_ABC` antiga não é modificada.

Preserva a regra de `app/looker_bi.py`: o produto que cruza 80% permanece na classe A; o que cruza 95%, na B. Empates de receita recebem a mesma classe, com desempate de apresentação pelo código. Portanto, os totais não precisam ser exatamente 80/15/5.

Não retorna CPF, cliente, contato ou número original de pedido. Não usa serviços de terceiros, chave de API, endpoint público, nem escrita nas planilhas. O OAuth solicita somente leitura do Google Sheets; a autorização de acesso é feita pelo usuário no Google. Nenhum compartilhamento do relatório ou da planilha precisa mudar.

## Instalação privada

1. No Google Apps Script da conta dona do relatório, crie um projeto com o nome **NexoFlow 1.0 - Curva ABC**.
2. Copie `AbcCore.js` e `Code.js` para arquivos de script com os mesmos nomes (o editor pode mostrá-los como `.gs`). Habilite a exibição de `appsscript.json` e copie o manifesto.
3. Salve. Em **Implantar > Testar implantações**, selecione o tipo conector do Looker/Data Studio. Use o ID de implantação fornecido pelo Google, sem tornar o projeto público.
4. Autorize a leitura no Google e crie três fontes privadas a partir da mesma implantação. Informe o ID da planilha BI e escolha `abc`, `resumo` e `juntos`, respectivamente. Mantenha o ID fora do repositório.
5. Somente após testar a resposta do conector, use `abc` na tabela detalhada, `resumo` no resumo e nos seis cartões, e `juntos` na tabela de compras conjuntas. Mantenha o intervalo automático dos componentes. Não substitua fontes do relatório inteiro.
6. Prefira componentes novos com campos nativos: referências calculadas de fontes antigas podem permanecer inválidas mesmo após trocar a fonte. Preserve o estilo e os limites dos componentes ao substituí-los.
7. Use `periodo_rotulo` para o período exibido. Os campos `*_pct` continuam em escala 0 a 100; os novos campos `participacao_receita`, `participacao_acumulada`, `participacao_top5_receita` e `receita_classe_*` usam frações 0 a 1 com tipo percentual. Não formatar o número 80 como 8.000%.
8. Os cartões usam `produtos_classe_a/b/c` e `receita_classe_a/b/c`. Sem dados completos, as classes e percentuais ficam indisponíveis. Não aplique um filtro de classe ao resumo global nem reduza a tabela antes de calcular o acumulado.
9. Ative o serviço avançado **Google Sheets API v4** com símbolo `Sheets`, conforme o manifesto. `SpreadsheetApp.openById` exige escopo mais amplo e não deve substituir essa leitura.

## Critérios de validação antes de considerar concluído

- Comparar receita e produtos com a página Produtos para dois intervalos diferentes e para um único dia.
- Conferir que o líder pode mudar, os percentuais são recalculados e os cards somam a quantidade total de produtos classificados e 100% da receita quando disponíveis.
- Conferir Top 5 sobre todos os produtos e pares por pedidos distintos.
- Testar um intervalo vazio e ausência/divergência de itens. Classes e Top 5 ficam indisponíveis em dados inconsistentes; não inventar classes ou percentuais.
- Confirmar desempenho com a base real. A versão lê até 150 mil linhas por aba e falha explicitamente se o limite for excedido; não entrega amostra silenciosa. Consulta com pedido acima de 500 SKUs também falha explicitamente no cálculo de pares.
- Exportar a página e conferir se o período, classes, cards, tabela e pares são coerentes. Retornar ao período padrão após os testes.

## Testes locais

```sh
node --test tests/test_looker_abc_connector.cjs
PYTHONPATH=app python3 -m unittest discover -s tests -p test_looker_bi.py
```

A suíte do conector usa somente dados fictícios. Não envia dados nem modifica Google Sheets. O resultado local não substitui a validação de OAuth, execução Apps Script e funcionamento no Looker com dados reais.

## Referências

- https://developers.google.com/looker-studio/connector/date-range
- https://developers.google.com/looker-studio/connector/reference
- https://developers.google.com/looker-studio/connector/manifest

## Uso privado e privacidade

Este componente destina-se ao uso interno, sob as permissões da conta que o instalar. O operador é responsável por autorizar apenas fontes e leitores permitidos. A instalação não estabelece oferta comercial, contrato de suporte ou política para terceiros. Não publicar em galeria sem revisão específica de identidade, termos e política de privacidade. O código não grava registros de negócio em logs nem transmite dados para fora dos serviços Google usados pelo próprio relatório.
