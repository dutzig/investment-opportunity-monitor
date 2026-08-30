---
name: investment-opportunity-monitor
description: Monitora oportunidades de investimento em multiplas classes de ativo — DeFi (yield farming, lending, LPs via DefiLlama), acoes/ETFs (via Yahoo Finance/Alpha Vantage) e renda fixa/Tesouro Direto (via Tesouro Transparente) — buscando dados 100% reais via API publica, calculando um score de risco transparente e configuravel por classe, e salvando historico com timestamp para acompanhar tendencia ao longo do tempo. Use esta skill sempre que o usuario pedir para monitorar oportunidades de investimento, buscar/comparar yield, rodar uma varredura de mercado, atualizar scores de risco, ou perguntar coisas como "quais pools de DeFi estao pagando mais agora", "roda o monitor de oportunidades", "atualiza os scores de renda fixa", "quero o ranking de ativos por score de risco". NAO ativar para perguntas gerais de educacao financeira ("o que e APY", "como funciona Tesouro Direto", "vale a pena investir em ETF"), para pedidos de recomendacao personalizada de compra/venda ("devo comprar X?"), nem para qualquer operacao que envolva executar ordens, conectar wallet, assinar transacao ou lidar com credenciais de corretora — esta skill e 100% somente-leitura/analise e nunca decide por voce.
---

# investment-opportunity-monitor

## O que essa skill faz

Roda um monitor de oportunidades de investimento por classe de ativo. Cada
classe tem um arquivo de config (`config/<classe>.json`) que descreve de
onde vem o dado, quais campos importam, e como calcular um score de risco
comparativo — a logica de busca, filtro, score e historico e **compartilhada**
entre classes (`scripts/`), nunca duplicada por classe de ativo.

Classes disponiveis hoje (todas completas e testadas contra a fonte real):

| Classe | Config | Fonte | Observacao |
|---|---|---|---|
| DeFi (yield farming, lending, LPs) | `config/defi.json` | DefiLlama `/pools`, sem key | Universo completo (todas as pools ativas) |
| Renda fixa — Tesouro Direto (BR) | `config/fixed-income.json` | CSV oficial do Tesouro Transparente, sem key | Universo completo (todos os titulos, foto do dia mais recente) |
| Acoes B3 (BR) | `config/stocks.json` | Yahoo Finance chart API, sem key | Watchlist = uniao real de Ibovespa + Small Caps (149 tickers, via API oficial da B3), dividida em 7 fatias diarias — ver abaixo |
| Fundos Imobiliarios (FII, B3) | `config/fiis.json` | bolsai (P/VP, dividend yield, vacancia, inadimplencia) | **Exige API key gratuita** (usebolsai.com, sem cartao) — nunca no config, sempre em `BOLSAI_API_KEY`. Watchlist = indice IFIX oficial da B3 (106 fundos). |

Acoes de outros mercados (ex: EUA via Alpha Vantage) nao foram implementadas — o foco atual e' pt-BR / B3.

### Janela overnight + lotes da watchlist de acoes

O universo inteiro (151 tickers hoje) e' grande demais pra buscar de uma
vez sem arriscar rate limit do Yahoo (sensibilidade a rajada, nao um
limite diario documentado). Duas configs em `config/stocks.json`
resolvem isso juntas:

- `watchlist.overnight_window`: so' busca fora do pregao da B3 (22:00 as
  11:30 UTC = ~19h as ~08h30 BRT), pra sempre pegar o candle de
  fechamento definitivo em vez de preco no meio do dia. Fora da janela,
  o adapter devolve lista vazia (nao e' erro) e nao grava snapshot.
- `watchlist.batch`: dentro da janela, um cursor persistido em disco
  (`data/stocks_batch_cursor.json`) avança 20 tickers por execucao e da'
  a volta quando chega no fim. Rodando a cada 30min (timer do systemd),
  cobre o universo inteiro em ~4h, com folga grande dentro da janela de
  ~13h30 -- e sobra folga mesmo se o universo crescer (ex: BDR/ETF).

`watchlist.priority_local_path` (arquivo local, fora do git — ver secao
de dado pessoal abaixo) coloca holdings pessoais no inicio da lista
resolvida, entao os primeiros lotes da noite sempre cobrem eles antes do
resto do universo.

Pra ver o quadro combinado mais recente de todos os tickers (nao so' o
ultimo lote buscado), use:

```bash
python3 scripts/monitor.py --asset-class stocks --rolling-days 2 --top 30
```

Isso le do historico (SQLite) em vez de buscar ao vivo, pegando o registro
mais recente de cada ticker dentro da janela pedida — cada linha mostra
ha quantos dias aquele dado especifico foi buscado, nunca escondido.

Rodar uma classe completa:

```bash
python3 scripts/monitor.py --asset-class defi --top 15
```

Classes que exigem API key (`fiis`, e `stocks` pelos fundamentos via
`fundamentals_source`) precisam da variavel de ambiente definida antes
de rodar -- nunca coloque a chave dentro de um config (os configs vao
pro git, que e' publico):

```bash
export BOLSAI_API_KEY="sua_chave_aqui"
python3 scripts/monitor.py --asset-class fiis --top 12
```

Na VPS, defina isso no `Environment=` do arquivo `.service` do systemd
(nunca commitado), nao num `.env` dentro do repo.

**Dado pessoal (composicao de carteira, valores, listas de terceiros
pagas) nunca vai pro config versionado.** Se quiser somar tickers
proprios a uma watchlist alem do universo publico (indice oficial), use
`watchlist.local_supplement_path` apontando pra um arquivo em
`data/*.json` (ja coberto pelo `.gitignore`) no formato
`{"tickers": [...]}` -- ele e' somado ao `tickers` do config na hora de
buscar, sem nunca aparecer no repo publico. Se quiser garantir que
holdings pessoais sejam sempre buscados primeiro (antes do resto do
universo, protegendo contra corte por rate limit ou cota), use
`watchlist.priority_local_path` no mesmo formato -- coloca esses
tickers no inicio da lista resolvida. Ver `_resolve_watchlist_tickers()`
em `scripts/fetch.py`.

Rodar e obter JSON (para consumir em outro processo, ex: agendamento):

```bash
python3 scripts/monitor.py --asset-class defi --top 15 --json
```

Quando o config da classe define um bloco `opportunity_score` (DeFi já
define), o CLI calcula os dois números por ativo — `risk_score` (quão
arriscado, isolado) e `opportunity_score` (yield ajustado pelo risco) — e
`--rank-by opportunity_score` reordena o top N por oportunidade em vez de
risco:

```bash
python3 scripts/monitor.py --asset-class defi --top 15 --rank-by opportunity_score
```

Toda execucao grava um snapshot com timestamp no historico SQLite da classe
(`data/<classe>_history.db`) a menos que `--no-history` seja passado. Isso
permite consultar tendencia (o score de uma pool subindo/caindo ao longo do
tempo) sem reprocessar nada.

## Regras de seguranca (nao-negociaveis, valem para toda classe de ativo)

Esta skill e **somente-leitura / analise**. Ela NUNCA:

- conecta wallet, guarda ou pede chave privada / seed phrase / assina transacao
- executa ordem de compra ou venda em corretora ou exchange
- armazena credenciais de corretora ou qualquer dado financeiro sensivel do usuario
- inventa ou estima um valor no lugar de um dado que a API nao retornou —
  campo ausente e sempre marcado como `"indisponivel"` nos resultados
  (ver `scripts/score.py`, politica `exclude_and_renormalize`)
- da recomendacao personalizada de investimento ("compre X", "venda Y").
  Ela apresenta dados publicos e um score de risco calculado de forma
  transparente e documentada em cada config; a decisao e sempre do usuario.

Se em algum momento um pedido do usuario pedir qualquer uma dessas coisas
(ex: "compra essa pool pra mim", "conecta minha carteira"), explique que a
skill nao faz isso e pare — nao tente contornar via outro caminho.

## Como o score funciona

O motor de score (`scripts/score.py`, funcao `compute_scores`) e generico:
le a lista `components` de um bloco de config (`risk_score` ou
`opportunity_score`), aplica um `transform` conhecido em cada campo
(`log10_minmax`, `minmax_capped`, `abs_minmax_capped`, `categorical_rules`,
`relative_deviation_inverse`), opcionalmente inverte com `"invert": true`
quando o campo cru vai na direcao oposta a "maior = mais seguro" (ex:
prazo mais longo = mais risco em renda fixa, volatilidade mais alta = mais
risco em acoes), combina os resultados numa media ponderada
0-100, e redistribui o peso automaticamente quando um componente nao pode
ser calculado por falta de dado. Se o bloco definir
`missing_field_penalty_points`, tambem desconta esses pontos do score
final por componente ausente (sinaliza "menos confianca", em vez de so
redistribuir silenciosamente). A formula completa, os pesos e o *porque*
de cada componente estao documentados dentro do proprio JSON (campos
`methodology` e `notes`) — leia `config/defi.json` para o exemplo completo
antes de explicar o score ao usuario ou de ajustar pesos.

`opportunity_score` roda a mesma funcao uma segunda vez sobre os records
que ja tem `risk_score`, com um componente lendo o proprio `risk_score`
(via `minmax_capped` com `cap: 100`) combinado com o yield — assim
"quao arriscado" e "quao boa e a oportunidade dado o risco" ficam em dois
numeros separados, sem misturar tudo numa unica nota nem duplicar codigo.

## Detectores de sinal (além do score por ativo)

Além do `monitor.py` (score por ativo individual), existem scripts que
comparam ativos ENTRE SI pra achar assimetrias — primeiro exemplo:

```bash
python3 scripts/lending_asymmetry.py
```

Compara a taxa de supply do mesmo ativo (ex: USDC) entre vários mercados de
lending conhecidos (`config/lending-rate-asymmetry.json`), separando spread
**na mesma chain** (só risco de contrato/protocolo) de spread **cross-chain**
(soma risco de ponte, tempo e custo — sinalizado explicitamente, nunca
escondido no número). Não persiste histórico ainda (fica pra quando fizer
sentido acompanhar tendência desse sinal específico). O borrow-rate real
(pra estratégia de "pega emprestado barato aqui, deposita caro ali") não
está disponível de graça na DefiLlama (`/poolsBorrow` é pago, HTTP 402
confirmado) — uma v2 desse detector precisaria ler direto on-chain via RPC
público, sem key, só leitura.

## Como adicionar uma nova classe de ativo

Nao mexa em `monitor.py` nem em `score.py`. Siga
[`docs/adding-asset-class.md`](docs/adding-asset-class.md): crie um novo
`config/<classe>.json` e, se a fonte de dado for nova, um adapter pequeno em
`scripts/fetch.py`. `stocks.json` e `fixed-income.json` ja sao templates
com endpoint validado, prontos para servir de ponto de partida.

## Deploy (VPS)

Rodar localmente (o que este documento cobre) e diferente de deixar
rodando periodicamente numa VPS. As instrucoes completas de deploy —
usuario dedicado sem root, venv isolado, systemd timer, logrotate, e como
expor visualizacao com seguranca (nginx+htpasswd ou tunel SSH/VPN, nunca
porta aberta sem autenticacao) — estao em
[`docs/deploy-vps.md`](docs/deploy-vps.md). **Qualquer alteracao de
sistema/rede numa VPS real (criar usuario, instalar pacote, abrir porta,
configurar nginx) exige explicar o plano completo e pedir confirmacao
passo a passo antes de executar** — nunca rode esses comandos direto numa
VPS do usuario so porque o doc descreve o passo.

## Antes de considerar um modulo pronto

Rode de verdade (`python3 scripts/monitor.py --asset-class <classe>`) e
mostre uma amostra real do output para o usuario validar contra a fonte
original, antes de dizer que a classe esta funcionando. Nunca reporte uma
classe como concluida so porque o codigo roda sem erro — o teste real
contra a API e obrigatorio.
