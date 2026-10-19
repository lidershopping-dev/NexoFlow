/* NexoFlow 1.0: cálculo puro, sem rede e sem escrita na planilha. */
var NexoAbc = (function () {
  'use strict';
  function fail(message) { throw new Error(message); }
  function day(value) {
    var s = String(value || '').trim(), m;
    if ((m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(s))) s = m[3] + '-' + m[2] + '-' + m[1];
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || new Date(s + 'T00:00:00Z').toISOString().slice(0, 10) !== s) fail('Data inválida na consulta ou na origem.');
    return s;
  }
  function amount(value) {
    if (value === '' || value === null || value === undefined || typeof value === 'boolean') fail('Valor monetário ausente na origem.');
    var n = Number(value);
    if (!Number.isFinite(n) || n < 0 || !Number.isSafeInteger(Math.round(n * 100))) fail('Valor monetário inválido na origem.');
    return Math.round(n * 100);
  }
  function rows(matrix, required) {
    if (!Array.isArray(matrix) || !matrix.length) fail('Cabeçalho da origem ausente.');
    var header = matrix[0], indexes = {};
    required.forEach(function (key) {
      if (header.filter(function (v) { return v === key; }).length !== 1) fail('Cabeçalho obrigatório ausente ou duplicado: ' + key);
      indexes[key] = header.indexOf(key);
    });
    return matrix.slice(1).filter(function (r) { return r.some(function (v) { return v !== ''; }); }).map(function (r) {
      var result = {};
      required.forEach(function (key) { result[key] = r[indexes[key]] === undefined ? '' : r[indexes[key]]; });
      return result;
    });
  }
  function unique(records, key) {
    var seen = new Map();
    records.forEach(function (r) {
      var id = String(r[key] || '').trim();
      if (!id) fail('Chave obrigatória ausente na origem.');
      if (seen.has(id) && JSON.stringify(seen.get(id)) !== JSON.stringify(r)) fail('Registros conflitantes na origem; cálculo interrompido.');
      seen.set(id, r);
    });
    return Array.from(seen.values());
  }
  function calculate(itemsMatrix, ordersMatrix, begin, finish) {
    begin = day(begin); finish = day(finish);
    if (begin > finish) fail('O início do período deve ser anterior ou igual ao fim.');
    var periodoRotulo = begin.split('-').reverse().join('/') + ' a ' + finish.split('-').reverse().join('/');
    var itemFields = ['item_chave', 'venda_chave', 'data', 'produto_codigo', 'produto_nome', 'categoria', 'quantidade', 'receita_item', 'atualizado_em'];
    var orderFields = ['venda_chave', 'data', 'faturamento_produtos', 'atualizado_em'];
    var items = unique(rows(itemsMatrix, itemFields), 'item_chave');
    var orders = unique(rows(ordersMatrix, orderFields), 'venda_chave');
    var selectedOrders = new Map(), generations = new Set();
    orders.forEach(function (o) {
      var date = day(o.data);
      if (date < begin || date > finish) return;
      var revenue = amount(o.faturamento_produtos);
      if (!o.atualizado_em) fail('Versão da carga ausente na origem.');
      generations.add(String(o.atualizado_em));
      selectedOrders.set(String(o.venda_chave), {date: date, revenue: revenue, items: 0, count: 0, skus: new Set()});
    });
    var products = new Map(), names = new Map(), selectedItems = 0;
    items.forEach(function (r) {
      var date = day(r.data);
      if (date < begin || date > finish) return;
      var o = selectedOrders.get(String(r.venda_chave));
      if (!o || date !== o.date) fail('Item sem pedido correspondente ou com data divergente.');
      if (!r.atualizado_em) fail('Versão da carga ausente na origem.');
      generations.add(String(r.atualizado_em));
      var revenue = amount(r.receita_item), quantity = Number(r.quantidade);
      if (!Number.isFinite(quantity) || quantity <= 0) fail('Quantidade inválida na origem.');
      var sku = String(r.produto_codigo || '').trim(), name = String(r.produto_nome || '').trim();
      var key = sku || 'sem_sku:' + r.item_chave;
      if (!products.has(key)) products.set(key, {key: key, sku: sku, name: name, category: String(r.categoria || ''), last: date, units: 0, revenue: 0, orders: new Set()});
      var p = products.get(key);
      if (date > p.last || (date === p.last && name >= p.name)) { p.name = name; p.category = String(r.categoria || ''); p.last = date; }
      p.units += quantity; p.revenue += revenue; p.orders.add(String(r.venda_chave));
      if (!Number.isSafeInteger(p.revenue)) fail('Valor acima do limite de precisão.');
      o.items += revenue; o.count += 1; selectedItems += 1;
      if (sku) {
        o.skus.add(sku);
        var previous = names.get(sku);
        if (!previous || date > previous.date || (date === previous.date && name >= previous.name)) names.set(sku, {date: date, name: name});
      }
    });
    if (generations.size > 1) fail('As bases pertencem a cargas diferentes. Aguarde a conclusão da atualização.');
    var complete = Array.from(selectedOrders.values()).every(function (o) { return o.count > 0 && Math.abs(o.revenue - o.items) <= 1; });
    var ranked = Array.from(products.values()).sort(function (a, b) { return b.revenue - a.revenue || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0); });
    var total = ranked.reduce(function (n, p) { return n + p.revenue; }, 0);
    if (!Number.isSafeInteger(total)) fail('Valor acima do limite de precisão.');
    var available = complete && total > 0 && ranked.every(function (p) { return !!p.sku; });
    var byUnits = ranked.slice().sort(function (a, b) { return b.units - a.units || (a.key < b.key ? -1 : 1); });
    var unitsRank = new Map(), lastUnits = null, urank = 0;
    byUnits.forEach(function (p, i) { if (p.units !== lastUnits) urank = i + 1; unitsRank.set(p.key, urank); lastUnits = p.units; });
    var cumulative = 0, lastRevenue = null, rank = 0, klass = '', updated = Array.from(generations)[0] || '';
    var abc = ranked.map(function (p, i) {
      if (p.revenue !== lastRevenue) {
        rank = i + 1;
        klass = available ? (cumulative * 100 < total * 80 ? 'A' : cumulative * 100 < total * 95 ? 'B' : 'C') : '';
      }
      cumulative += p.revenue; lastRevenue = p.revenue;
      return {periodo_inicio: begin, periodo_fim: finish, periodo_rotulo: periodoRotulo, produto_chave: p.key, produto_codigo: p.sku, produto_nome: p.name, categoria: p.category,
        unidades: p.units, receita_produtos: p.revenue / 100, pedidos: p.orders.size, ranking_receita: rank, ranking_unidades: unitsRank.get(p.key),
        participacao_receita_pct: total > 0 ? p.revenue / total * 100 : null,
        participacao_acumulada_pct: total > 0 ? cumulative / total * 100 : null,
        participacao_receita: total > 0 ? p.revenue / total : null, participacao_acumulada: total > 0 ? cumulative / total : null,
        classe_abc: klass, abc_disponivel: available ? 1 : 0, atualizado_em: updated};
    });
    var pairCounts = new Map();
    selectedOrders.forEach(function (o) {
      var skus = Array.from(o.skus).sort();
      if (skus.length > 500) fail('Pedido com produtos demais para cálculo de pares nesta consulta.');
      for (var i = 0; i < skus.length; i++) for (var j = i + 1; j < skus.length; j++) {
        var key = JSON.stringify([skus[i], skus[j]]); pairCounts.set(key, (pairCounts.get(key) || 0) + 1);
      }
    });
    var pairs = Array.from(pairCounts.entries()).sort(function (a, b) { return b[1] - a[1] || (a[0] < b[0] ? -1 : 1); }).map(function (entry) {
      var pair = JSON.parse(entry[0]);
      return {periodo_inicio: begin, periodo_fim: finish, periodo_rotulo: periodoRotulo, produto_a_codigo: pair[0], produto_a_nome: names.get(pair[0]).name,
        produto_b_codigo: pair[1], produto_b_nome: names.get(pair[1]).name, pedidos_juntos: entry[1], atualizado_em: updated};
    });
    var summary = [{periodo_inicio: begin, periodo_fim: finish, periodo_rotulo: periodoRotulo,
      skus_distintos: ranked.filter(function (p) { return !!p.sku; }).length, faturamento_produtos: total / 100,
      participacao_top5_receita_pct: complete && total > 0 ? ranked.slice(0, 5).reduce(function (n, p) { return n + p.revenue; }, 0) / total * 100 : null,
      abc_disponivel: available ? 1 : 0, atualizado_em: updated}];
    ['A','B','C'].forEach(function (c) {
      var produtos = abc.filter(function (p) { return p.classe_abc === c; });
      summary[0]['produtos_classe_' + c.toLowerCase()] = available ? produtos.length : null;
      summary[0]['receita_classe_' + c.toLowerCase()] = available ? produtos.reduce(function (n, p) { return n + p.receita_produtos; }, 0) / (total / 100) : null;
    });
    summary[0].participacao_top5_receita = summary[0].participacao_top5_receita_pct === null ? null : summary[0].participacao_top5_receita_pct / 100;
    return {abc: abc, resumo: summary, juntos: pairs, diagnostics: {orders: selectedOrders.size, items: selectedItems, complete: complete, available: available}};
  }
  return {calculate: calculate, day: day};
}());
if (typeof module !== 'undefined') module.exports = NexoAbc;
