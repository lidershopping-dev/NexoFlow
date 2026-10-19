const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../connectors/looker_abc/AbcCore.js');
const IH = ['item_chave','venda_chave','data','produto_codigo','produto_nome','categoria','quantidade','receita_item','atualizado_em'];
const OH = ['venda_chave','data','faturamento_produtos','atualizado_em'];
const V = '2026-10-17T12:00:00-03:00';
function item(id, order, sku, date, value, name = sku) {return [id,order,date,sku,name,'',1,value,V];}
function order(id, date, value) {return [id,date,value,V];}
function calc(items, orders, start='2026-10-01', end='2026-10-17') {return core.calculate([IH,...items],[OH,...orders],start,end);}
test('filtra antes de agrupar: liderança e classes mudam com o período',()=>{
 const items = [item('i1','o1','A','2026-10-01',100),item('i2','o2','B','2026-10-10',90),item('i3','o3','A','2026-10-10',1)];
 const orders = [order('o1','2026-10-01',100),order('o2','2026-10-10',90),order('o3','2026-10-10',1)];
 const full=calc(items,orders), recent=calc(items,orders,'2026-10-10','2026-10-10');
 assert.deepEqual(full.abc.map(r=>[r.produto_codigo,r.classe_abc]),[['A','A'],['B','A']]);
 assert.deepEqual(recent.abc.map(r=>[r.produto_codigo,r.classe_abc]),[['B','A'],['A','C']]);
 assert.equal(recent.resumo[0].faturamento_produtos,91);
 assert.equal(recent.abc[1].participacao_acumulada_pct,100);
});
test('limites inclusivos, centavos e duplicatas idênticas',()=>{
 const a=item('a','x','P','2026-10-01',0.1), b=item('b','x','P','2026-10-01',0.2);
 const r=calc([a,a,b],[order('x','2026-10-01',0.3)],'2026-10-01','2026-10-01');
 assert.equal(r.abc[0].receita_produtos,0.3); assert.equal(r.abc[0].unidades,2); assert.equal(r.diagnostics.available,true);
});
test('empates de receita ficam na mesma classe; ranking estável por SKU',()=>{
 const amounts=[80,10,10];
 const r=calc(amounts.map((n,i)=>item('i'+i,'o'+i,'S'+i,'2026-10-01',n)),amounts.map((n,i)=>order('o'+i,'2026-10-01',n)));
 assert.deepEqual(r.abc.map(r=>r.classe_abc),['A','B','B']);
 assert.deepEqual(r.abc.map(r=>r.ranking_receita),[1,2,2]);
});
test('participação dos Top 5 usa todos os produtos; classe C não desaparece',()=>{
 const ns=[50,20,10,8,7,5];
 const r=calc(ns.map((n,i)=>item('i'+i,'o'+i,'S'+i,'2026-10-01',n)),ns.map((n,i)=>order('o'+i,'2026-10-01',n)));
 assert.equal(r.resumo[0].participacao_top5_receita_pct,95);
 assert.equal(r.abc[5].classe_abc,'C');
 assert.equal(r.resumo[0].skus_distintos,6);
});
test('ausência de SKU preserva itens distintos e desativa as classes',()=>{
 const r=calc([item('a','x','','2026-10-01',1,'Igual'),item('b','x','','2026-10-01',2,'Igual')],[order('x','2026-10-01',3)]);
 assert.equal(r.abc.length,2); assert.ok(r.abc.every(r=>r.classe_abc==='' && r.abc_disponivel===0));
});
test('item ausente/divergente não produz ABC nem Top 5 válidos',()=>{
 const r=calc([item('a','x','A','2026-10-01',5)],[order('x','2026-10-01',10),order('y','2026-10-01',1)]);
 assert.equal(r.diagnostics.complete,false); assert.equal(r.abc[0].classe_abc,''); assert.equal(r.resumo[0].participacao_top5_receita_pct,null);
});
test('duplicatas conflitantes, órfãos e cargas concorrentes falham sem dados parciais',()=>{
 const a=item('a','x','A','2026-10-01',1), b=item('a','x','A','2026-10-01',2);
 assert.throws(()=>calc([a,b],[order('x','2026-10-01',1)]),/conflitantes/);
 assert.throws(()=>calc([a],[]),/sem pedido/);
 const other=order('x','2026-10-01',1);other[3]='2026-10-16T00:00:00-03:00';
 assert.throws(()=>calc([a],[other]),/cargas diferentes/);
});
test('juntos usa pedidos distintos e recorta o período antes dos pares',()=>{
 const r=calc([item('a','x','A','2026-10-01',1),item('b','x','B','2026-10-01',1),item('c','x','A','2026-10-01',1),item('d','y','A','2026-10-10',1),item('e','y','C','2026-10-10',1)],
 [order('x','2026-10-01',3),order('y','2026-10-10',2)],'2026-10-10','2026-10-10');
 assert.equal(r.juntos.length,1); assert.equal(r.juntos[0].produto_b_codigo,'C'); assert.equal(r.juntos[0].pedidos_juntos,1);
});
test('datas, intervalo, cabeçalhos e valores inválidos são rejeitados',()=>{
 assert.throws(()=>core.day('2026-02-31'),/Data inválida/);
 assert.throws(()=>calc([],[],'2026-10-17','2026-10-01'),/início/);
 assert.throws(()=>core.calculate([[]],[OH],'2026-10-01','2026-10-17'),/Cabeçalho/);
 assert.throws(()=>calc([item('i','o','A','2026-10-01',-1)],[order('o','2026-10-01',1)]),/monetário/);
});
test('sem dados: classes vazias e percentuais indisponíveis, não NaN',()=>{
 const r=calc([],[]);assert.deepEqual(r.abc,[]);assert.equal(r.resumo[0].participacao_top5_receita_pct,null);assert.equal(r.resumo[0].abc_disponivel,0);
});
test('campos nativos dos cards e percentuais usam o mesmo período',()=>{
 const ns=[80,15,5];const r=calc(ns.map((n,i)=>item('i'+i,'o'+i,'S'+i,'2026-10-01',n)),ns.map((n,i)=>order('o'+i,'2026-10-01',n)));
 const s=r.resumo[0];assert.deepEqual([s.produtos_classe_a,s.produtos_classe_b,s.produtos_classe_c],[1,1,1]);
 assert.equal(s.receita_classe_a+s.receita_classe_b+s.receita_classe_c,1);
 assert.equal(r.abc[0].participacao_receita,0.8);assert.equal(r.abc[2].participacao_acumulada,1);
 assert.equal(s.periodo_rotulo,'01/10/2026 a 17/10/2026');
 assert.equal(calc([],[]).resumo[0].produtos_classe_a,null);
});
