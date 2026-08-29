# Como adicionar uma nova classe de ativo

O objetivo é nunca duplicar `monitor.py` nem `score.py` por classe. Tudo o
que é específico de uma classe vive em `config/<classe>.json`, e — só
quando a fonte de dado é nova — um adapter pequeno em `scripts/fetch.py`.

## Passo a passo

1. **Valide o endpoint antes de escrever qualquer código.** Use `curl` para
   confirmar que a API responde e olhe a forma real da resposta (campos
   disponíveis, tipos, nulos). Nunca escreva um config a partir de memória
   ou suposição sobre o formato de uma API — confira de verdade.

2. **Crie `config/<classe>.json`** com estes blocos:
   - `asset_class`, `display_name`
   - `source`: `type` (nome que vai identificar o adapter), `endpoint`,
     `requires_api_key` e, se precisar de key, `api_key_env_var`
   - `record_fields`: mapeia nomes lógicos (`id_field`, `name_field`, etc)
     para os nomes de campo reais que a API retorna
   - `filters` (opcional): `min_value_field`, `range_field`, `exclude_if` —
     ver `config/defi.json` para o formato exato; `monitor.py` já sabe
     aplicar esses três tipos sem mudança de código
   - `risk_score`: `scale`, `methodology` (texto explicando o que o score
     representa e o que ele NÃO captura), `components` — cada componente
     com `field`, `label`, `weight`, `transform` e `notes` explicando o
     *porquê* daquele peso
   - `history`: `backend: "sqlite"`, `path`, `table`

3. **Escolha os `transform` dos componentes.** Os já implementados em
   `scripts/score.py` são:
   - `log10_minmax` — bom para métricas de escala muito ampla (TVL, volume)
   - `minmax_capped` — bom para "quanto maior/mais longo, melhor", com teto
   - `categorical_rules` — mapeia combinações de campos categóricos
     (`derive_from` + `rules` com `when`/`score`) para um valor 0-1
   - `relative_deviation_inverse` — penaliza desvio entre um valor atual e
     uma referência (ex: APY atual vs média de 30 dias)

   Se nenhum desses serve, é legítimo adicionar um `transform` novo em
   `score.py` (ele é genérico por design, então crescer o vocabulário de
   transforms beneficia todas as classes) — mas prefira reaproveitar antes
   de criar um novo.

4. **Se a fonte é nova, escreva o adapter em `scripts/fetch.py`:**
   ```python
   def fetch_minha_fonte(source: dict, cache_ttl: int) -> list[dict]:
       payload = fetch_json(source["endpoint"], ttl_seconds=cache_ttl)
       return payload["algum_campo_com_a_lista"]
   ```
   Registre em `ADAPTERS = {"minha_fonte_type": fetch_minha_fonte, ...}`.
   O adapter só busca e retorna os dados crus — nunca inventa um campo que
   a API não mandou. Se a API não tiver o dado, simplesmente não o inclua
   no record; `score.py` já trata ausência como "indisponível".

5. **Rode de verdade e valide.**
   ```bash
   python3 scripts/monitor.py --asset-class <classe> --top 10
   ```
   Compare uma amostra do output contra a fonte original antes de
   considerar a classe pronta. Não reporte como concluído sem esse passo.

## Exemplos reais já implementados

`config/defi.json`, `config/fixed-income.json` e `config/stocks.json` são os
três exemplos completos — leia-os antes de criar uma classe nova, cada um
mostra um padrão diferente:

- **defi**: universo completo via um único endpoint JSON
  (`fetch_defillama_yields`), sem watchlist, score puro + `opportunity_score`
  combinando yield com risco.
- **fixed-income**: universo completo via um CSV histórico gigante
  (`fetch_tesouro_transparente_csv`), filtrado no adapter pra só a "Data
  Base" mais recente. Usa `"invert": true` no prazo (prazo maior = mais
  risco, não menos) e campos derivados calculados de dado real (`prazo_anos`,
  `indexador`) em vez de vindos prontos da API.
- **stocks (B3)**: fonte que não lista "todos" os ativos, então funciona por
  `watchlist.tickers` explícita (`fetch_yahoo_finance_chart`), com
  `request_interval_seconds` pra não tomar rate limit (HTTP 429) da fonte —
  quando um ticker falha, ele fica marcado com `_fetch_error` e o
  `risk_score` sai "indisponível" pra aquele ativo, sem derrubar a consulta
  inteira nem inventar dado. Beta usa `abs_minmax_capped` (magnitude importa,
  não o sinal).

Se for adicionar uma classe nova cuja fonte não lista "todos" os ativos
gratuitamente (ex: ações de outro mercado), copie o padrão de watchlist do
`stocks.json` em vez de reinventar.

Ao terminar uma classe nova, remova qualquer campo `"status":
"TEMPLATE_NAO_IMPLEMENTADO"` do config só depois que o adapter estiver
escrito, testado contra a fonte real, e você tiver mostrado uma amostra do
output para validação.
